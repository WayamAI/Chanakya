"""Browser authorisation + loopback redirect capture for native/public OAuth clients (RFC 8252)."""
from __future__ import annotations

import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import anyio
from mcp.shared.auth import AuthorizationCodeResult

log = logging.getLogger(__name__)

_OK_PAGE = b"<html><body><h3>Chanakya: Clay authorisation complete. You can close this tab.</h3></body></html>"
_ERR_PAGE = b"<html><body><h3>Chanakya: authorisation failed. Check the terminal.</h3></body></html>"


class OAuthCallbackError(RuntimeError):
    pass


class LoopbackCallback:
    """Listens on 127.0.0.1:<port><path> for exactly one OAuth redirect."""

    def __init__(self, port: int = 33418, path: str = "/callback", timeout_s: float = 300):
        self.port, self.path, self.timeout_s = port, path, timeout_s
        self._result: dict | None = None
        self._done = threading.Event()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path}"

    async def open_browser(self, authorization_url: str) -> None:
        """redirect_handler for OAuthClientProvider."""
        self._start_server()
        log.info("Opening browser for Clay authorisation...")
        print("If the browser does not open, visit the Clay authorisation URL shown by your browser prompt.")
        if not webbrowser.open(authorization_url):
            # URL contains only public PKCE challenge + state, no secrets; safe to show for manual paste.
            print(f"Open this URL to authorise: {authorization_url}")

    async def wait_for_code(self) -> AuthorizationCodeResult:
        """callback_handler for OAuthClientProvider."""
        got = await anyio.to_thread.run_sync(self._done.wait, self.timeout_s)
        self._stop_server()
        if not got or self._result is None:
            raise OAuthCallbackError("timed out waiting for browser authorisation")
        q = self._result
        if "error" in q:
            raise OAuthCallbackError(f"authorisation denied: {q.get('error')} {q.get('error_description', '')}")
        if "code" not in q:
            raise OAuthCallbackError("callback missing authorisation code")
        return AuthorizationCodeResult(code=q["code"], state=q.get("state"), iss=q.get("iss"))

    # ---- server ------------------------------------------------------------
    def _start_server(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                if u.path != outer.path:
                    self.send_response(404); self.end_headers(); return
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                outer._result = q
                self.send_response(200 if "code" in q else 400)
                self.send_header("Content-Type", "text/html"); self.end_headers()
                self.wfile.write(_OK_PAGE if "code" in q else _ERR_PAGE)
                outer._done.set()

            def log_message(self, *a):  # keep auth codes out of logs
                pass

        self._done.clear(); self._result = None
        self._srv = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def _stop_server(self) -> None:
        srv = getattr(self, "_srv", None)
        if srv:
            srv.shutdown(); srv.server_close(); self._srv = None
