"""Outlook through Chanakya's OWN Microsoft Entra app (delegated Microsoft Graph), as an alternative to the shared
Softeria app used by the MCP route (outreach/outlook.py, unchanged).

* Authorization-code flow with PKCE, started from Settings > Outlook; the redirect comes back to the backend through
  the UI proxy, so the Chanakya session cookie binds the Microsoft account to the signed-in Chanakya user.
* Delegated scopes only: openid profile offline_access User.Read Mail.ReadWrite. Mail.Send and application
  permissions are never requested (a test asserts it). Mail.ReadWrite is the narrowest Graph scope that can create a
  message in the user's Drafts folder.
* Tokens are encrypted at rest (Fernet) in table outlook_connections and refreshed on the backend; they are never
  returned by the API, logged, or written to the UI. Key: CHANAKYA_TOKEN_KEY, else a key file created once in data/.
* Draft creation reuses outlook.create_outlook_draft (same human-review checks, test-mode recipient rule and
  "never sent" guarantees) with a Graph session that offers the same two calls as the MCP session.
* Tenant policy is never bypassed: if Microsoft says an administrator must approve, the error says so and names the
  one-time admin action. No app setting can override a tenant's consent policy.

Configuration (.env, set by the user, never pasted into chat):
  CHANAKYA_MS_CLIENT_ID      Application (client) ID of the Wayam app registration
  CHANAKYA_MS_TENANT_ID      Wayam's tenant ID (single-tenant app), or 'organizations'
  CHANAKYA_MS_CLIENT_SECRET  optional: only if the app is registered as a confidential web app
  CHANAKYA_MS_REDIRECT_URI   default http://localhost:3000/auth/callback (the UI forwards it, server-side, to
                             /api/intel/outlook/graph/callback; the same value is sent in authorize and token calls)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from chanakya.storage.db import now

SCOPES = ["openid", "profile", "offline_access", "User.Read", "Mail.ReadWrite"]
FORBIDDEN = ("Mail.Send", ".All", "Mail.ReadWrite.Shared")
GRAPH = "https://graph.microsoft.com/v1.0"
PROP_GUID = "8c3e3a5e-6a0f-4b8e-9b9a-3c7f0c1d2e4f"  # Chanakya's extended-property namespace on draft messages
ROOT = Path(__file__).resolve().parents[3]
_PENDING: dict[str, dict] = {}  # state -> {verifier, user_id, at}

ADMIN_CODES = {"AADSTS65001", "AADSTS90094", "AADSTS90095", "AADSTS900941", "AADSTS50105", "AADSTS53003"}


class GraphAuthError(RuntimeError):
    pass


def config() -> dict:
    return {"client_id": (os.getenv("CHANAKYA_MS_CLIENT_ID") or "").strip() or None,
            "tenant": (os.getenv("CHANAKYA_MS_TENANT_ID") or "organizations").strip(),
            "confidential": bool(os.getenv("CHANAKYA_MS_CLIENT_SECRET")),
            "redirect_uri": os.getenv("CHANAKYA_MS_REDIRECT_URI",
                                      "http://localhost:3000/auth/callback")}


_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def tenant_problem() -> str | None:
    t = config()["tenant"]
    if not _GUID.match(t):
        return (f"CHANAKYA_MS_TENANT_ID '{t}' is not a valid Directory (tenant) ID: copy the 36-character GUID from "
                "Entra admin center > Overview > Directory (tenant) ID")
    return None


def configured() -> bool:
    return bool(config()["client_id"]) and tenant_problem() is None


def _authority() -> str:
    return f"https://login.microsoftonline.com/{config()['tenant']}/oauth2/v2.0"


assert not any(f in s for s in SCOPES for f in FORBIDDEN), "forbidden scope"


# ------------------------------------------------------------------ token storage (encrypted)
def _fernet():
    from cryptography.fernet import Fernet
    key = os.getenv("CHANAKYA_TOKEN_KEY")
    if not key:
        p = Path(os.getenv("CHANAKYA_TOKEN_KEY_FILE", ROOT / "data" / ".outlook_token_key"))
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(Fernet.generate_key())
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        key = p.read_bytes().strip()
    return Fernet(key if isinstance(key, bytes) else key.encode())


def _ensure(con):
    con.execute("CREATE TABLE IF NOT EXISTS outlook_connections (user_id INTEGER PRIMARY KEY, account TEXT, "
                "display_name TEXT, tenant_id TEXT, scopes TEXT, token_enc BLOB NOT NULL, expires_at REAL NOT NULL, "
                "connected_at TEXT NOT NULL, refreshed_at TEXT, last_error TEXT)")


def _save(con, user_id: int, tok: dict, me: dict | None = None):
    _ensure(con)
    enc = _fernet().encrypt(json.dumps({"access_token": tok["access_token"],
                                        "refresh_token": tok.get("refresh_token")}).encode())
    exp = time.time() + int(tok.get("expires_in", 3600)) - 60
    if me is not None:
        con.execute("INSERT OR REPLACE INTO outlook_connections(user_id,account,display_name,tenant_id,scopes,token_enc,"
                    "expires_at,connected_at,refreshed_at,last_error) VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                    (user_id, me.get("mail") or me.get("userPrincipalName"), me.get("displayName"),
                     tok.get("_tenant"), tok.get("scope", ""), enc, exp, now(), now()))
    else:
        con.execute("UPDATE outlook_connections SET token_enc=?, expires_at=?, scopes=COALESCE(?,scopes), "
                    "refreshed_at=?, last_error=NULL WHERE user_id=?", (enc, exp, tok.get("scope"), now(), user_id))
    con.commit()


def _explain(err: str, desc: str) -> str:
    code = next((c for c in ADMIN_CODES if c in (desc or "")), None)
    if code or err in ("consent_required", "access_denied") and "admin" in (desc or "").lower():
        return ("Your Microsoft 365 organisation requires an administrator to approve Chanakya before users can "
                "connect (" + (code or err) + "). This is a tenant policy; Chanakya cannot and does not bypass it. "
                "One-time fix for an Entra administrator: Entra admin center > Enterprise applications > Chanakya > "
                "Permissions > Grant admin consent for the organisation (User.Read, Mail.ReadWrite, delegated). "
                "After that, every user connects without an approval screen.")
    if err == "access_denied":
        return "You cancelled the Microsoft sign-in or declined the permissions."
    return f"Microsoft sign-in failed: {err}: {(desc or '').splitlines()[0][:300]}"


# ------------------------------------------------------------------ OAuth flow
def start(user_id: int) -> dict:
    c = config()
    if not c["client_id"]:
        raise GraphAuthError("Chanakya's Microsoft app is not configured: set CHANAKYA_MS_CLIENT_ID (and "
                             "CHANAKYA_MS_TENANT_ID) in .env, then restart")
    if tenant_problem():
        raise GraphAuthError(tenant_problem())
    for k in [k for k, v in _PENDING.items() if time.time() - v["at"] > 600]:
        _PENDING.pop(k, None)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    _PENDING[state] = {"verifier": verifier, "user_id": user_id, "at": time.time()}
    q = {"client_id": c["client_id"], "response_type": "code", "redirect_uri": c["redirect_uri"],
         "response_mode": "query", "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
         "code_challenge_method": "S256", "prompt": "select_account"}
    return {"auth_url": f"{_authority()}/authorize?{urlencode(q)}", "scopes": SCOPES}


def _token_request(data: dict, transport=None) -> dict:
    c = config()
    data = {"client_id": c["client_id"], "scope": " ".join(SCOPES), **data}
    if c["confidential"]:
        data["client_secret"] = os.environ["CHANAKYA_MS_CLIENT_SECRET"]
    with httpx.Client(timeout=30, transport=transport) as h:
        r = h.post(f"{_authority()}/token", data=data)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in j:
        raise GraphAuthError(_explain(j.get("error", f"HTTP {r.status_code}"), j.get("error_description", "")))
    granted = set(j.get("scope", "").split())
    if any("Mail.Send" in s for s in granted) and not tolerate_granted_send():
        raise GraphAuthError("Microsoft granted Mail.Send, which Chanakya never requests; refusing the token. Ask "
                             "the administrator to revoke Mail.Send for Chanakya, or set "
                             "CHANAKYA_TOLERATE_GRANTED_MAIL_SEND=1 to keep it unused.")
    return j


def tolerate_granted_send() -> bool:
    """The tenant admin approved Mail.Send for the app. Chanakya still never requests or uses it (no send tool, no
    send endpoint); with this set, the connection is kept and Settings shows a warning instead of refusing."""
    return os.getenv("CHANAKYA_TOLERATE_GRANTED_MAIL_SEND", "0") == "1"


def _claims(jwt: str) -> dict:
    """Claims of a token received directly from Microsoft's token endpoint over TLS (no signature check is needed
    for a token Chanakya itself just redeemed; it is never accepted from the browser)."""
    try:
        part = jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def _me(access_token: str, transport=None) -> dict:
    with httpx.Client(timeout=30, transport=transport) as h:
        r = h.get(f"{GRAPH}/me?$select=displayName,mail,userPrincipalName",
                  headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code != 200:
        raise GraphAuthError(f"Graph /me failed: HTTP {r.status_code}")
    return r.json()


def callback(con, user_id: int, *, code=None, state=None, error=None, error_description=None, transport=None) -> dict:
    if error:
        raise GraphAuthError(_explain(error, error_description or ""))
    p = _PENDING.pop(state or "", None)
    if p is None or time.time() - p["at"] > 600:
        raise GraphAuthError("sign-in link expired or was not started here: click Connect again")
    if p["user_id"] != user_id:
        raise GraphAuthError("sign-in was started by a different Chanakya user")
    tok = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": config()["redirect_uri"],
                          "code_verifier": p["verifier"]}, transport)
    tid = _claims(tok.get("id_token") or tok["access_token"]).get("tid")
    if (tid or "").lower() != config()["tenant"].lower():
        raise GraphAuthError("This Microsoft account belongs to a different organisation. Sign in with your Wayam "
                             "work account.")
    tok["_tenant"] = tid
    me = _me(tok["access_token"], transport)
    _save(con, user_id, tok, me)
    return {"connected": True, "account": me.get("mail") or me.get("userPrincipalName")}


def access_token(con, user_id: int, transport=None) -> str:
    _ensure(con)
    r = con.execute("SELECT token_enc, expires_at FROM outlook_connections WHERE user_id=?", (user_id,)).fetchone()
    if r is None:
        raise GraphAuthError("Outlook is not connected for this user: Settings > Outlook > Connect")
    t = json.loads(_fernet().decrypt(r["token_enc"]))
    if time.time() < r["expires_at"]:
        return t["access_token"]
    if not t.get("refresh_token"):
        raise GraphAuthError("Outlook session expired: connect again")
    try:
        tok = _token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]}, transport)
    except GraphAuthError as e:
        con.execute("UPDATE outlook_connections SET last_error=? WHERE user_id=?", (str(e)[:500], user_id))
        con.commit()
        raise
    tok.setdefault("refresh_token", t["refresh_token"])
    _save(con, user_id, tok)
    return tok["access_token"]


def status(con, user_id: int) -> dict:
    _ensure(con)
    c = config()
    r = con.execute("SELECT account, display_name, scopes, expires_at, connected_at, refreshed_at, last_error FROM "
                    "outlook_connections WHERE user_id=?", (user_id,)).fetchone()
    return {"configured": configured(), "config_problem": None if c["client_id"] is None else tenant_problem(),
            "tenant": c["tenant"], "redirect_uri": c["redirect_uri"],
            "app_type": "confidential web app" if c["confidential"] else "public client + PKCE",
            "client_id_set": bool(c["client_id"]), "requested_scopes": SCOPES, "send_permission": False,
            "connected": r is not None, "account": r["account"] if r else None,
            "send_granted_unused": bool(r and "Mail.Send" in (r["scopes"] or "")),
            "display_name": r["display_name"] if r else None,
            "granted_scopes": (r["scopes"] or "").split() if r else [], "connected_at": r["connected_at"] if r else None,
            "refreshed_at": r["refreshed_at"] if r else None, "error": r["last_error"] if r else None}


def disconnect(con, user_id: int) -> dict:
    _ensure(con)
    con.execute("DELETE FROM outlook_connections WHERE user_id=?", (user_id,))
    con.commit()
    return {"disconnected": True, "note": "Chanakya deleted its stored tokens. To also remove the consent, open "
                                          "https://myapps.microsoft.com > Chanakya > Manage > Revoke permissions."}


# ------------------------------------------------------------------ Graph session (same interface as MCPSession)
class GraphSession:
    supports_find = True  # drafts carry a ChanakyaDraftId property, so a retry finds instead of re-creating
    def __init__(self, con, user_id: int, transport=None):
        self.con, self.user_id, self.transport = con, user_id, transport

    async def __aenter__(self):
        from chanakya.outreach.outlook import OutlookError
        try:
            self.token = access_token(self.con, self.user_id, self.transport)
        except GraphAuthError as e:
            raise OutlookError(str(e)) from None
        return self

    async def __aexit__(self, *exc):
        return None

    async def call(self, tool: str, args: dict):
        from chanakya.outreach.outlook import OutlookError
        h = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as c:
            if tool == "get-current-user":
                r = await c.get(f"{GRAPH}/me", params={"$select": args.get("select", "displayName,mail")}, headers=h)
            elif tool == "create-draft-email":
                r = await c.post(f"{GRAPH}/me/messages", json=args["body"], headers=h)  # creates in Drafts; no send
            elif tool == "find-draft":  # retry safety: a draft already created for this Chanakya draft id
                pid = f"String {{{PROP_GUID}}} Name ChanakyaDraftId"
                r = await c.get(f"{GRAPH}/me/mailFolders/drafts/messages", headers=h, params={
                    "$filter": f"singleValueExtendedProperties/Any(ep: ep/id eq '{pid}' and ep/value eq "
                               f"'{args['draft_id']}')", "$select": "id,isDraft,subject,webLink", "$top": "1"})
            elif tool == "get-mail-message":  # read back from the Drafts folder only
                r = await c.get(f"{GRAPH}/me/mailFolders/drafts/messages/{args['messageId']}",
                                params={"$select": "id,isDraft,subject,body,toRecipients"}, headers=h)
            else:
                raise OutlookError(f"tool {tool} is not allowed (drafts only)")
        if r.status_code >= 400:
            raise OutlookError(f"{tool} failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json()
