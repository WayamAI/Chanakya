"""Bottom-up discovery - offline. Harness and LLM are fakes; data is SYNTHETIC."""
import json

import pytest

from chanakya.bottomup import agent as B
from chanakya.resolver.harness_client import BOTTOMUP_COMMANDS, HarnessError
from tests.helpers import (FakeHarness, SeqLLM, company_about_data, company_search_data, harness, ok,
                           people_search_data, profile_data, seed_company, seed_product)

RAVI = "https://www.linkedin.com/in/synthetic-ravi-002"


def fh_full():
    return FakeHarness({"people": ok(people_search_data()), "profile": ok(profile_data()),
                        "companies": ok(company_search_data()), "company": ok(company_about_data())})


def test_queries_come_only_from_approved_personas(con):
    assert B.plan_queries(con) == []
    seed_product(con)
    assert B.plan_queries(con) == ["Chief Financial Officer", "Head of Finance"]


def test_client_least_privilege(tmp_path):
    c = harness(tmp_path, FakeHarness({})).with_commands(BOTTOMUP_COMMANDS)
    for bad in ("resolve", "research", "run", "chat"):
        with pytest.raises(HarnessError):
            c.build_argv(bad, "x")
    assert c.search_argv("people", "Head of Finance", 5)[3:] == ["people", "Head of Finance", "-n", "5"]


def test_search_dry_run_and_live(con, tmp_path):
    fh = fh_full()
    h = harness(tmp_path, fh)
    rep = B.search_people(h, con, "Head of Finance")
    assert rep.dry_run and fh.calls == []
    rep = B.search_people(h, con, "Head of Finance", allow_live=True, lock_path=tmp_path / "l")
    assert rep.counts == {"new": 2, "already_known": 0}
    assert [c[3] for c in fh.calls] == ["status", "people"]
    rep2 = B.search_people(h, con, "Head of Finance", allow_live=True, lock_path=tmp_path / "l")
    assert rep2.counts == {"new": 0, "already_known": 2}  # deduped by profile URL


def test_full_people_first_chain(con, tmp_path):
    seed_product(con)
    h = harness(tmp_path, fh_full())
    lk = tmp_path / "l"
    B.search_people(h, con, "Head of Finance", allow_live=True, lock_path=lk)
    rel = B.assess_relevance(con)
    assert rel.counts == {"relevant": 1, "not_relevant": 0, "unclear": 1}  # designer has no persona title
    assert con.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0  # still no company: people first
    B.profile_leads(h, con, allow_live=True, lock_path=lk)
    lead = con.execute("SELECT * FROM bottomup_leads WHERE profile_url=?", (RAVI,)).fetchone()
    assert (lead["status"], lead["company_observed"]) == ("company_identified", "Beta Foods Pvt Ltd")
    B.verify_companies(h, con, allow_live=True, lock_path=lk)
    lead = con.execute("SELECT * FROM bottomup_leads WHERE profile_url=?", (RAVI,)).fetchone()
    assert (lead["status"], lead["company_domain"]) == ("linked", "betafoods.example")
    comp = con.execute("SELECT * FROM companies WHERE id=?", (lead["company_id"],)).fetchone()
    assert comp["qualification_status"] == "needs_manual_review"  # a human must still qualify it
    assert con.execute("SELECT source FROM company_sources WHERE company_id=?", (comp["id"],)).fetchone()[0] == \
        "linkedin-harness"
    person = con.execute("SELECT * FROM people WHERE id=?", (lead["person_id"],)).fetchone()
    assert person["linkedin_url"] == RAVI and person["verification"] == "unverified"
    assert con.execute("SELECT review_status FROM person_roles WHERE person_id=?",
                       (person["id"],)).fetchone()[0] == "needs_manual_review"
    m = B.match_accelerators(con)
    assert m.counts == {"matched": 1}
    matches = json.loads(con.execute("SELECT matches_json FROM bottomup_leads WHERE id=?",
                                     (lead["id"],)).fetchone()[0])["matches"]
    assert matches[0]["product_id"] == "PRD-TEST" and matches[0]["method"] == "approved_persona_title"
    hist = [r[0] for r in con.execute("SELECT to_status FROM bottomup_lead_history WHERE lead_id=? ORDER BY id",
                                      (lead["id"],))]
    assert hist == ["found", "relevant", "company_identified", "company_verified", "linked"]


def test_existing_company_keeps_its_icp(con, tmp_path):
    icp = seed_product(con)
    seed_company(con, "betafoods.example", "Beta Foods", icp_id=icp, qualify=False)
    h = harness(tmp_path, fh_full())
    B.search_people(h, con, "Head of Finance", allow_live=True, lock_path=tmp_path / "l")
    B.assess_relevance(con)
    B.profile_leads(h, con, allow_live=True, lock_path=tmp_path / "l")
    B.verify_companies(h, con, allow_live=True, lock_path=tmp_path / "l")
    assert con.execute("SELECT icp_id FROM companies WHERE domain='betafoods.example'").fetchone()[0] == icp


def test_llm_relevance_must_quote_lead_text(con, tmp_path):
    h = harness(tmp_path, fh_full())
    B.search_people(h, con, "Head of Finance", allow_live=True, lock_path=tmp_path / "l")
    llm = SeqLLM({"relevant": True, "role_quote": "Head of Finance", "reason": "finance"},
                 {"relevant": True, "role_quote": "Chief Data Officer", "reason": "made up"})
    rep = B.assess_relevance(con, llm=llm)  # no approved personas -> LLM decides, grounded
    assert rep.counts == {"relevant": 1, "not_relevant": 0, "unclear": 1}


def test_unverified_company_is_kept_and_human_can_link(con, tmp_path):
    seed_product(con)
    fh = FakeHarness({"people": ok(people_search_data()), "profile": ok(profile_data()),
                      "companies": ok({"results": [{"url": "https://www.linkedin.com/company/zeta/", "title": "Zeta"}]}),
                      "company": ok({"text": ""})})
    h = harness(tmp_path, fh)
    lk = tmp_path / "l"
    B.search_people(h, con, "Head of Finance", allow_live=True, lock_path=lk)
    B.assess_relevance(con)
    B.profile_leads(h, con, allow_live=True, lock_path=lk)
    B.verify_companies(h, con, allow_live=True, lock_path=lk)
    lead = con.execute("SELECT * FROM bottomup_leads WHERE profile_url=?", (RAVI,)).fetchone()
    assert lead["status"] == "company_unverified" and lead["company_id"] is None
    cid, pid = B.link_lead(con, lead["id"], domain="https://betafoods.example", by="krisha")
    assert con.execute("SELECT domain FROM companies WHERE id=?", (cid,)).fetchone()[0] == "betafoods.example"


def test_stop_on_page_limit(con, tmp_path):
    fh = FakeHarness({"people": {"ok": False, "error": "RuntimeError: Daily page limit 200 reached (DAILY_PAGE_LIMIT)."}})
    rep = B.search_people(harness(tmp_path, fh), con, "Head of Finance", allow_live=True, lock_path=tmp_path / "l")
    assert rep.stopped == "page_limit" and con.execute("SELECT COUNT(*) FROM bottomup_leads").fetchone()[0] == 0


def test_human_relevance_override(con, tmp_path):
    B.search_people(harness(tmp_path, fh_full()), con, "x", allow_live=True, lock_path=tmp_path / "l")
    B.assess_relevance(con)
    lid = con.execute("SELECT id FROM bottomup_leads WHERE status='unclear'").fetchone()[0]
    with pytest.raises(ValueError):
        B.review_lead(con, lid, "relevant", by="", reason="")
    B.review_lead(con, lid, "relevant", by="krisha", reason="she owns finance tooling")
    assert con.execute("SELECT status FROM bottomup_leads WHERE id=?", (lid,)).fetchone()[0] == "relevant"


def test_current_company_parsing():
    assert B.current_company(profile_data(), None) == ("Beta Foods Pvt Ltd", "experience_section")
    assert B.current_company({}, "VP Engineering at Gamma Labs | Speaker") == ("Gamma Labs", "headline")
    assert B.current_company({}, "Freelancer") == (None, "none")
    assert B.website_from_about(company_about_data()["text"]) == "betafoods.example"
    assert B.name_match("Beta Foods Pvt Ltd", "Beta Foods") == 1.0
