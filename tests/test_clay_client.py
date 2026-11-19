import asyncio
import threading
import urllib.request

import keyring
import pytest
from keyring.backend import KeyringBackend
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from chanakya.clay.mcp_client import ClayMCPClient, ToolNotAllowed
from chanakya.clay.oauth_callback import LoopbackCallback, OAuthCallbackError
from chanakya.clay.token_store import KeyringTokenStorage


class MemKeyring(KeyringBackend):
    priority = 1
    def __init__(self): self.d = {}
    def get_password(self, s, u): return self.d.get((s, u))
    def set_password(self, s, u, p): self.d[(s, u)] = p
    def delete_password(self, s, u): self.d.pop((s, u), None)


@pytest.fixture
def mem():
    prev = keyring.get_keyring(); kr = MemKeyring(); keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(prev)


def test_token_roundtrip_and_rotation(mem):
    s = KeyringTokenStorage(service="test")
    async def go():
        assert await s.get_tokens() is None
        await s.set_tokens(OAuthToken(access_token="a1", refresh_token="r1", expires_in=60))
        await s.set_tokens(OAuthToken(access_token="a2", refresh_token="r2", expires_in=60))  # rotated
        t = await s.get_tokens()
        assert t.access_token == "a2" and t.refresh_token == "r2"
        await s.set_client_info(OAuthClientInformationFull(client_id="cid", redirect_uris=["http://127.0.0.1:1/cb"]))
        assert (await s.get_client_info()).client_id == "cid"
        s.clear()
        assert await s.get_tokens() is None and (await s.get_client_info()).client_id == "cid"
    asyncio.run(go())


def _hit(url):
    try: urllib.request.urlopen(url, timeout=5).read()
    except Exception: pass


def test_loopback_captures_code(monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda u: True)
    cb = LoopbackCallback(port=33499)
    async def go():
        await cb.open_browser("https://auth.example/authorize")
        threading.Timer(0.2, _hit, [cb.redirect_uri + "?code=abc&state=xyz&iss=https%3A%2F%2Fi"]).start()
        return await cb.wait_for_code()
    r = asyncio.run(go())
    assert (r.code, r.state, r.iss) == ("abc", "xyz", "https://i")


def test_loopback_denied(monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda u: True)
    cb = LoopbackCallback(port=33498)
    async def go():
        await cb.open_browser("x")
        threading.Timer(0.2, _hit, [cb.redirect_uri + "?error=access_denied"]).start()
        return await cb.wait_for_code()
    with pytest.raises(OAuthCallbackError):
        asyncio.run(go())


def test_call_tool_blocked_by_default():
    c = ClayMCPClient(storage=KeyringTokenStorage(service="unused"))
    with pytest.raises(ToolNotAllowed):
        asyncio.run(c.call_tool("search-companies", {}))
