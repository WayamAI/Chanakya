"""In-app Clay (re)authorisation: the same OAuth 2.1 + PKCE loopback flow as scripts/clay_discover.py, but started
from the UI (Integrations > Clay > Connect) instead of a terminal.

start()  -> begins a background connection attempt. If the stored token is still valid (or can be refreshed) it
            connects at once. Otherwise the SDK asks for a browser authorisation: the authorisation URL is captured
            and returned so the UI can open it; a loopback listener on 127.0.0.1 waits for Clay's redirect (5 min).
status() -> idle | connecting | waiting_browser | connected | failed, with the workspace, credits and tool count
            read from Clay after a successful connection (metadata tools only: no search, no spend).
cancel() -> stops waiting.

Tokens go to the OS credential store through KeyringTokenStorage, exactly as before. Nothing here logs or returns a
token; the authorisation URL holds only the public PKCE challenge and state.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

from chanakya.clay.mcp_client import ClayMCPClient, unwrap
from chanakya.clay.oauth_callback import LoopbackCallback

META_TOOLS = {"get-current-workspace", "get-credits-available"}
_lock = threading.Lock()
_state: dict = {"state": "idle"}
_cb: "_Capturing | None" = None


class _Capturing(LoopbackCallback):
    """Captures the authorisation URL for the UI instead of opening a browser on the server side."""

    async def open_browser(self, authorization_url: str) -> None:
        self._start_server()
        with _lock:
            _state.update(state="waiting_browser", auth_url=authorization_url,
                          expires_at=time.time() + self.timeout_s)

    def abort(self) -> None:
        self._result = {"error": "cancelled", "error_description": "cancelled from the UI"}
        self._done.set()


def _public(s: dict) -> dict:
    out = {k: v for k, v in s.items() if not k.startswith("_")}
    if out.get("expires_at"):
        out["seconds_left"] = max(0, int(out["expires_at"] - time.time()))
    return out


def status() -> dict:
    with _lock:
        return _public(_state)


async def _probe(cb, timeout_s: float) -> dict:
    from chanakya.scout.agent import result_payload
    async with ClayMCPClient(callback=cb, allowed_tools=META_TOOLS, timeout_s=timeout_s) as c:
        tools = await c.list_tools()
        ws = result_payload(await c.call_tool("get-current-workspace", {}))
        cr = result_payload(await c.call_tool("get-credits-available", {}))
        return {"tools": [t.name for t in tools], "workspace": ws, "credits": cr}


def _run(cb: "_Capturing", on_done=None) -> None:
    try:
        d = asyncio.run(_probe(cb, 60))
        pinned = os.getenv("CLAY_WORKSPACE_ID", "1461095")
        ws_id = str((d["workspace"] or {}).get("workspaceId"))
        ok = ws_id == pinned
        new = {"state": "connected" if ok else "failed", "tools": d["tools"], "tool_count": len(d["tools"]),
               "workspace": d["workspace"], "credits": d["credits"], "at": time.time(),
               "error": None if ok else f"connected to workspace {ws_id}, but this installation is pinned to "
                                        f"workspace {pinned}; searches would be refused"}
    except BaseException as e:  # noqa: BLE001 - reported to the UI as the real reason
        leaf = unwrap(e)
        new = {"state": "failed", "error": f"{type(leaf).__name__}: {str(leaf)[:300]}", "at": time.time()}
    with _lock:
        _state.clear()
        _state.update(new)
    if on_done:
        try:
            on_done(_public(new))
        except Exception:  # noqa: BLE001 - recording the result must never break the flow
            pass


def start(on_done=None, *, timeout_s: float = 300) -> dict:
    """Begin (or report) a connection attempt. Returns the state; when a browser step is needed the caller polls
    status() until `auth_url` appears (normally well under two seconds)."""
    global _cb
    with _lock:
        if _state.get("state") in ("connecting", "waiting_browser"):
            return _public(_state)
        _state.clear()
        _state.update(state="connecting", started_at=time.time())
    _cb = _Capturing(timeout_s=timeout_s)
    threading.Thread(target=_run, args=(_cb, on_done), daemon=True, name="clay-connect").start()
    for _ in range(40):  # give the SDK a moment to either connect or ask for the browser
        time.sleep(0.25)
        s = status()
        if s["state"] != "connecting":
            return s
    return status()


def cancel() -> dict:
    with _lock:
        waiting = _state.get("state") == "waiting_browser"
    if waiting and _cb is not None:
        _cb.abort()
    return status()


def disconnect() -> dict:
    """Forget the stored tokens (the next connection needs a browser authorisation again)."""
    from chanakya.clay.token_store import KeyringTokenStorage
    KeyringTokenStorage().clear()
    with _lock:
        _state.clear()
        _state.update(state="idle", note="stored Clay tokens removed")
        return _public(_state)
