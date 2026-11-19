"""Output-quality evaluator: clean data passes; each defect class is caught; origins are counted apart."""
import json

from chanakya.intel import entities as E
from chanakya.intel import proof as P
from chanakya.intel import scoring as SC
from chanakya.intel import signals as SG
from chanakya.qa import evaluate as EV
from tests.helpers import seed_product


def _sig(con, company, provenance="test-fixture", url="https://x.example/a", **kw):
    if not con.execute("SELECT 1 FROM products WHERE id='PRD-SETU'").fetchone():
        seed_product(con, pid="PRD-SETU", name="Setu", summary="BI migration")
        P.seed(con)
    sid = SG.store_signal(con, type="hiring", summary=f"Tableau to Power BI at {company}", direction=E.TOP,
                          provenance=provenance, company={"name": company}, source_url=url,
                          fits=[{"product_id": "PRD-SETU", "score": 80}], **kw)[0]
    cid = con.execute("SELECT company_id FROM signals WHERE id=?", (sid,)).fetchone()[0]
    SC.score_opportunity(con, cid, "PRD-SETU")
    return sid, cid


def failing(r):
    return {c["check"] for c in r["checks"] if not c["pass"]}


def test_clean_data_passes_and_origins_are_separated(con):
    _sig(con, "Acme", provenance="lakshya-handover-research-2026-11-04")
    _sig(con, "Beta", provenance="company-website:beta.example")
    _sig(con, "Gamma", provenance="simulated-e2e")
    r = EV.evaluate(con)
    assert failing(r) == set(), r["checks"]
    assert r["counts"]["signals_by_origin"] == {"historical_seed": 1, "live": 1, "test_fixture": 1}


def test_missing_source_url_is_caught(con):
    _sig(con, "Acme", url=None)
    assert "every signal has a source URL" in failing(EV.evaluate(con))


def test_auto_verified_signal_is_caught(con):
    sid, _ = _sig(con, "Acme")
    con.execute("UPDATE signals SET verification_status='verified', verification_json='{}' WHERE id=?", (sid,))
    assert "no signal marked verified without a human reviewer" in failing(EV.evaluate(con))


def test_tampered_score_and_foreign_citation_are_caught(con):
    _, a = _sig(con, "Acme")
    sb, _ = _sig(con, "Beta")
    con.execute("UPDATE opportunities SET score=score+5 WHERE company_id=?", (a,))
    expl = json.dumps({"facts": [{"component": "need_fit", "detail": f"signal #{sb}"}]})
    con.execute("UPDATE opportunities SET explanation_json=? WHERE company_id=?", (expl, a))
    f = failing(EV.evaluate(con, rescore=False))
    assert "opportunity components in range and sum to the score" in f
    assert "every opportunity explanation has facts citing its own company's signals" in f


def test_duplicate_domain_forbidden_claim_and_unapproved_email_are_caught(con):
    _sig(con, "Acme")
    # domains are UNIQUE in the schema; a same-name record under another domain is the realistic duplicate
    con.execute("INSERT INTO companies(domain,name,data_json,first_seen,last_seen) VALUES('acme2.example','ACME','{}','t','t')")
    con.commit()
    con.execute("PRAGMA foreign_keys=OFF")  # a bare draft row is enough for the evaluator
    con.execute("INSERT INTO outreach_drafts(prospect_id,person_id,channel,to_address,subject,body,hooks_json,generator,"
                "status,created_at,updated_at) VALUES(1,1,'email','guess@acme.example','Hi',"
                "'We migrated a Cognos estate last year','[]','t','draft','t','t')")
    f = failing(EV.evaluate(con, rescore=False))
    assert {"no duplicate normalised company names", "no draft makes a forbidden proof claim",
            "no draft addressed to an email a human has not approved"} <= f


def test_evaluator_never_writes(con, tmp_path):
    _sig(con, "Acme")
    before = con.total_changes
    EV.evaluate(con)
    assert con.total_changes == before


def test_foreign_website_evidence_is_caught_and_rejection_clears_it(con):
    """The Omega failure class: a page from a different organisation's site attached to the account."""
    sid, cid = _sig(con, "Omega", provenance="company-website", url="https://www.omegahealthcare.com/news/1")
    con.execute("UPDATE companies SET domain='omegahms.com' WHERE id=?", (cid,))
    name = "company-website evidence comes from the account's own domain"
    assert name in failing(EV.evaluate(con))
    con.execute("UPDATE signals SET verification_status='rejected' WHERE id=?", (sid,))
    SC.score_opportunity(con, cid, "PRD-SETU")
    r = EV.evaluate(con)
    assert name not in failing(r) and "no opportunity cites a human-rejected signal" not in failing(r)


def test_own_subdomain_evidence_passes(con):
    sid, cid = _sig(con, "Beta", provenance="company-website", url="https://news.beta.example/p")
    con.execute("UPDATE companies SET domain='beta.example' WHERE id=?", (cid,))
    assert "company-website evidence comes from the account's own domain" not in failing(EV.evaluate(con))
