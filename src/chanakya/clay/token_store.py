"""OAuth client-info + token persistence in the OS credential store (Windows Credential Manager).

Implements mcp.client.auth.TokenStorage. The SDK calls set_tokens() after every token
exchange and refresh, so rotated refresh tokens are persisted automatically.
Nothing here ever logs secret values.
"""
from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SERVICE = "chanakya-clay-mcp"


class KeyringTokenStorage:
    def __init__(self, account: str = "default", service: str = SERVICE):
        self._service = service
        self._tok_key = f"{account}:tokens"
        self._cli_key = f"{account}:client_info"

    async def get_tokens(self) -> OAuthToken | None:
        raw = keyring.get_password(self._service, self._tok_key)
        return OAuthToken.model_validate_json(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        keyring.set_password(self._service, self._tok_key, tokens.model_dump_json(exclude_none=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = keyring.get_password(self._service, self._cli_key)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        keyring.set_password(self._service, self._cli_key, client_info.model_dump_json(exclude_none=True))

    def clear(self, *, include_client: bool = False) -> None:
        """Forget tokens (forces re-auth). Optionally forget the DCR registration too."""
        keys = [self._tok_key] + ([self._cli_key] if include_client else [])
        for k in keys:
            try:
                keyring.delete_password(self._service, k)
            except PasswordDeleteError:
                pass
