"""Independent Clay MCP client: OAuth 2.1 (DCR + PKCE, public client) over Streamable HTTP.

Tools are discovered dynamically; nothing here assumes Clay tool names or parameters.
call_tool() is guarded by an explicit allowlist so discovery can never spend credits.
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, types
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.auth import OAuthClientMetadata

from chanakya.clay.oauth_callback import LoopbackCallback
from chanakya.clay.token_store import KeyringTokenStorage

log = logging.getLogger(__name__)

CLAY_MCP_URL = "https://api.clay.com/v3/mcp"


class ToolNotAllowed(PermissionError):
    pass


class ClayReauthRequired(RuntimeError):
    """Stored OAuth token is missing/expired and could not be refreshed; run scripts/clay_discover.py."""


def root_causes(exc: BaseException) -> list[BaseException]:
    """Flatten (nested) ExceptionGroups raised by the MCP SDK's anyio TaskGroups into leaf exceptions."""
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for sub in exc.exceptions:
            out.extend(root_causes(sub))
        return out
    return [exc]


def unwrap(exc: BaseException) -> BaseException:
    """Return the most informative leaf exception (the first non-cancellation one)."""
    leaves = root_causes(exc)
    for leaf in leaves:
        if not isinstance(leaf, (KeyboardInterrupt, SystemExit)) and type(leaf).__name__ != "CancelledError":
            return leaf
    return leaves[0] if leaves else exc


@dataclass
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] | None


class ClayMCPClient:
    def __init__(self, url: str = CLAY_MCP_URL, *, storage: KeyringTokenStorage | None = None,
                 callback: LoopbackCallback | None = None, allowed_tools: set[str] | None = None,
                 timeout_s: float = 60):
        self.url = url
        self.storage = storage or KeyringTokenStorage()
        self.callback = callback or LoopbackCallback()
        self.allowed_tools = set(allowed_tools or ())  # empty = no tool may be called
        self.timeout_s = timeout_s
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.server_info: Any = None

    def _auth(self) -> OAuthClientProvider:
        metadata = OAuthClientMetadata(
            client_name="Chanakya Catalog Agent (local)",
            redirect_uris=[self.callback.redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",  # public client; PKCE is enforced by the SDK
            application_type="native",
        )
        return OAuthClientProvider(
            server_url=self.url, client_metadata=metadata, storage=self.storage,
            redirect_handler=self.callback.open_browser, callback_handler=self.callback.wait_for_code,
        )

    async def __aenter__(self) -> "ClayMCPClient":
        self._stack = AsyncExitStack()
        try:
            http = await self._stack.enter_async_context(create_mcp_http_client(auth=self._auth()))
            read, write = await self._stack.enter_async_context(streamable_http_client(self.url, http_client=http))
            self.session = await self._stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=self.timeout_s))
            init = await self.session.initialize()
            self.server_info = init
            log.info("Clay MCP session initialised (protocol %s)", getattr(init, "protocol_version", "?"))
        except BaseException as exc:
            try:
                await self._stack.aclose()
            except BaseException as close_exc:  # the SDK's TaskGroup re-raises the real failure on close
                if isinstance(close_exc, (KeyboardInterrupt, SystemExit)):
                    raise
                exc = close_exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            leaf = unwrap(exc)
            if self._is_auth_failure(leaf):
                raise ClayReauthRequired(
                    "Clay OAuth token is expired or missing and could not be refreshed "
                    f"({type(leaf).__name__}: {leaf}); "
                    "re-authorise in the UI (Integrations > Clay > Connect) or with: python scripts/clay_discover.py") from leaf
            if leaf is not exc:
                raise leaf from exc
            raise
        return self

    def _is_auth_failure(self, leaf: BaseException) -> bool:
        # The SDK only invokes the redirect/callback handler when a refresh is impossible, so a failure raised
        # from our callback (or an explicit 401) means interactive re-authorisation is needed.
        tb, names = leaf.__traceback__, set()
        while tb is not None:
            names.add(tb.tb_frame.f_code.co_name)
            tb = tb.tb_next
        if names & {"open_browser", "wait_for_code", "_perform_authorization_code_grant"}:
            return True
        status = getattr(getattr(leaf, "response", None), "status_code", None)
        return status == 401

    async def __aexit__(self, *exc) -> None:
        if self._stack:
            await self._stack.aclose()

    async def list_tools(self) -> list[ToolInfo]:
        assert self.session, "not connected"
        tools, cursor = [], None
        while True:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            res = await self.session.list_tools(params=params)
            for t in res.tools:
                ann = t.annotations.model_dump(exclude_none=True) if getattr(t, "annotations", None) else None
                schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
                tools.append(ToolInfo(t.name, t.description or "", schema, ann))
            cursor = getattr(res, "next_cursor", None) or getattr(res, "nextCursor", None)
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        if name not in self.allowed_tools:
            raise ToolNotAllowed(f"Clay tool '{name}' is not in the allowlist")
        assert self.session, "not connected"
        return await self.session.call_tool(name, arguments)
