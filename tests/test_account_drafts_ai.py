"""Account-based drafts and model-backed agent steps (fake LLM: no network). Fixtures are synthetic."""
import json

import pytest

from chanakya.intel import ai_agents as AI
from chanakya.intel import entities as E
from chanakya.intel import proof as P
from chanakya.intel import scoring as SC
from chanakya.intel import signals as SG
from chanakya.outreach import account_drafts as AD
from chanakya.outreach.drafts import DraftError
from tests.helpers import seed_company, seed_person, seed_product


class FakeLLM:
    model = "fake-model"

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat(self, messages, tools=None, *, json_format=False, temperature=0.0, **kw):
        from chanakya.llm.routing import _Result
        self.calls.append(messages)
        text = json.dumps(self.payload(messages) if callable(self.payload) else self.payload)
        return _Result(self.model, text, {"role": "assistant", "content": text}, 1, 1, 0.0, [])


def _account(con):
    icp = seed_product(con, pid="PRD-SETU", name="Setu", summary="BI migration")
    P.seed(con)
    cid = seed_company(con, "nhsbsa.example", "NHS Example Authority", icp_id=icp)
    s = SG.store_signal(con, type="tender", summary="Market engagement for an OAC/OBIEE to Power BI migration utility",
                        direction=E.TOP, provenance="test", company={"name": "NHS Example Authority",
                                                                     "domain": "nhsbsa.example"},
                        source_url="https://tenders.example/notice/1", fits=[{"product_id": "PRD-SETU", "score": 90}])[0]
    SC.rescore_company(con, cid)
    pid = seed_person(con, cid, "Darren Example", approve=False)
    con.commit()
    return cid, pid, s


def _approve_role(con, pid):
    con.execute("UPDATE person_roles SET review_status='approved' WHERE person_id=?", (pid,))
    con.commit()


def test_account_draft_requires_human_role_approval(con):
    cid, pid, _ = _account(con)
    with pytest.raises(DraftError, match="approve this person's role"):
        AD.generate_for_person(con, pid, by="krisha")


def test_account_draft_template_is_labelled_rule_based_and_never_sent(con):
    cid, pid, _ = _account(con)
    _approve_role(con, pid)
    r = AD.generate_for_person(con, pid, by="krisha", sender="Vineet Shukla")
    assert r["sent"] is False and r["generator"].startswith("template (rule-based")
    d = con.execute("SELECT * FROM outreach_drafts WHERE id=?", (r["draft_id"],)).fetchone()
    assert d["status"] == "draft" and d["to_address"] is None
    with pytest.raises(DraftError, match="already exists"):
        AD.generate_for_person(con, pid, by="krisha")


def test_account_draft_uses_model_when_valid_and_rejects_invented_urls(con):
    cid, pid, _ = _account(con)
    _approve_role(con, pid)
    good = FakeLLM({"subject": "OBIEE to Power BI", "body": "Hi Darren,\n\nI saw the market engagement notice for an "
                    "OBIEE to Power BI migration utility.\n\nSetu moves legacy BI estates.\n\nWorth 20 minutes?",
                    "hook_ids": [1]})
    r = AD.generate_for_person(con, pid, by="krisha", llm=good, sender="Vineet Shukla")
    assert r["generator"] == "llm (fake-model)"
    body = con.execute("SELECT body FROM outreach_drafts WHERE id=?", (r["draft_id"],)).fetchone()[0]
    assert "https://wayam.ai/accelerators/setu" in body and "Vineet Shukla" in body

    con.execute("UPDATE outreach_drafts SET status='discarded'")
    bad = FakeLLM({"subject": "x", "body": "See https://evil.example/now", "hook_ids": [1]})
    r2 = AD.generate_for_person(con, pid, by="krisha", llm=bad)
    assert r2["generator"].startswith("template (rule-based; model draft rejected")


def test_rejected_evidence_is_not_used_for_drafts(con):
    cid, pid, s = _account(con)
    _approve_role(con, pid)
    SG.review_signal(con, s, "rejected", by="krisha", reason="wrong organisation")
    with pytest.raises(DraftError):
        AD.generate_for_person(con, pid, by="krisha")


def test_classifier_labels_only_known_signals_and_never_verifies(con):
    cid, pid, s = _account(con)
    llm = FakeLLM({"labels": [{"signal_id": s, "evidence_class": "strong_evidence", "reason": "tender for migration"},
                              {"signal_id": 99999, "evidence_class": "strong_evidence", "reason": "foreign"},
                              {"signal_id": s, "evidence_class": "verified", "reason": "not a class"}]})
    r = AI.classify_signals(con, cid, llm=llm)
    assert r["labelled"] == 1
    row = dict(con.execute("SELECT * FROM signals WHERE id=?", (s,)).fetchone())
    assert AI.evidence_class(row)["class"] == "strong_evidence"
    assert row["verification_status"] != "verified"


def test_analysis_must_cite_own_evidence(con):
    cid, pid, s = _account(con)
    brief = {"summary": "s", "why_now": "w", "accelerator_fit": "a", "risks": ["r"], "recommended_next_action": "n",
             "confidence": "low"}
    with pytest.raises(AI.AgentOutputError):
        AI.analyse_account(con, cid, by="krisha", llm=FakeLLM({**brief, "cited_signal_ids": [s + 1000]}))
    r = AI.analyse_account(con, cid, by="krisha", llm=FakeLLM({**brief, "cited_signal_ids": [s]}))
    assert r["model"] == "fake-model" and AI.latest_analysis(con, cid)["cited_signal_ids"] == [s]


def test_agents_report_unavailable_without_a_key(con, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cid, pid, s = _account(con)
    with pytest.raises(AI.AgentUnavailable):
        AI.classify_signals(con, cid)


@pytest.mark.parametrize("qwen", [False, True])
def test_intel_stage_runs_haiku_opus_and_coordinator_on_routed_models(con, qwen):
    from chanakya.workflow import orchestrator as W
    cid, pid, s = _account(con)
    seen = {}

    def llm_for(task):
        def payload(messages):
            if task == "signal_classification":
                return {"labels": [{"signal_id": s, "evidence_class": "strong_evidence", "reason": "tender"}]}
            if task == "complex_qualification":
                return {"summary": "s", "why_now": "w", "accelerator_fit": "a", "risks": [], "confidence": "low",
                        "recommended_next_action": "verify", "cited_signal_ids": [s]}
            return {"summary": "run", "converged_company_ids": [], "gaps": ["no people"],
                    "priorities": [{"company_id": cid, "reason": "tender", "next_human_action": "qualify"}]}
        if task == "chanakya_agent" and not qwen:
            return None  # Qwen Max not configured: the coordinator must fall back to Opus and say so
        m = FakeLLM(payload); m.model = {"signal_classification": "haiku-x", "complex_qualification": "opus-x",
                                         "multi_agent_coordination": "opus-x", "chanakya_agent": "qwen-max"
                                         }.get(task, "other")
        seen[task] = m.model
        return m
    wid = W.create_workflow(con, "top_down", {"product_id": "PRD-SETU", "verify_sources": False})
    wf = W.get_workflow(con, wid)
    ctx = W.Ctx(con, wf, W.Services(llm_for=llm_for), True)
    ai = W._intel_ai(ctx, [cid])
    assert ai["classified"][0]["labelled"] == 1 and ai["briefs"][0]["confidence"] == "low"
    assert not ai["errors"]
    if qwen:  # the intended orchestrator ran; the Opus coordination route was not called at all
        assert ai["coordination"]["model"] == "qwen-max" and ai["orchestrator"]["fallback"] is False
        assert ctx.models_used == {"signal_classification": "haiku-x", "complex_qualification": "opus-x",
                                   "chanakya_agent": "qwen-max"}
        assert con.execute("SELECT purpose FROM llm_calls WHERE agent='coordinator'").fetchone()[0] == "chanakya_agent"
    else:  # a reported fallback, never a silent one
        assert ai["coordination"]["model"] == "opus-x" and ai["orchestrator"]["fallback"] is True
        assert ctx.models_used == {"signal_classification": "haiku-x", "complex_qualification": "opus-x",
                                   "multi_agent_coordination": "opus-x"}
    # the coordinator's ranking is stored and drives the order of later stages
    import json as _json
    assert _json.loads(W.get_workflow(con, wid)["params_json"])["priority_company_ids"] == [cid]


def test_coordinator_rejects_unknown_accounts(con):
    cid, pid, s = _account(con)
    from chanakya.workflow import orchestrator as W
    wf = W.get_workflow(con, W.create_workflow(con, "top_down", {}))
    bad = FakeLLM({"summary": "x", "converged_company_ids": [cid], "gaps": [],
                   "priorities": [{"company_id": 4242, "reason": "r", "next_human_action": "n"}]})
    with pytest.raises(AI.AgentOutputError):
        AI.coordinate_run(con, wf, [cid], llm=bad)


def test_haiku_draft_check_is_stored_in_history(con):
    cid, pid, _ = _account(con)
    _approve_role(con, pid)
    did = AD.generate_for_person(con, pid, by="krisha")["draft_id"]
    r = AI.check_draft(con, did, llm=FakeLLM({"issues": [], "unsupported_claims": [], "ok": True}))
    assert r["ok"] and con.execute("SELECT COUNT(*) FROM outreach_draft_history WHERE draft_id=? AND action='ai_check'",
                                   (did,)).fetchone()[0] == 1
