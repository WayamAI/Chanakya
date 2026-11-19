"""Model routing, the Wayam content catalogue and the Outlook draft bridge (fake MCP session: no mailbox touched)."""
import inspect
import json

import httpx
import pytest

from chanakya.intel import content as CT
from chanakya.llm import routing as R
from chanakya.outreach import drafts as D
from chanakya.outreach import outlook as O


# ---------------- routing ----------------
def test_tasks_route_to_intended_tiers(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "CHANAKYA_MODEL_OPUS", "CHANAKYA_ROUTE_OUTREACH_DRAFT"):
        monkeypatch.delenv(k, raising=False)
    assert R.route("multi_agent_coordination").model == "claude-opus-5-5"
    assert R.route("outreach_draft").model == "claude-sonnet-5-5"
    assert R.route("signal_classification").model == "claude-haiku-5-5"
    assert R.route("chanakya_agent").provider == "dashscope" and R.route("chanakya_agent").model == "qwen-max"
    assert not R.route("outreach_draft").configured  # no key -> not configured, no silent substitute
    assert R.client_for("outreach_draft") is None


def test_routing_is_configurable_from_env(monkeypatch):
    monkeypatch.setenv("CHANAKYA_ROUTE_OUTREACH_DRAFT", "haiku")
    monkeypatch.setenv("CHANAKYA_MODEL_HAIKU", "claude-haiku-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    r = R.route("outreach_draft")
    assert (r.tier, r.model, r.configured) == ("haiku", "claude-haiku-x", True)
    monkeypatch.setenv("CHANAKYA_ROUTE_OUTREACH_DRAFT", "gpt")
    with pytest.raises(ValueError):
        R.route("outreach_draft")


def test_anthropic_client_speaks_messages_api_and_feeds_structured(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["key"] = req.headers.get("x-api-key")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"model": "claude-sonnet-5-5", "usage": {"input_tokens": 3, "output_tokens": 4},
                                         "content": [{"type": "text", "text": 'Here: {"subject":"s","body":"b",'
                                                                              '"hook_ids":[1]}'}]})
    c = R.AnthropicClient("claude-sonnet-5-5", api_key="k", transport=httpx.MockTransport(handler))
    from chanakya.llm.structured import ask_structured
    out = ask_structured(c, D.DraftOut, system="sys", user="u", agent="t", purpose="p")
    assert out.hook_ids == [1] and seen["key"] == "k"
    assert seen["body"]["model"] == "claude-sonnet-5-5" and "sys" in seen["body"]["system"][0]["text"]
    assert "temperature" not in seen["body"]  # current models reject it
    assert all(m["role"] != "system" for m in seen["body"]["messages"])


def test_health_separates_configured_available_tested(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "qwen-max")
    monkeypatch.setenv("OLLAMA_API_KEY", "")
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"models": [{"name": "gpt-oss:120b"}]}))
    h = R.health(transport=t)
    a, o = h["providers"]["anthropic"], h["providers"]["ollama"]
    assert a["configured"] is False and "ANTHROPIC_API_KEY" in a["error"]
    assert o["configured"] is False and o["available"] is False and o["intended_available"] is False
    assert all(m["tested"] is False for m in a["models"].values())


# ---------------- content catalogue ----------------
def test_content_catalogue_loads_and_is_used_by_drafts(con, tmp_path):
    c = CT.load()
    assert c["company"]["name"] == "Wayam AI" and c["signature"]["lines"][0] == "Vineet Shukla"
    from tests.test_qa_outreach import approved_prospect
    approved_prospect(con, tmp_path)
    [did] = D.generate(con, sender="[Your name]")
    body = con.execute("SELECT body FROM outreach_drafts WHERE id=?", (did,)).fetchone()[0]
    assert c["paragraphs"]["ask"] in body and body.endswith("\n".join(c["signature"]["lines"]))


def test_content_rejects_voice_breaks_and_false_proof(tmp_path, monkeypatch):
    monkeypatch.setenv("CHANAKYA_CONTENT_PATH", str(tmp_path / "c.yaml"))
    import yaml
    data = yaml.safe_load(CT.DEFAULT_PATH.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(data))
    bad["paragraphs"]["ask"] = "We leverage synergy — amazing!"
    with pytest.raises(CT.ContentError):
        CT.save(bad)
    bad = json.loads(json.dumps(data))
    bad["paragraphs"]["what_we_do"] = "We migrated a Cognos estate for a bank, a proven case study."
    with pytest.raises(CT.ContentError, match="(?i)cognos"):
        CT.save(bad)
    CT.save(data)
    assert CT.load()["company"]["website"] == "https://wayam.ai"


# ---------------- Outlook ----------------
class FakeSession:
    calls: list = []

    def __init__(self, me="tester@contoso.com", draft=None):
        self.me, self.draft = me, draft or {"id": "AAMk-1", "webLink": "https://outlook/1", "isDraft": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        pass

    async def list_tools(self):
        return ["get-current-user", "create-draft-email"]

    async def call(self, tool, args):
        FakeSession.calls.append((tool, args))
        if tool == "get-current-user":
            return {"displayName": "Tester", "mail": self.me}
        if tool == "get-mail-message":  # what the mailbox stored: echo the created message
            m = FakeSession.calls[-2][1]["body"]
            return {**self.draft, "subject": m["subject"], "toRecipients": m["toRecipients"],
                    "body": {"contentType": "html", "content": "<p>" + m["body"]["content"] + "</p>"}}
        return self.draft


def _ready_draft(con, tmp_path, to="ceo@realcompany.com"):
    from tests.test_qa_outreach import approved_prospect
    approved_prospect(con, tmp_path)
    [did] = D.generate(con, sender="Vineet")
    D.edit(con, did, by="k", reason="personalised", subject="Month-end close",
           body="Hi Asha,\n\nI saw your close takes ten days.\nWorth 20 minutes next week?\n\nVineet")
    con.execute("UPDATE outreach_drafts SET to_address=? WHERE id=?", (to, did))
    con.commit()
    return did


def test_outlook_never_sends_and_has_no_send_path():
    src = inspect.getsource(O)
    assert "send-draft-message" not in src and "Mail.Send\"" not in src and "sendMail" not in src
    assert O.DRAFT_TOOLS == "^(get-current-user|create-draft-email|get-mail-message)$"  # read-back is read-only


def test_outlook_draft_created_in_test_mode_without_real_recipient(con, tmp_path, monkeypatch):
    monkeypatch.setenv("CHANAKYA_OUTLOOK_TEST_MODE", "1")
    FakeSession.calls = []
    did = _ready_draft(con, tmp_path)
    r = O.create_outlook_draft(con, did, by="Krisha", session_factory=FakeSession)
    assert r["sent"] is False and r["recipient"] is None and "test mode" in r["note"]
    tool, args = FakeSession.calls[-2]
    assert tool == "create-draft-email" and args["body"]["toRecipients"] == []
    assert FakeSession.calls[-1][0] == "get-mail-message" and r["verified_in_drafts"] is True
    assert all(r["readback"].values())
    bad = O.readback_check({"id": "AAMk-1", "isDraft": True, "subject": "changed", "toRecipients": []}, "AAMk-1",
                           None, "orig", "b")
    assert bad["subject"] is False
    row = con.execute("SELECT outlook_message_id, status FROM outreach_drafts WHERE id=?", (did,)).fetchone()
    assert row[0] == "AAMk-1" and row[1] == "saved_to_outlook"
    assert [r[0] for r in con.execute("SELECT action FROM outreach_draft_history WHERE draft_id=? ORDER BY id DESC "
                                      "LIMIT 2", (did,))] == ["saved_to_outlook", "outlook_draft"]
    with pytest.raises(O.OutlookError, match="already in Outlook"):
        O.create_outlook_draft(con, did, by="Krisha", session_factory=FakeSession)


def test_outlook_fills_allowlisted_recipient(con, tmp_path, monkeypatch):
    monkeypatch.setenv("CHANAKYA_OUTLOOK_TEST_MODE", "1")
    did = _ready_draft(con, tmp_path, to="dummy@example.com")
    r = O.create_outlook_draft(con, did, by="Krisha", session_factory=FakeSession)
    assert r["recipient"] == "dummy@example.com"


def test_outlook_refuses_unreviewed_or_unsafe_drafts(con, tmp_path):
    from tests.test_qa_outreach import approved_prospect
    approved_prospect(con, tmp_path)
    [did] = D.generate(con, sender="Vineet")
    with pytest.raises(O.OutlookError, match="must edit or mark it ready"):
        O.create_outlook_draft(con, did, by="k", session_factory=FakeSession)
    D.edit(con, did, by="k", reason="r", body="Hi, [PERSONALISE: x]")
    with pytest.raises(O.OutlookError, match="PERSONALISE"):
        O.create_outlook_draft(con, did, by="k", session_factory=FakeSession)


def test_outlook_status_reports_unauthenticated_clearly():
    s = O.status(session_factory=FakeSession, verify=lambda: {"success": False, "message": "No valid token found"})
    assert s["authenticated"] is False and s["error"] == "No valid token found"
    assert s["send_permission"] is False and s["permissions"] == ["User.Read", "Mail.ReadWrite"]
    assert "--login" in s["login_command"]


def test_chanakya_agent_routes_to_qwen_max_and_never_substitutes(monkeypatch):
    import httpx
    from chanakya.llm import routing
    from chanakya.workflow.services import make_llm
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("CHANAKYA_ROUTE_CHANAKYA_AGENT", raising=False)
    r = routing.route("chanakya_agent")
    assert (r.provider, r.model, r.configured) == ("dashscope", "qwen-max", False)
    assert make_llm(task="chanakya_agent") is None  # not Sonnet, not Ollama
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    seen = {}

    def h(req):
        seen["body"] = __import__("json").loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"model": "qwen-max", "choices": [{"message": {"content": "OK"}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    c = routing.QwenClient("qwen-max", transport=httpx.MockTransport(h))
    out = c.chat([{"role": "user", "content": "hi"}], max_tokens=5)
    assert out.model == "qwen-max" and out.content == "OK" and seen["body"]["model"] == "qwen-max"
