"""Outlook integration through MCP: put a reviewed Chanakya draft into the user's Outlook Drafts folder. Never sends.

Transport: Softeria's ms-365-mcp-server (the same server MCP-CHANAKYA/outlook_mvp uses), started over stdio with
`--enabled-tools` limited to DRAFT_TOOLS, which also limits the Microsoft Graph scopes it asks for to User.Read and
Mail.ReadWrite. Mail.Send is never requested, and no send tool exists in this module (a test asserts that).

Safety:
* Only drafts a human has edited or marked ready, with no [PERSONALISE] marker, voice issue or forbidden proof claim.
* One Outlook draft per Chanakya draft (the message id is stored on outreach_drafts; a second push is refused).
* Test mode (CHANAKYA_OUTLOOK_TEST_MODE, on by default): a recipient is filled in only if it is an example.* address,
  on CHANAKYA_OUTLOOK_TEST_ALLOWLIST, or the signed-in mailbox. Otherwise the Outlook draft is created with no
  recipient and the response says so; the human adds the address in Outlook.
* Tokens stay in the MCP server's own encrypted cache (%APPDATA%), never in Chanakya's database or logs.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from chanakya.storage.db import now

DRAFT_TOOLS = "^(get-current-user|create-draft-email|get-mail-message)$"
SCOPES = ["User.Read", "Mail.ReadWrite"]
TEST_DOMAINS = ("example.com", "example.org", "example.net")
DEFAULT_JS = Path(__file__).resolve().parents[4] / "MCP-CHANAKYA" / "outlook_mvp" / "node_modules" / "@softeria" / \
    "ms-365-mcp-server" / "dist" / "index.js"


class OutlookError(RuntimeError):
    pass


def server_js() -> Path:
    return Path(os.getenv("OUTLOOK_MCP_SERVER_JS") or DEFAULT_JS)


def _args(extra: list[str]) -> list[str]:
    a = [str(server_js())]
    if os.getenv("OUTLOOK_ORG_MODE", "1") != "0":
        a.append("--org-mode")
    return a + ["--enabled-tools", DRAFT_TOOLS, *extra]


def _env() -> dict:
    return {**os.environ, **{k: v for k, v in os.environ.items() if k.startswith("MS365_MCP_")}}


def test_mode() -> bool:
    return os.getenv("CHANAKYA_OUTLOOK_TEST_MODE", "1") != "0"


def login_command() -> str:
    """Device-code sign-in the user runs once (opens microsoft.com/devicelogin). Shown in Settings > Outlook."""
    parts = _args(["--login"])
    parts[parts.index("--enabled-tools") + 1] = f'"{DRAFT_TOOLS}"'
    return "node " + f'"{parts[0]}" ' + " ".join(parts[1:])


# ------------------------------------------------------------------ MCP session

class MCPSession:
    """Real stdio session to ms-365-mcp-server."""

    def __init__(self):
        self._stack = None
        self.session = None

    async def __aenter__(self):
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        if not server_js().exists():
            raise OutlookError(f"ms-365-mcp-server not installed at {server_js()} (run npm install in "
                               "MCP-CHANAKYA/outlook_mvp or set OUTLOOK_MCP_SERVER_JS)")
        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(
                stdio_client(StdioServerParameters(command="node", args=_args([]), env=_env())))
            self.session = await self._stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
        except BaseException as e:  # noqa: BLE001 — surface the leaf error, not an anyio ExceptionGroup
            try:
                await self._stack.aclose()
            except BaseException:  # noqa: BLE001
                pass
            from chanakya.clay.mcp_client import unwrap
            raise OutlookError(f"could not start the Outlook MCP server: {unwrap(e)}") from None
        return self

    async def __aexit__(self, *exc):
        try:
            await self._stack.aclose()
        except BaseException:  # noqa: BLE001
            pass

    async def list_tools(self) -> list[str]:
        return [t.name for t in (await self.session.list_tools()).tools]

    async def call(self, tool: str, args: dict):
        if tool not in ("get-current-user", "create-draft-email", "get-mail-message"):
            raise OutlookError(f"tool {tool} is not allowed (drafts only)")
        r = await self.session.call_tool(tool, args)
        text = "\n".join(getattr(c, "text", "") for c in r.content)
        if getattr(r, "is_error", None) or getattr(r, "isError", None):
            raise OutlookError(f"{tool} failed: {text[:300]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict) and data.get("error"):
            raise OutlookError(f"{tool} failed: {json.dumps(data['error'])[:300]}")
        return data


# ------------------------------------------------------------------ status

def verify_login(timeout: int = 60) -> dict:
    """Runs the server's own --verify-login (no Graph call, no token shown)."""
    if not shutil.which("node"):
        return {"success": False, "message": "Node.js is not installed"}
    if not server_js().exists():
        return {"success": False, "message": f"ms-365-mcp-server not installed at {server_js()}"}
    try:
        p = subprocess.run(["node", *_args(["--verify-login"])], capture_output=True, text=True, timeout=timeout,
                           env=_env())
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "verify-login timed out"}
    line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.strip().startswith("{")), "")
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"success": False, "message": (p.stderr or p.stdout).strip()[-300:] or "no output"}


def status(*, session_factory=MCPSession, verify=verify_login) -> dict:
    out = {"server": str(server_js()), "installed": server_js().exists(), "node": bool(shutil.which("node")),
           "mcp_connected": False, "tools": [], "authenticated": False, "account": None, "permissions": SCOPES,
           "send_permission": False, "test_mode": test_mode(), "error": None,
           "login_command": login_command(), "expected_account": expected_account(),
           "login": login_state(),
           "note": "Chanakya creates Outlook drafts only. Mail.Send is never requested; you send from Outlook."}
    v = verify()
    out["authenticated"] = bool(v.get("success"))
    if not out["authenticated"]:
        out["error"] = v.get("message") or "not signed in"
    if not out["installed"]:
        return out

    async def probe():
        async with session_factory() as s:
            out["mcp_connected"] = True
            out["tools"] = await s.list_tools()
            out["send_tools_exposed"] = [t for t in out["tools"] if "send" in t.lower()]
            if out["authenticated"]:
                me = await s.call("get-current-user", {"select": "displayName,mail,userPrincipalName"})
                out["account"] = {"name": me.get("displayName"), "address": me.get("mail") or
                                  me.get("userPrincipalName")} if isinstance(me, dict) else None
                exp, addr = expected_account(), (out["account"] or {}).get("address")
                out["account_matches_expected"] = None if not exp else bool(addr and addr.lower() == exp.lower())
    try:
        asyncio.run(probe())
    except OutlookError as e:
        out["error"] = str(e)
    return out


# ------------------------------------------------------------------ connect / disconnect (device code)

import re
import threading

_LOGIN: dict = {"state": "idle"}
_LOCK = threading.Lock()
_CODE = re.compile(r"(https://\S*devicelogin\S*|https://login\.microsoft\S*).*?code\s+([A-Z0-9-]{6,})", re.I | re.S)


def expected_account() -> str | None:
    return (os.getenv("MS365_MCP_EXPECTED_USERNAME") or "").strip() or None


def login_state() -> dict:
    with _LOCK:
        return {k: v for k, v in _LOGIN.items() if k != "proc"}


def _run_login(proc):
    buf = ""
    for line in proc.stdout:
        buf += line
        m = _CODE.search(buf)
        with _LOCK:
            if m and _LOGIN.get("state") == "starting":
                _LOGIN.update(state="waiting_for_user", verification_uri=m.group(1).rstrip(".,"),
                              user_code=m.group(2))
            if line.strip().startswith("{"):
                try:
                    _LOGIN["result"] = json.loads(line)
                except json.JSONDecodeError:
                    pass
    proc.wait()
    with _LOCK:
        res = _LOGIN.get("result") or {}
        ok = proc.returncode == 0 and res.get("success", False)
        _LOGIN.update(state="connected" if ok else "failed", finished_at=now(),
                      error=None if ok else (res.get("message") or buf.strip()[-400:] or
                                             f"login exited with code {proc.returncode}"))
        _LOGIN.pop("proc", None)


def start_login(timeout_s: int = 900) -> dict:
    """Start the MCP server's own device-code login (same scopes: User.Read, Mail.ReadWrite). Returns the code to show."""
    if not shutil.which("node"):
        raise OutlookError("Node.js is not installed")
    if not server_js().exists():
        raise OutlookError(f"ms-365-mcp-server not installed at {server_js()}")
    with _LOCK:
        if _LOGIN.get("state") in ("starting", "waiting_for_user"):
            return {k: v for k, v in _LOGIN.items() if k != "proc"}
        proc = subprocess.Popen(["node", *_args(["--login"])], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=_env())
        _LOGIN.clear()
        _LOGIN.update(state="starting", started_at=now(), proc=proc, expected_account=expected_account())
    threading.Thread(target=_run_login, args=(proc,), daemon=True).start()
    t = threading.Timer(timeout_s, lambda: proc.poll() is None and proc.kill())
    t.daemon = True
    t.start()
    import time
    for _ in range(40):
        if login_state()["state"] != "starting":
            break
        time.sleep(0.5)
    return login_state()


def cancel_login() -> dict:
    with _LOCK:
        proc = _LOGIN.get("proc")
    if proc and proc.poll() is None:
        proc.kill()
    with _LOCK:
        _LOGIN.update(state="idle", error=None)
    return login_state()


def logout(timeout: int = 60) -> dict:
    cancel_login()
    if not server_js().exists():
        raise OutlookError(f"ms-365-mcp-server not installed at {server_js()}")
    p = subprocess.run(["node", *_args(["--logout"])], capture_output=True, text=True, timeout=timeout, env=_env())
    if p.returncode != 0:
        raise OutlookError(f"logout failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return {"disconnected": True}


# ------------------------------------------------------------------ draft push

def _check_draft(con, did: int, allow_unreviewed: bool = False):
    from chanakya.intel.proof import claim_violations
    from chanakya.intel.voice import voice_issues
    d = con.execute("SELECT * FROM outreach_drafts WHERE id=?", (did,)).fetchone()
    if d is None:
        raise OutlookError(f"unknown draft {did}")
    if d["outlook_message_id"]:
        raise OutlookError(f"draft {did} is already in Outlook (created {d['outlook_created_at']})")
    ok = ("edited", "approved", "ready", "failed", "saved_to_outlook") + (("generated",) if allow_unreviewed else ())
    if d["status"] not in ok:
        raise OutlookError(f"draft {did} is '{d['status']}': a human must edit or mark it ready (or approve it) first")
    text = f"{d['subject']}\n{d['body']}"
    bad = (["still has [PERSONALISE] markers"] if "[PERSONALISE" in text else []) + \
        voice_issues(text) + claim_violations(text, "PRD-SETU")
    if bad:
        raise OutlookError("fix before sending to Outlook: " + "; ".join(bad))
    return d


def _recipient(addr: str | None, me: str | None) -> tuple[str | None, str]:
    a = (addr or "").strip().lower()
    if not a:
        return None, "no address on the draft: add the recipient in Outlook"
    if not test_mode():
        return a, "recipient filled in"
    allow = {x.strip().lower() for x in os.getenv("CHANAKYA_OUTLOOK_TEST_ALLOWLIST", "").split(",") if x.strip()}
    if a.split("@")[-1] in TEST_DOMAINS or a in allow or (me and a == me.lower()):
        return a, "recipient filled in (test mode allowlist)"
    return None, "test mode: recipient left blank so a real person cannot be addressed by accident; add it in Outlook"


def _plain(x: str | None) -> str:
    x = re.sub(r"<[^>]+>", " ", x or "")
    x = x.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\s+", " ", x).strip()


def readback_check(back, mid: str, to: str | None, subject: str, body: str) -> dict:
    """What Outlook really stored, compared field by field with the approved draft."""
    b = back if isinstance(back, dict) else {}
    got_to = [((x or {}).get("emailAddress") or {}).get("address", "").lower() for x in b.get("toRecipients") or []]
    return {"same_message": b.get("id") == mid, "is_draft": b.get("isDraft") is True,
            "subject": (b.get("subject") or "") == subject,
            "recipient": got_to == ([to.lower()] if to else []) if "toRecipients" in b else True,
            "body": _plain(body)[:200] in _plain(((b.get("body") or {}).get("content")) or "") if b.get("body") else True}


def _mark(con, did, d, to, by, reason):
    con.execute("INSERT INTO outreach_draft_history(draft_id,action,from_status,to_status,subject,body,reviewer,reason,"
                "at) VALUES(?,?,?,?,?,?,?,?,?)", (did, to, d["status"], to, d["subject"], d["body"], by, reason, now()))
    con.execute("UPDATE outreach_drafts SET status=?, updated_at=? WHERE id=?", (to, now(), did))
    con.commit()


def create_outlook_draft(con, did: int, *, by: str, session_factory=MCPSession, allow_unreviewed: bool = False) -> dict:
    """allow_unreviewed: the user asked for generated drafts to go straight to Outlook Drafts for review THERE. The
    proof/voice checks still apply and nothing is ever sent."""
    if not (by or "").strip():
        raise OutlookError("reviewer name is required")
    d = _check_draft(con, did, allow_unreviewed)

    async def push():
        async with session_factory() as s:
            me = await s.call("get-current-user", {"select": "displayName,mail,userPrincipalName"})
            my = (me.get("mail") or me.get("userPrincipalName")) if isinstance(me, dict) else None
            to, note = _recipient(d["to_address"], my)
            msg = {"subject": d["subject"], "body": {"contentType": "text", "content": d["body"]},
                   "toRecipients": [{"emailAddress": {"address": to}}] if to else []}
            r = None
            if getattr(s, "supports_find", False):  # a retry after a lost response must not create a second draft
                from chanakya.outreach.graph_oauth import PROP_GUID
                msg["singleValueExtendedProperties"] = [{"id": f"String {{{PROP_GUID}}} Name ChanakyaDraftId",
                                                         "value": str(did)}]
                found = None
                for _ in range(2):  # Graph sometimes answers this filter with a transient 500
                    try:
                        found = await s.call("find-draft", {"draft_id": did})
                        break
                    except OutlookError:
                        await asyncio.sleep(1.5)
                r = (found.get("value") or [None])[0] if isinstance(found, dict) else None
            if r is None:
                r = await s.call("create-draft-email", {"body": msg})
            if not isinstance(r, dict) or not r.get("id"):
                raise OutlookError(f"create-draft-email returned no message id: {str(r)[:200]}")
            try:  # read the message back from the mailbox: proof it is in Drafts with the approved content
                back = await s.call("get-mail-message", {"messageId": r["id"],
                                                         "select": "id,isDraft,subject,body,toRecipients"})
                r["_check"] = readback_check(back, r["id"], to, d["subject"], d["body"])
                r["_verified"] = all(r["_check"].values())
            except Exception as e:  # noqa: BLE001 - the draft exists; the read-back failure is reported
                r["_verified"], r["_verify_error"] = False, str(e)[:200]
            return r, my, to, note
    try:
        r, my, to, note = asyncio.run(push())
    except Exception as e:
        _mark(con, did, d, "failed", by, f"Outlook save failed: {str(e)[:200]}; nothing was sent")
        raise
    if r.get("isDraft") is False:  # Graph reports isDraft on the created message; anything else is a hard stop
        raise OutlookError("Outlook reported the message is not a draft; stopping")
    if r.get("_check") is not None:
        r["_check"]["mailbox"] = bool(my)
        r["_verified"] = all(r["_check"].values())
    t = now()
    con.execute("UPDATE outreach_drafts SET outlook_message_id=?, outlook_web_link=?, outlook_account=?, "
                "outlook_created_at=?, updated_at=? WHERE id=?", (r["id"], r.get("webLink"), my, t, t, did))
    con.execute("INSERT INTO outreach_draft_history(draft_id,action,from_status,to_status,subject,body,reviewer,reason,"
                "at) VALUES(?,?,?,?,?,?,?,?,?)", (did, "outlook_draft", d["status"], d["status"], d["subject"],
                                                    d["body"], by, f"Outlook draft created ({note}); read back from mailbox: "
                                                    f"{'verified in Drafts' if r.get('_verified') else 'NOT verified'}; not sent", t))
    con.commit()
    if not r.get("_verified"):  # the message id is stored (no duplicate on retry), but success needs the read-back
        _mark(con, did, d, "failed", by, "Outlook read-back did not match; nothing was sent")
        failed = [k for k, v in (r.get("_check") or {}).items() if not v]
        raise OutlookError(f"Outlook created message {r['id'][:12]}... but the read-back check failed "
                           f"({', '.join(failed) or r.get('_verify_error') or 'not readable'}). Open Outlook Drafts "
                           "and check it; nothing was sent.")
    _mark(con, did, d, "saved_to_outlook", by, "saved to Outlook Drafts and verified by read-back; not sent")
    return {"draft_id": did, "outlook_message_id": r["id"], "web_link": r.get("webLink"), "account": my,
            "recipient": to, "note": note, "sent": False, "verified_in_drafts": bool(r.get("_verified")), "readback": r.get("_check"),
            "verify_error": r.get("_verify_error")}
