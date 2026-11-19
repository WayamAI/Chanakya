"""Unified intelligence layer (Lakshya methodology + convergence). Offline; fixtures are synthetic unless stated."""
import datetime as dt
import json
import math
import os
from pathlib import Path

import pytest

from chanakya.emailhunter.checks import Page
from chanakya.intel import compliance as C
from chanakya.intel import entities as E
from chanakya.intel import gaps as G
from chanakya.intel import proof as P
from chanakya.intel import scoring as SC
from chanakya.intel import settings as S
from chanakya.intel import signals as SG
from chanakya.intel import voice as V
from tests.helpers import seed_company, seed_person, seed_product

HANDOVER_SEED = Path(os.getenv("LAKSHYA_SEED", r"C:\Users\LOQ\Desktop\lakshya-handover\Lakshya_Handover"
                                              r"\Lakshya_Handover\03_Research\lakshya_seed_all.json"))


def sig(con, company, type="hiring", fit=70, pid="PRD-TEST", gap=None, src_date=None, url=None, **kw):
    seed_product(con) if not con.execute("SELECT 1 FROM products WHERE id='PRD-TEST'").fetchone() else None
    return SG.store_signal(con, type=type, summary=f"{type} at {company}", direction=E.TOP, provenance="test",
                           company={"name": company, "key": E.slug(company)} if company else None,
                           market_subject=None if company else "Market", source_url=url, source_date=src_date,
                           fits=[{"product_id": pid, "score": fit, "why": "t"}] if fit else [], is_gap=bool(gap),
                           gap_theme=gap, **kw)[0]


# ---------------- methodology ----------------
def test_weights_and_recency_defaults(con):
    assert S.get(con, "signal_weights") == {"tender": 1.0, "conversation": 0.9, "hiring": 0.8, "regulation": 0.7,
                                            "filing": 0.7, "exec": 0.6, "news": 0.5, "post": 0.4}
    today = dt.date(2026, 11, 6)
    r = SG.recency(con, "2026-09-07", "2026-11-05", today)
    assert r["basis"] == "source_date" and r["days"] == 60 and r["r"] == round(math.exp(-1), 4)
    r2 = SG.recency(con, None, "2026-11-05", today)
    assert r2["basis"] == "capture_date_fallback" and r2["days"] == 1
    S.put(con, "signal_weights", {**S.get(con, "signal_weights"), "post": 0.2}, by="krisha")
    assert S.weight(con, "post") == 0.2
    with pytest.raises(ValueError):
        S.put(con, "nope", 1, by="k")


def test_store_signal_is_idempotent_and_keeps_both_dates(con):
    a, new = SG.store_signal(con, type="tender", summary="x", direction=E.TOP, provenance="t", external_id="t:1",
                             company={"name": "Acme"}, source_date="2026-10-01", captured_at="2026-11-01T00:00:00")
    b, again = SG.store_signal(con, type="tender", summary="x2", direction=E.TOP, provenance="t", external_id="t:1",
                               company={"name": "Acme"})
    s = con.execute("SELECT * FROM signals WHERE id=?", (a,)).fetchone()
    assert new and not again and a == b and s["source_date"] == "2026-10-01" and s["captured_at"].startswith("2026-11-01")
    with pytest.raises(ValueError):
        SG.store_signal(con, type="rumour", summary="x", direction=E.TOP, provenance="t")


def test_gap_verdicts_follow_presell_rule(con):
    # 3 accounts but one signal type -> Building evidence; add a second type -> Ready to pre-sell
    for co in ("A Co", "B Co", "C Co"):
        sig(con, co, type="hiring", fit=0, gap="Theme X")
    assert {g["key"]: g["verdict"] for g in G.recompute(con)}["theme-x"] == "building_evidence"
    sig(con, "A Co", type="regulation", fit=0, gap="Theme X")
    assert {g["key"]: g["verdict"] for g in G.recompute(con)}["theme-x"] == "ready_to_presell"
    # a strong fit (60+) means it is not a gap; a market-level signal is not an account
    sig(con, "D Co", type="news", fit=80, gap="Theme Y")
    sig(con, None, type="regulation", fit=0, gap="Theme Z")
    sig(con, "E Co", type="news", fit=0, gap="Theme Z")
    v = {g["key"]: g for g in G.recompute(con)}
    assert "theme-y" not in v and v["theme-z"]["accounts"] == 1 and v["theme-z"]["verdict"] == "early_signal"
    with pytest.raises(ValueError):
        G.decide(con, 1, "ship_it", by="k", reason="r")
    G.decide(con, 1, "pre_selling", by="krisha", reason="three buyers confirmed")


def test_opportunity_components_and_explanation(con):
    seed_product(con)
    P.seed(con)
    s1 = sig(con, "Acme Analytics", type="tender", fit=90, budget="explicit", src_date=str(dt.date.today()),
             url="https://acme.example/t", roles=["CFO"])
    cid = con.execute("SELECT company_id FROM signals WHERE id=?", (s1,)).fetchone()[0]
    o = SC.score_opportunity(con, cid, "PRD-TEST")
    c = o["components"]
    assert c == {"need_fit": 36, "timing": 20, "access": 6, "proof_fit": 0} and o["score"] == 62
    assert any("no registered proof" in u for u in o["unknowns"]) and any("no named" in u for u in o["unknowns"])
    # undated signal: timing halved and said so
    s2 = sig(con, "Beta", type="tender", fit=90)
    o2 = SC.score_opportunity(con, con.execute("SELECT company_id FROM signals WHERE id=?", (s2,)).fetchone()[0],
                              "PRD-TEST")
    assert o2["components"]["timing"] == 10 and any("halved" in u for u in o2["unknowns"])
    # rescoring writes history only on change
    SC.score_opportunity(con, cid, "PRD-TEST")
    assert con.execute("SELECT COUNT(*) FROM opportunity_history").fetchone()[0] == 2


def test_setu_proof_fit_uses_registry_only(con):
    seed_product(con, pid="PRD-SETU", name="Setu", summary="BI migration")
    P.seed(con)
    s = SG.store_signal(con, type="hiring", summary="BI Engineer to migrate Tableau to Power BI", direction=E.TOP,
                        provenance="t", company={"name": "Lytx"}, fits=[{"product_id": "PRD-SETU", "score": 82}])[0]
    cid = con.execute("SELECT company_id FROM signals WHERE id=?", (s,)).fetchone()[0]
    assert SC.score_opportunity(con, cid, "PRD-SETU")["components"]["proof_fit"] == 20
    assert P.claim_violations("We migrated a Cognos estate last year", "PRD-SETU")
    assert not P.claim_violations("Cognos 11.2 left standard support in April", "PRD-SETU")
    assert "BusinessObjects migration proof" not in P.allowed_proofs(con, "PRD-SETU")


def test_human_rejected_signal_no_longer_counts(con):
    """Live finding (9 Nov 2026): signals a human rejects (e.g. evidence from a wrongly resolved website) still lifted
    opportunity score, account heat, the accelerator ladder and gap themes."""
    s1 = sig(con, "Acme", type="tender", fit=90, src_date=str(dt.date.today()), gap="Theme A")
    cid = con.execute("SELECT company_id FROM signals WHERE id=?", (s1,)).fetchone()[0]
    assert SC.score_opportunity(con, cid, "PRD-TEST")["components"]["need_fit"] == 36
    assert SC.accelerator_ladder(con) and SC.account_heat(con, cid)["heat"] > 0
    SG.review_signal(con, s1, "rejected", by="krisha", reason="wrong company: site belongs to another Acme")
    o = SC.score_opportunity(con, cid, "PRD-TEST")
    assert o["components"]["need_fit"] == 0 and o["components"]["timing"] == 0
    assert SC.accelerator_ladder(con) == [] and SC.account_heat(con, cid)["heat"] == 0
    G.recompute(con)
    row = con.execute("SELECT accounts FROM gap_themes WHERE name='Theme A'").fetchone()
    assert row is None or row[0] == 0  # a theme with no remaining evidence is dropped or empty


def test_generic_migration_word_does_not_earn_tableau_proof(con):
    """Live finding (NHSBSA, 9 Nov 2026): an OAC/OBIEE -> Power BI need scored proof_fit 20 because the word
    'migration' matched the Tableau claim. Handover R4: no OBIEE proof exists, so this must not be full proof."""
    seed_product(con, pid="PRD-SETU", name="Setu", summary="BI migration")
    P.seed(con)
    s = SG.store_signal(con, type="tender", summary="Market engagement for an OAC/OBIEE to Power BI migration utility",
                        direction=E.TOP, provenance="t", company={"name": "NHSBSA"},
                        fits=[{"product_id": "PRD-SETU", "score": 90}])[0]
    cid = con.execute("SELECT company_id FROM signals WHERE id=?", (s,)).fetchone()[0]
    o = SC.score_opportunity(con, cid, "PRD-SETU")
    assert o["components"]["proof_fit"] == 10
    assert any("obiee" in u.lower() and "no published proof" in u.lower() for u in o["unknowns"])


# ---------------- convergence / entity resolution ----------------
def test_name_only_company_upgraded_when_domain_arrives_top_then_bottom(con):
    sig(con, "Lytx Inc", type="hiring")  # top-down: name only
    cid = con.execute("SELECT id FROM companies WHERE name='Lytx Inc'").fetchone()[0]
    assert con.execute("SELECT identity_status FROM companies WHERE id=?", (cid,)).fetchone()[0] == "unresolved"
    r = E.resolve_company(con, name="Lytx", domain="https://www.lytx.example/about", direction=E.BOTTOM,
                          source="manual-import")
    assert r["company_id"] == cid and r["matched_by"] == "name"
    row = con.execute("SELECT domain, identity_status FROM companies WHERE id=?", (cid,)).fetchone()
    assert tuple(row) == ("lytx.example", "resolved") and E.origin(con, "company", cid) == "both"
    assert con.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


def test_same_domain_merge_moves_evidence_and_keeps_snapshot(con):
    icp = seed_product(con)
    real = seed_company(con, "nhsbsa.example", "NHS Business Services Authority", icp_id=icp)  # bottom-up, has domain
    E.record_discovery(con, "company", real, E.BOTTOM, "manual-import")  # it was found people-first
    pid = seed_person(con, real, "Darren Example")
    s = sig(con, "NHSBSA", type="tender")  # top-down by a different name -> separate unresolved record
    tmp = con.execute("SELECT company_id FROM signals WHERE id=?", (s,)).fetchone()[0]
    assert tmp != real
    r = E.resolve_company(con, name="NHSBSA", domain="nhsbsa.example", direction=E.TOP, source="test")
    assert r["company_id"] == real and r["merged_from"] == tmp
    assert con.execute("SELECT company_id FROM signals WHERE id=?", (s,)).fetchone()[0] == real
    assert con.execute("SELECT COUNT(*) FROM companies WHERE id=?", (tmp,)).fetchone()[0] == 0
    m = con.execute("SELECT * FROM company_merges").fetchone()
    assert m["kept_id"] == real and json.loads(m["snapshot_json"])["company"]["name"] == "NHSBSA"
    assert E.origin(con, "company", real) == "both"
    assert con.execute("SELECT COUNT(*) FROM person_roles WHERE company_id=?", (real,)).fetchone()[0] == 1
    assert pid


def test_two_real_domains_never_auto_merge(con):
    a = seed_company(con, "a.example", "Same Name Ltd")
    b = seed_company(con, "b.example", "Other")
    with pytest.raises(ValueError):
        E.merge_companies(con, keep=a, drop=b, by="entity-resolver", reason="name")


def test_person_found_by_both_directions_is_one_person(con):
    from chanakya.people.finder import upsert_person
    cid = seed_company(con, "acme.example", "Acme")
    p1, new1 = upsert_person(con, {"full_name": "Asha Rao", "job_title": "CFO"}, cid, mappings=[], run_id=None,
                             raw_response_id=None, tool="search-contacts", task_id=None, query="q")
    p2, new2 = upsert_person(con, {"full_name": "Asha Rao", "job_title": "CFO",
                                   "linkedin_url": "https://www.linkedin.com/in/asha-rao"}, cid, mappings=[],
                             run_id=None, raw_response_id=None, tool="manual-import", task_id=None, query="q",
                             source="manual-import")
    assert p1 == p2 and new1 and not new2 and E.origin(con, "person", p1) == "both"
    assert con.execute("SELECT linkedin_url FROM people WHERE id=?", (p1,)).fetchone()[0].endswith("asha-rao")


def test_manual_import_links_lead_after_website_check(con):
    from chanakya.bottomup.agent import import_people
    seed_product(con)
    site = lambda url: Page(url, 200, "<title>Beta Foods - snacks</title>", True)  # noqa: E731
    r = import_people(con, [{"name": "Ravi Example", "headline": "Head of Finance",
                             "profile_url": "https://www.linkedin.com/in/ravi-example/", "company": "Beta Foods",
                             "company_domain": "betafoods.example"},
                            {"name": "No Url"}], query="Head of Finance", by="krisha", fetcher=site)
    assert len(r["created"]) == 1 and r["linked"] == r["created"] and r["issues"]
    lead = con.execute("SELECT * FROM bottomup_leads").fetchone()
    assert lead["status"] == "linked" and lead["company_domain"] == "betafoods.example"
    assert E.origin(con, "company", lead["company_id"]) == "bottom_up"
    assert con.execute("SELECT source FROM person_sources").fetchone()[0] == "manual-import"
    # importing the same profile again updates, it does not duplicate
    import_people(con, [{"name": "Ravi Example", "profile_url": "https://www.linkedin.com/in/ravi-example/",
                         "company": "Beta Foods"}], query="q", by="krisha")
    assert con.execute("SELECT COUNT(*) FROM bottomup_leads").fetchone()[0] == 1
    with pytest.raises(ValueError):
        import_people(con, [], query="q", by="")


def test_company_website_signals_attach_to_existing_account(con):
    from chanakya.intel.research_web import company_web_signals
    seed_product(con)
    cid = seed_company(con, "startup.example", "Startup Park")
    html = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Head of IT Operations",'
            '"datePosted":"2026-10-30","description":"incident response and alert triage"}</script>')
    f = lambda url: Page(url, 200, html if url.endswith("/careers") else "", True)  # noqa: E731
    out = company_web_signals(con, cid, f)
    assert len(out["signals"]) == 1
    s = con.execute("SELECT * FROM signals").fetchone()
    assert (s["company_id"], s["type"], s["source_date"], s["provenance"]) == (cid, "hiring", "2026-10-30",
                                                                                "company-website")


def test_verify_signal_never_marks_verified_automatically(con):
    s = sig(con, "Acme", url="https://acme.example/job")
    page = lambda url: Page(url, 200, '<title>Acme jobs</title><meta property="article:published_time" ' \
                                      'content="2026-10-02T00:00:00Z">acme hiring', True)  # noqa: E731
    v = SG.verify_signal(con, s, page)
    row = con.execute("SELECT * FROM signals WHERE id=?", (s,)).fetchone()
    assert v["status"] == "source_mentions_company" and row["source_date"] == "2026-10-02"
    SG.review_signal(con, s, "verified", by="krisha", reason="opened the page")
    SG.verify_signal(con, s, lambda u: Page(u, 404, "", True))
    assert con.execute("SELECT verification_status FROM signals WHERE id=?", (s,)).fetchone()[0] == "verified"


# ---------------- rules ----------------
def test_voice_and_compliance_rules(con):
    t = V.clean_text("We leverage a seamless ecosystem \u2014 it is best-in-class! \U0001F680")
    assert V.voice_issues(t) == [] and "\u2014" not in t and "!" not in t
    assert "jargon: synergy" in V.voice_issues("real synergy")
    assert V.accelerator_url("PRD-ARJUNA") == "https://wayam.ai/arjuna"
    assert V.accelerator_url("PRD-SETU") == "https://wayam.ai/accelerators/setu"
    assert not C.linkedin_automation_enabled(con)
    assert C.linkedin_people_search_link("Head of IT", "Lytx").endswith("Head%20of%20IT%20Lytx")
    assert not C.public_naming_allowed(con, dt.date(2026, 11, 6))
    assert C.public_naming_allowed(con, dt.date(2027, 1, 2))


@pytest.mark.skipif(not HANDOVER_SEED.exists(), reason="Lakshya handover seed not present on this machine "
                                                       "(set LAKSHYA_SEED to its path)")
def test_handover_seed_reproduces_documented_verdicts(con):
    """REAL handover data: the three gap verdicts must match the handover's Research_Findings 'Gap themes' tab."""
    for pid in {"PRD-" + f["id"].upper() for s in json.loads(HANDOVER_SEED.read_text(encoding="utf-8"))["signals"]
                .values() for f in s.get("fits", [])}:
        seed_product(con, pid=pid, name=pid) if not con.execute("SELECT 1 FROM products WHERE id=?",
                                                                (pid,)).fetchone() else None
    con.execute("DELETE FROM persona_role_mappings")
    r = SG.import_lakshya_seed(con, HANDOVER_SEED)
    assert r["signals_created"] == 38 and r["accounts"] == 27
    v = {g["name"]: g["verdict"] for g in G.recompute(con)}
    assert v == {"Forecasting and scheduling for DSM compliance": "ready_to_presell",
                 "Prior authorization automation": "building_evidence",
                 "Governed semantic layer for AI agents": "building_evidence"}
    assert SG.import_lakshya_seed(con, HANDOVER_SEED)["signals_created"] == 0  # idempotent


def test_combined_workflow_converges_signal_and_person_on_one_account(con, tmp_path):
    """Top-down signal names 'Beta Foods' (no domain). Bottom-up, a human imports its Head of Finance with the domain.
    The combined run must end with ONE company (origin both), the signal attached, and a scored opportunity."""
    from chanakya.bottomup.agent import import_people
    from chanakya.workflow import orchestrator as W
    seed_product(con)
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"signals": {"s1": {
        "type": "hiring", "company": "Beta Foods", "companyKey": "beta-foods", "url": "https://jobs.example/beta",
        "summary": "Beta Foods is hiring a finance automation lead to cut reconciliation time.", "industry": "Food",
        "region": "India", "roles": ["Head of Finance"], "urgency": 4, "budget": "likely", "noise": False,
        "fits": [{"id": "test", "score": 72, "why": "reconciliation"}], "isGap": False, "createdAt": 1793793540000}},
        "accounts": {"beta-foods": {"name": "Beta Foods", "stage": "watching"}}, "themes": {}}), encoding="utf-8")
    site = lambda url: Page(url, 200, "<title>Beta Foods</title>beta foods snacks", True)  # noqa: E731
    svc = W.Services(fetcher=site, mx_lookup=lambda d: __import__("chanakya.emailhunter.checks", fromlist=["x"])
                     .MXResult("ok", ["mx"], "fake"), lock_path=tmp_path / "l")
    wid = W.create_workflow(con, "combined", {"product_id": "PRD-TEST", "seed_path": str(seed),
                                              "query": "Head of Finance", "verify_sources": False,
                                              "pilot_scope": str(tmp_path / "no_scope.json")})
    r = W.run(con, wid, svc, live=True)
    assert r["steps"][-1]["stage"] == "bu_search" and r["steps"][-1]["status"] == "waiting_human"
    assert r["steps"][-1]["output"]["links"]  # compliant manual route offered
    import_people(con, [{"name": "Ravi Example", "headline": "Head of Finance at Beta Foods",
                         "profile_url": "https://www.linkedin.com/in/ravi-example/", "company": "Beta Foods",
                         "company_domain": "betafoods.example"}], query="Head of Finance", by="krisha",
                  workflow_id=wid, fetcher=site)
    r = W.run(con, wid, svc, live=True)
    assert r["steps"][-1]["stage"] == "company_gate" and r["steps"][-1]["status"] == "waiting_human"
    cos = con.execute("SELECT id, domain FROM companies WHERE domain NOT LIKE 'merged:%'").fetchall()
    beta = [c for c in cos if c["domain"] == "betafoods.example"]
    assert len(beta) == 1 and not [c for c in cos if "beta" in c["domain"] and c["domain"] != "betafoods.example"]
    cid = beta[0]["id"]
    assert E.origin(con, "company", cid) == "both"
    assert con.execute("SELECT company_id FROM signals WHERE external_id='lakshya:s1'").fetchone()[0] == cid
    opp = con.execute("SELECT * FROM opportunities WHERE company_id=? AND product_id='PRD-TEST'", (cid,)).fetchone()
    assert opp and opp["need_fit"] == 29 and opp["origin"] == "both"


def test_drafts_follow_handover_structure_and_block_false_proof(con, tmp_path):
    from chanakya.outreach import drafts as D
    from tests.test_qa_outreach import approved_prospect
    approved_prospect(con, tmp_path)
    [did] = D.generate(con, sender="Vineet")
    d = con.execute("SELECT * FROM outreach_drafts WHERE id=?", (did,)).fetchone()
    assert "Worth 20 minutes" in d["body"] and "wayam.ai/accelerators/" in d["body"] and "Vineet\nFounder & CEO, Wayam AI" in d["body"]
    assert V.voice_issues(d["body"].replace("[PERSONALISE", "")) == []
    D.edit(con, did, by="k", reason="r", subject="Close faster", body="Hi Asha, we migrated a Cognos estate for a bank.")
    with pytest.raises(D.DraftError):
        D.mark_ready(con, did, by="k", reason="r")
    D.edit(con, did, by="k", reason="r", body="Hi Asha, your close takes 9 days. Worth 20 minutes?")
    D.mark_ready(con, did, by="k", reason="checked")


def test_backfill_gives_old_records_their_origin(con):
    cid = seed_company(con, "old.example", "Old Co")  # created via Clay path (top-down provenance)
    con.execute("DELETE FROM discovery_events")
    pid = seed_person(con, cid, "Old Person")
    con.execute("DELETE FROM discovery_events")
    assert E.origin(con, "company", cid) is None
    n = E.backfill_discovery(con)
    assert n == {"companies": 1, "people": 1} and E.origin(con, "company", cid) == "top_down"
    assert E.backfill_discovery(con) == {"companies": 0, "people": 0} and pid
