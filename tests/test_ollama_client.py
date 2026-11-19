import json
import httpx
import pytest

from chanakya.config import Settings, load_settings, ConfigError
from chanakya.llm.ollama_client import OllamaClient, OllamaAuthError, OllamaError, tool_result_message


def settings(**kw):
    base = dict(ollama_api_key="sk-test-secret", ollama_model="primary-m", ollama_fallback_model="fb-m",
                ollama_host="https://ollama.test", ollama_timeout_s=5, ollama_max_retries=1,
                db_path="x.db", clay_mcp_url=None)
    base.update(kw)
    return Settings(**base)


def client(handler, **kw):
    c = OllamaClient(settings(**kw), transport=httpx.MockTransport(handler))
    return c


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("chanakya.llm.ollama_client.time.sleep", lambda s: None)


def test_key_never_in_repr_or_str():
    s = settings()
    assert "sk-test-secret" not in repr(s) and "sk-test-secret" not in str(s)


def test_missing_env(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        load_settings(env_file=None)


def test_parses_tool_calls_and_string_args():
    def h(req):
        body = json.loads(req.content)
        assert req.headers["authorization"] == "Bearer sk-test-secret"
        assert body["model"] == "primary-m" and body["tools"]
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "a", "arguments": {"x": 1}}},
            {"function": {"name": "b", "arguments": "{\"y\": 2}"}}]},
            "prompt_eval_count": 10, "eval_count": 3})
    r = client(h).chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "a"}}])
    assert [(t.name, t.arguments) for t in r.tool_calls] == [("a", {"x": 1}), ("b", {"y": 2})]
    assert r.message["tool_calls"] and r.prompt_tokens == 10


def test_retry_then_success():
    n = {"i": 0}
    def h(req):
        n["i"] += 1
        return httpx.Response(503) if n["i"] == 1 else httpx.Response(200, json={"message": {"content": "ok"}})
    assert client(h).chat([]).content == "ok" and n["i"] == 2


def test_fallback_model_after_retries_exhausted():
    seen = []
    def h(req):
        m = json.loads(req.content)["model"]; seen.append(m)
        return httpx.Response(500) if m == "primary-m" else httpx.Response(200, json={"message": {"content": "fb"}})
    r = client(h, ollama_allow_fallback=True).chat([])
    assert r.model == "fb-m" and seen == ["primary-m", "primary-m", "fb-m"]


def test_no_silent_fallback_by_default():
    seen = []
    def h(req):
        seen.append(json.loads(req.content)["model"]); return httpx.Response(500)
    with pytest.raises(OllamaError):
        client(h).chat([])
    assert seen == ["primary-m", "primary-m"]


def test_auth_error_not_retried_nor_fallback():
    calls = []
    def h(req):
        calls.append(1); return httpx.Response(401)
    with pytest.raises(OllamaAuthError):
        client(h).chat([])
    assert len(calls) == 1


def test_non_retryable_4xx():
    with pytest.raises(OllamaError):
        client(lambda r: httpx.Response(400, text="bad")).chat([], model="primary-m")


def test_tool_result_message():
    assert tool_result_message("t", {"a": 1}) == {"role": "tool", "tool_name": "t", "content": '{"a": 1}'}
