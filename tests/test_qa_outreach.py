"""Agent 7 (QA / Sync) and outreach drafts - offline, SYNTHETIC data."""
import json
from pathlib import Path

import pytest

from chanakya.emailhunter import agent as E
from chanakya.outreach import drafts as D
from chanakya.qa import agent as Q
from chanakya.research import agent as R
from tests.helpers import (FakeHarness, FakeSite, SeqLLM, harness, mx_ok, ok, research_data, seed_company, seed_person,
                           seed_product, PROFILE_URL)
from tests.test_research import GOOD

SRC = Path(__file__).resolve().parents[1] / "src" / "chanakya"


def full_prospect(con, tmp_path):
    icp = seed_product(con)
    cid = seed_company(con, icp_id=icp)
    pid = seed_person(con, cid, linkedin=PROFILE_URL)
    E.run_email_hunter(con, allow_live=True, mx_lookup=mx_ok,
                       fetcher=FakeSite({"https://acme.example/team": "asha.testperson@acme.example"}))
    E.approve(con, con.execute("SELECT id FROM email_records").fetchone()[0], by="k", reason="published")
    R.run_researcher(harness(tmp_path, FakeHarness({"research": ok(research_data())})), con, llm=SeqLLM(GOOD),
                     allow_live=True, lock_path=tmp_path / "l")
    R.review(con, con.execute("SELECT id FROM profile_research").fetchone()[0], "approved", by="k", reason="ok")
    return pid


def test_full_prospect_scores_high_and_passes(con, tmp_path):
    pid = full_prospect(con, tmp_path)
    r = Q.run_qa(con)
    assert r["qa_status"]["passed"] == 1 and r["tiers"]["High"] == 1
    p = con.execute("SELECT * FROM prospects WHERE person_id=?", (pid,)).fetchone()
    assert p["score"] == 100 and p["tier"] == "High" and p["review_status"] == "pending_review"
    rec = json.loads(p["record_json"])
    assert rec["email"]["email"] == "asha.testperson@acme.example" and rec["accelerators"][0]["product_id"] == "PRD-TEST"
    assert rec["origin"] == "top_down"
    assert con.execute("SELECT COUNT(*) FROM qa_results WHERE entity_id=?", (pid,)).fetchone()[0] == 7


def test_blocked_without_gates(con):
    cid = seed_company(con, qualify=False)
    seed_person(con, cid, approve=False)
    r = Q.run_qa(con)
    assert r["qa_status"]["blocked"] == 1
    pr = con.execute("SELECT id FROM prospects").fetchone()[0]
    with pytest.raises(Q.ProspectReviewError):
        Q.review_prospect(con, pr, "approved", by="k", reason="r")


def test_conflicts_are_surfaced_not_dropped(con):
    cid = seed_company(con)
    a = seed_person(con, cid, "Asha Testperson", linkedin="https://www.linkedin.com/in/asha-one")
    b = seed_person(con, cid, "Asha Testperson", linkedin="https://www.linkedin.com/in/asha-two")  # 2nd identity
    seed_company(con, "acme-two.example", "Acme Analytics Limited")  # same normalized name, other domain
    E.override(con, a, "asha@elsewhere.example", by="k", reason="given verbally")
    Q.run_qa(con)
    ca = {c["check"] for c in json.loads(con.execute("SELECT conflicts_json FROM prospects WHERE person_id=?",
                                                    (a,)).fetchone()[0])}
    assert {"duplicate_person", "duplicate_company", "email_domain_match"} <= ca
    assert con.execute("SELECT qa_status FROM prospects WHERE person_id=?", (b,)).fetchone()[0] == "needs_review"
    assert con.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2  # nothing merged


def test_changed_record_needs_fresh_review(con, tmp_path):
    full_prospect(con, tmp_path)
    Q.run_qa(con)
    xid = con.execute("SELECT id FROM prospects").fetchone()[0]
    Q.review_prospect(con, xid, "approved", by="krisha", reason="good")
    Q.run_qa(con)  # unchanged -> stays approved
    assert con.execute("SELECT review_status FROM prospects").fetchone()[0] == "approved"
    E.override(con, con.execute("SELECT person_id FROM prospects").fetchone()[0], "asha.t@acme.example", by="k",
               reason="new address", evidence_url="https://acme.example/x")
    Q.run_qa(con)
    assert con.execute("SELECT review_status FROM prospects").fetchone()[0] == "pending_review"
    acts = [r[0] for r in con.execute("SELECT action FROM prospect_history WHERE prospect_id=? ORDER BY id", (xid,))]
    assert acts == ["create", "approve", "qa_changed"]


def test_exports_and_sync(con, tmp_path):
    full_prospect(con, tmp_path)
    Q.run_qa(con)
    rep = Q.sync(con, [("csv", str(tmp_path / "p.csv")), ("xlsx", str(tmp_path / "p.xlsx")),
                       ("jsonl", str(tmp_path / "p.jsonl")), ("webhook", "https://crm.example/hook?key=s3cret")],
                 only_approved=False)
    assert [r["status"] for r in rep] == ["ok", "ok", "ok", "dry_run"]
    assert "asha.testperson@acme.example" in (tmp_path / "p.csv").read_text(encoding="utf-8")
    from openpyxl import load_workbook
    assert load_workbook(tmp_path / "p.xlsx").active["C2"].value == "Asha Testperson"
    assert "s3cret" not in "".join(r[0] for r in con.execute("SELECT target FROM sync_runs"))
    sent = []
    Q.review_prospect(con, 1, "approved", by="k", reason="r")
    out = Q.sync(con, [("webhook", "https://crm.example/hook")], allow_live=True,
                 poster=lambda u, b: sent.append(b) or 200)
    assert out[0]["status"] == "ok" and len(sent[0]["prospects"]) == 1
    with pytest.raises(ValueError):
        Q.parse_targets("ftp:x")


def test_tiers():
    assert [Q.tier_of(s) for s in (100, 80, 79, 55, 54, 0)] == ["High", "High", "Medium", "Medium", "Low", "Low"]


# ---------------- outreach ----------------
def approved_prospect(con, tmp_path):
    full_prospect(con, tmp_path)
    Q.run_qa(con)
    Q.review_prospect(con, 1, "approved", by="k", reason="ok")


def test_no_draft_before_prospect_approval(con, tmp_path):
    full_prospect(con, tmp_path)
    Q.run_qa(con)
    assert D.generate(con) == []


def test_template_draft_and_human_flow(con, tmp_path):
    approved_prospect(con, tmp_path)
    [did] = D.generate(con, sender="Krisha")
    d = con.execute("SELECT * FROM outreach_drafts WHERE id=?", (did,)).fetchone()
    assert d["to_address"] == "asha.testperson@acme.example" and d["generator"] == "template"
    assert "[PERSONALISE" in d["body"] and json.loads(d["hooks_json"])
    with pytest.raises(D.DraftError):
        D.mark_ready(con, did, by="k", reason="r")
    D.edit(con, did, by="krisha", reason="personalised", subject="Faster close at Acme", body="Hi Asha, ...")
    D.mark_ready(con, did, by="krisha", reason="will send myself")
    assert [h["action"] for h in D.history(con, did)] == ["create", "edited", "ready"]
    assert D.generate(con) == []  # one live draft per prospect
    [p] = D.export_eml(con, tmp_path / "out")
    txt = Path(p).read_text()
    assert "NOT SENT" in txt and "Faster close at Acme" in txt


def test_llm_draft_validated(con, tmp_path):
    approved_prospect(con, tmp_path)
    bad = {"subject": "Hi", "body": "See https://evil.example now", "hook_ids": [1]}
    good = {"subject": "Month-end close", "body": "Hi Asha, you mentioned close takes 9 days...", "hook_ids": [2]}
    [did] = D.generate(con, llm=SeqLLM(bad, bad))
    assert con.execute("SELECT generator FROM outreach_drafts").fetchone()[0].startswith("template (llm draft rejected")
    D.discard(con, did, by="k", reason="retry")
    [did2] = D.generate(con, llm=SeqLLM(good))
    assert con.execute("SELECT generator, subject FROM outreach_drafts WHERE id=?", (did2,)).fetchone()[:] == \
        ("llm", "Month-end close")


def test_validate_rejects_unknown_hooks():
    with pytest.raises(D.DraftError):
        D.validate(D.DraftOut(subject="s", body="b", hook_ids=[9]), [{"id": 1, "evidence": []}])


def test_chanakya_contains_no_sending_code():
    banned = ("smtplib", "send_message(", "sendmail", "li_message", "li_connect", "Send.Mail")
    for f in SRC.rglob("*.py"):
        t = f.read_text(encoding="utf-8")
        for b in banned:
            assert b not in t, f"{b} found in {f}"
