"""Clay MCP client error surfacing: SDK TaskGroup ExceptionGroups must not hide the real cause."""
import asyncio

import pytest

from chanakya.clay import mcp_client as m


def _raise_from_callback():
    async def open_browser(url):
        raise RuntimeError("no browser")
    try:
        asyncio.run(open_browser("x"))
    except RuntimeError as e:
        return e


def test_root_causes_flattens_nested_groups():
    a, b = ValueError("a"), KeyError("b")
    g = BaseExceptionGroup("outer", [ExceptionGroup("inner", [a]), b])
    assert m.root_causes(g) == [a, b]
    assert m.unwrap(g) is a


def test_unwrap_plain_exception_is_identity():
    e = RuntimeError("x")
    assert m.unwrap(e) is e


def test_callback_failure_is_reauth(monkeypatch):
    c = m.ClayMCPClient.__new__(m.ClayMCPClient)
    assert c._is_auth_failure(_raise_from_callback()) is True
    assert c._is_auth_failure(ValueError("unrelated")) is False


def test_aenter_converts_taskgroup_auth_error(monkeypatch):
    leaf = _raise_from_callback()

    class Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            raise ExceptionGroup("unhandled errors in a TaskGroup", [leaf])

    def fake_http(auth=None):
        return Boom()

    def fake_stream(url, http_client=None):
        class S:
            async def __aenter__(self):
                raise asyncio.CancelledError()

            async def __aexit__(self, *a):
                return False
        return S()

    monkeypatch.setattr(m, "create_mcp_http_client", fake_http)
    monkeypatch.setattr(m, "streamable_http_client", fake_stream)
    monkeypatch.setattr(m.ClayMCPClient, "_auth", lambda self: None)

    class CB:
        redirect_uri = "http://127.0.0.1:1/cb"

    async def go():
        async with m.ClayMCPClient(storage=object(), callback=CB()):
            pass

    with pytest.raises(m.ClayReauthRequired):
        asyncio.run(go())
