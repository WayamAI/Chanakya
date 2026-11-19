"""Shared layer: structured LLM calls (validation, retries, usage log) and harness adapter extensions. Offline."""
import datetime as dt
import json

import pytest
from pydantic import BaseModel

from chanakya.llm.structured import LLMOutputError, ask_structured, grounded
from chanakya.resolver.harness_client import BOTTOMUP_COMMANDS, NEVER_ALLOWED, HarnessClient, HarnessError
from tests.helpers import FakeHarness, SeqLLM, harness


class Out(BaseModel):
    answer: str
    score: int


def test_structured_ok_and_logged(con):
    o = ask_structured(SeqLLM({"answer": "x", "score": 2}), Out, system="s", user="u", agent="a", purpose="p", con=con)
    assert o.score == 2
    r = con.execute("SELECT * FROM llm_calls").fetchone()
    assert (r["ok"], r["attempts"], r["model"], r["prompt_tokens"]) == (1, 1, "qwen-test", 10)


def test_structured_repairs_then_fails(con):
    llm = SeqLLM("not json", {"answer": "x", "score": 1})
    assert ask_structured(llm, Out, system="s", user="u", agent="a", purpose="p", con=con).answer == "x"
    assert "Invalid output" in llm.calls[1][-1]["content"]
    with pytest.raises(LLMOutputError):
        ask_structured(SeqLLM({"answer": 1}, {"nope": 1}), Out, system="s", user="u", agent="a", purpose="p", con=con)
    assert con.execute("SELECT ok, attempts FROM llm_calls ORDER BY id DESC").fetchone()[:] == (0, 2)


def test_structured_transport_error_logged_and_raised(con):
    class Boom:
        def chat(self, *a, **k):
            raise RuntimeError("HTTP 503")
    with pytest.raises(RuntimeError):
        ask_structured(Boom(), Out, system="s", user="u", agent="a", purpose="p", con=con)
    assert "503" in con.execute("SELECT error FROM llm_calls").fetchone()[0]


def test_grounded():
    assert grounded("Month-end  close", "our MONTH-END close takes 9 days")
    assert not grounded("short", "short text") and not grounded("invented quote here", "other text")


def test_harness_flags_budget_and_browser_mode(tmp_path):
    h = harness(tmp_path, FakeHarness({}), allowed_commands=BOTTOMUP_COMMANDS)
    assert h.search_argv("people", "CFO", 3)[-2:] == ["-n", "3"]
    assert h.browser_mode() == "launch-per-command"
    (tmp_path / ".browser.json").write_text("{}")
    assert h.browser_mode() == "attached"
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "usage.json").write_text(json.dumps({dt.date.today().isoformat(): 150}))
    assert h.budget()["remaining"] == 50
    h.ensure_budget("people", 10)
    with pytest.raises(HarnessError):
        h.ensure_budget("people", 30)


def test_write_commands_can_never_be_allowed(tmp_path):
    with pytest.raises(AssertionError):
        HarnessClient(tmp_path, allowed_commands=frozenset({"run", "status"}))
    assert {"run", "chat", "login", "batch", "nav"} <= NEVER_ALLOWED


def test_harness_cmd_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LINKEDIN_HARNESS_DIR", str(tmp_path))
    monkeypatch.setenv("LINKEDIN_HARNESS_CMD", ".venv/Scripts/python linkedin_harness.py")
    assert HarnessClient.from_env().command_prefix == [".venv/Scripts/python", "linkedin_harness.py"]
