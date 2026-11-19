"""Live smoke tests: run with `pytest -m live`. Small requests only."""
import pytest
from chanakya.config import load_settings
from chanakya.llm.ollama_client import OllamaClient, tool_result_message

pytestmark = pytest.mark.live

TOOL = {"type": "function", "function": {"name": "get_company_size", "description": "Employee count for a domain",
        "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}, "required": ["domain"]}}}


@pytest.fixture(scope="module")
def llm():
    with OllamaClient(load_settings()) as c:
        yield c


def test_simple_response(llm):
    r = llm.chat([{"role": "user", "content": "Reply with exactly the word: pong"}], max_tokens=200)
    assert "pong" in r.content.lower()


def test_tool_call_round_trip(llm):
    msgs = [{"role": "user", "content": "How many employees does acme.com have? Use the tool."}]
    r = llm.chat(msgs, tools=[TOOL])
    assert r.tool_calls and r.tool_calls[0].name == "get_company_size"
    assert r.tool_calls[0].arguments.get("domain") == "acme.com"
    msgs += [r.message, tool_result_message("get_company_size", {"employee_count": 321})]
    r2 = llm.chat(msgs, tools=[TOOL])
    assert "321" in r2.content
