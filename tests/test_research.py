"""Agent 6 (Profile Researcher) - offline. Harness subprocess and LLM are fakes; data is SYNTHETIC."""
import json

import pytest

from chanakya.research import agent as R
from chanakya.resolver.agent import ResolverBlocked
from chanakya.resolver.harness_client import HarnessError, RESEARCH_COMMANDS
from tests.helpers import (POST_URL, PROFILE_URL, FakeHarness, SeqLLM, harness, ok, research_data, seed_company,
                           seed_person, seed_product)

GOOD = {"current_designation": "Chief Financial Officer",
        "responsibilities": [{"text": "Owns audit and close", "evidence": [
            {"quote": "Responsible for statutory audit, treasury and month-end close", "source_url": PROFILE_URL + "/"}]}],
        "priorities": [{"text": "Faster month-end close", "evidence": [
            {"quote": "Month-end close still takes us 9 days", "source_url": POST_URL}]}],
        "pain_points": [{"text": "Reconciliation bottleneck", "evidence": [
            {"quote": "Reconciliation is our biggest bottleneck", "source_url": POST_URL}]},
            {"text": "INVENTED", "evidence": [{"quote": "We are migrating to SAP next year", "source_url": POST_URL}]}],
        "accelerator_matches": [
            {"product_id": "PRD-TEST", "rationale": "reconciliation", "product_quote": "cuts reconciliation time",
             "person_evidence": [{"quote": "Reconciliation is our biggest bottleneck", "source_url": POST_URL}]},
            {"product_id": "PRD-NOPE", "rationale": "x", "product_quote": "x" * 10,
             "person_evidence": [{"quote": "Reconciliation is our biggest bottleneck", "source_url": POST_URL}]}],
        "summary": "Finance leader focused on faster close."}


def setup(con, linkedin=PROFILE_URL):
    icp = seed_product(con)
    cid = seed_company(con, icp_id=icp)
    return seed_person(con, cid, linkedin=linkedin)


def test_candidates_need_linkedin_and_approved_role(con):
    icp = seed_product(con)
    cid = seed_company(con, icp_id=icp)
    seed_person(con, cid, "No Url")
    seed_person(con, cid, "Not Approved", linkedin="https://www.linkedin.com/in/x-2", approve=False)
    pid = seed_person(con, cid, "Asha Testperson", linkedin=PROFILE_URL)
    assert [c.person_id for c in R.select_candidates(con)] == [pid]


def test_dry_run_starts_no_subprocess(con, tmp_path):
    setup(con)
    fh = FakeHarness({})
    rep = R.run_researcher(harness(tmp_path, fh), con, allow_live=False)
    assert rep.dry_run and fh.calls == []
    assert rep.planned[0]["argv"][3:] == ["research", PROFILE_URL, "-n", "10", "--days", "90"]


def test_extract_and_validate_drops_ungrounded(con):
    seed_product(con)
    facts = R.extract_facts(research_data())
    assert facts["current_role_lines"][0] == "Chief Financial Officer" and facts["activity"][0]["date_kind"] == "exact"
    kept, dropped = R.validate_analysis(R.ResearchAnalysis.model_validate(GOOD), facts, R.product_catalog(con))
    assert kept["current_designation"] == "Chief Financial Officer"
    assert [p["text"] for p in kept["pain_points"]] == ["Reconciliation bottleneck"]
    assert [m["product_id"] for m in kept["accelerator_matches"]] == ["PRD-TEST"]
    whys = {d["field"]: d["why"] for d in dropped}
    assert "pain_points" in whys and whys["accelerator_matches"] == "unknown product"
    assert R.confidence_of(facts, kept) == "high"


def test_source_url_must_come_from_harness(con):
    seed_product(con)
    facts = R.extract_facts(research_data())
    bad = {"priorities": [{"text": "x", "evidence": [{"quote": "Month-end close still takes us 9 days",
                                                       "source_url": "https://evil.example/post"}]}]}
    kept, dropped = R.validate_analysis(R.ResearchAnalysis.model_validate(bad), facts, [])
    assert kept["priorities"] == [] and dropped


def test_live_research_with_llm(con, tmp_path):
    pid = setup(con)
    fh = FakeHarness({"research": ok(research_data())})
    llm = SeqLLM(GOOD)
    rep = R.run_researcher(harness(tmp_path, fh), con, llm=llm, allow_live=True, lock_path=tmp_path / "l")
    assert rep.counts == {"researched": 1, "blocked": 0, "error": 0}
    assert [c[3] for c in fh.calls] == ["status", "research"]
    r = con.execute("SELECT * FROM profile_research").fetchone()
    assert (r["person_id"], r["status"], r["confidence"], r["review_status"], r["model"]) == \
        (pid, "researched", "high", "pending_review", "qwen-test")
    assert json.loads(r["harness_json"])["ok"] is True and r["harness_saved_path"] == "outputs/x.json"
    assert len(json.loads(r["dropped_json"])) == 2
    assert con.execute("SELECT ok, agent FROM llm_calls").fetchone()[:] == (1, "profile_researcher")
    assert R.select_candidates(con) == []


def test_without_llm_facts_only_low_confidence(con, tmp_path):
    setup(con)
    R.run_researcher(harness(tmp_path, FakeHarness({"research": ok(research_data())})), con, allow_live=True,
                     lock_path=tmp_path / "l")
    r = con.execute("SELECT * FROM profile_research").fetchone()
    assert r["analysis_json"] is None and r["confidence"] == "low" and json.loads(r["facts_json"])["name"]


def test_blocked_access_stores_no_research_and_stops(con, tmp_path):
    setup(con)
    blocked = {"ok": False, "error": "LinkedInBlocked: Security checkpoint / CAPTCHA at https://www.linkedin.com/x"}
    rep = R.run_researcher(harness(tmp_path, FakeHarness({"research": blocked})), con, llm=SeqLLM(),
                           allow_live=True, lock_path=tmp_path / "l")
    assert rep.stopped == "security_check"
    r = con.execute("SELECT * FROM profile_research").fetchone()
    assert r["status"] == "blocked" and r["facts_json"] is None and r["analysis_json"] is None
    with pytest.raises(R.ResearchReviewError):
        R.review(con, r["id"], "approved", by="k", reason="r")
    assert len(R.select_candidates(con)) == 1  # blocked rows do not count as research


def test_not_logged_in_refuses_before_research(con, tmp_path):
    setup(con)
    fh = FakeHarness({}, logged_in=False)
    with pytest.raises(ResolverBlocked):
        R.run_researcher(harness(tmp_path, fh), con, allow_live=True, lock_path=tmp_path / "l")
    assert [c[3] for c in fh.calls] == ["status"]


def test_page_budget_checked_before_any_call(con, tmp_path):
    setup(con)
    (tmp_path / "outputs").mkdir()
    import datetime as dt
    (tmp_path / "outputs" / "usage.json").write_text(json.dumps({dt.date.today().isoformat(): 199}))
    fh = FakeHarness({})
    with pytest.raises(HarnessError) as e:
        R.run_researcher(harness(tmp_path, fh), con, allow_live=True, lock_path=tmp_path / "l")
    assert e.value.kind == "page_limit" and fh.calls == []


def test_research_client_cannot_resolve_or_run(tmp_path):
    c = harness(tmp_path, FakeHarness({})).with_commands(RESEARCH_COMMANDS)
    for cmd in ("resolve", "people", "run"):
        with pytest.raises(HarnessError):
            c.build_argv(cmd, "x")


def test_review_history(con, tmp_path):
    setup(con)
    R.run_researcher(harness(tmp_path, FakeHarness({"research": ok(research_data())})), con, llm=SeqLLM(GOOD),
                     allow_live=True, lock_path=tmp_path / "l")
    rid = con.execute("SELECT id FROM profile_research").fetchone()[0]
    with pytest.raises(R.ResearchReviewError):
        R.review(con, rid, "approved", by="k", reason="")
    R.review(con, rid, "approved", by="krisha", reason="evidence checked")
    assert [h["action"] for h in R.history(con, rid)] == ["create", "approve"]
    assert R.approved_research(con, con.execute("SELECT person_id FROM profile_research").fetchone()[0])["id"] == rid
