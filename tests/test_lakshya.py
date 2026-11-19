"""Lakshya handover ingestion: full package, idempotent, provenance kept, entity resolution shared, people gated."""
import json

import openpyxl
import pytest

from chanakya.intel import entities as E
from chanakya.intel import lakshya as LK
from chanakya.storage.db import connect


def _handover(root):
    (root / "03_Research").mkdir(parents=True)
    (root / "02_Deliverables").mkdir()
    seed = {"signals": {"sig-01": {"type": "tender", "company": "Acme Health", "companyKey": "acme-health",
                                   "url": "https://example.org/notice/1", "text": "note",
                                   "summary": "Acme asked the market for an OBIEE to Power BI migration utility.",
                                   "industry": "Healthcare", "region": "UK", "roles": ["Head of BI"], "urgency": 3,
                                   "budget": "likely", "noise": False, "fits": [], "createdAt": 1793793600000}},
            "accounts": {"acme-health": {"name": "Acme Health", "stage": "watching", "contacts": [], "notes": "",
                                         "createdAt": 1793793600000}},
            "themes": {}, "note": "test"}
    (root / "03_Research" / "lakshya_seed_all.json").write_text(json.dumps(seed), encoding="utf-8")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Gap themes"
    ws.append(["Gap theme", "What buyers need", "Reuse idea", "Signals (formula)", "Named companies",
               "Kinds of signal", "Verdict", "Next step"])
    ws.append(["Semantic layer", "One governed layer", "Setu parity", 5, "Acme Health", "1 (hiring)",
               "Building evidence", "Five calls"])
    ws = wb.create_sheet("Competitors")
    ws.append(["Competitor", "Area", "Offer", "Vendor-stated claim (unverified)", "How to answer", "Source"])
    ws.append(["KPI Partners", "Setu", "Migration utility", "70% lower cost", "Parity proof", "Source"])
    ws.cell(2, 6).hyperlink = "https://example.org/kpi"
    wb.save(root / "03_Research" / "Research_Findings.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Setu leads"
    ws.append(["Setu leads"])
    hdr = ["#", "Priority", "Lead", "Lead type", "What is needed", "Why now (evidence)", "Whom to reach out: first",
           "Whom to reach out: second", "By when to reach out", "Why that date",
           "What part of Setu could interest them", "Opening line (draft, your voice)", "Proof to bring",
           "Warm path to check", "Risk or caveat", "Next step", "Source", "Status", "Owner", "Email subject",
           "Email (copy and paste)", "Accelerator URL"]
    ws.append(hdr)
    ws.append([1, "A", "Acme Health", "End buyer", "Move OBIEE to Power BI", "Notice 2025", "Dana Field, CDO", "",
               "2026-11-06", "No deadline", "Migration and parity", "I saw your notice", "Tableau proof only", "",
               "Older notice", "Register", "Source", "Not started", None, "Subject", "Hello", "https://x"])
    ws.cell(3, 17).hyperlink = "https://example.org/notice/1"
    ws.append([2, "A", "BusinessObjects 4.3 customers", "Market segment", "A plan", "Support ends", "CDO", "",
               "2026-11-11", "", "Entry offer", "", "", "", "", "", "Source", "Not started", None, "S", "B", "u"])
    ws = wb.create_sheet("Deep dive Acme")
    ws.append(["Deep dive: Acme Health"])
    ws.append(["Section", "Item", "Detail", "Source"])
    ws.append(["Snapshot", "Who they are", "A health body.", "Source"])
    ws.append(["Whom to reach out", "1. Dana Field", "Chief Data Officer. Sponsor of the data agenda.", "Source"])
    ws.cell(4, 4).hyperlink = "https://acme.example/board"
    ws.append(["Whom to reach out", "2. Director of Data", "Advertised role.", None])
    ws.append(["Whom to reach out", "3. Others to map", "Sam Hill (Head of Cloud), Commercial team.", None])
    ws = wb.create_sheet("Setu parts")
    ws.append(["Part", "What it does", "Who cares most", "Proof you can show today", "Proof gap to close",
               "Status on your site"])
    ws.append(["Migration", "Converts dashboards", "Heads of BI", "1,200 Tableau dashboards", "No OBIEE proof",
               "On the site"])
    wb.save(root / "02_Deliverables" / "Setu_leads.xlsx")
    return root


@pytest.fixture
def con(tmp_path):
    return connect(tmp_path / "t.db")


def test_full_import_counts_provenance_and_idempotency(con, tmp_path):
    root = _handover(tmp_path / "h")
    r = LK.import_all(con, root, by="tester")
    assert r["validation"]["ok"], r["validation"]
    assert r["validation"]["source"] == {"signals": 1, "accounts": 1, "themes": 0}
    rec = r["records"]
    assert (rec["lead"]["created"], rec["competitor"]["created"], rec["setu_part"]["created"],
            rec["gap_theme"]["created"], rec["account"]["created"], rec["deep_dive"]["created"]) == (2, 1, 1, 1, 1, 4)
    # one canonical company for the signal, the account, the lead and the deep dive; the market segment has none
    assert con.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    lead = con.execute("SELECT * FROM lakshya_records WHERE external_id='lakshya:lead:setu-01'").fetchone()
    assert lead["company_id"] and lead["source_url"] == "https://example.org/notice/1"
    assert lead["source_file"].endswith("Setu_leads.xlsx") and lead["source_ref"] == "Setu leads!3"
    assert con.execute("SELECT company_id FROM lakshya_records WHERE external_id='lakshya:lead:setu-02'"
                       ).fetchone()[0] is None
    assert con.execute("SELECT source_url FROM lakshya_records WHERE kind='competitor'").fetchone()[0] == \
        "https://example.org/kpi"
    # named people only (not role descriptions), never approved automatically, no email taken
    ppl = {tuple(x) for x in con.execute("SELECT p.full_name, r.title, r.review_status FROM people p JOIN person_roles "
                                         "r ON r.person_id=p.id")}
    assert ppl == {("Dana Field", "Chief Data Officer", "needs_manual_review"),
                   ("Sam Hill", "Head of Cloud", "needs_manual_review")}
    assert con.execute("SELECT COUNT(*) FROM email_records").fetchone()[0] == 0
    assert "lakshya" in E.sources(con, "company", lead["company_id"])
    # second run changes nothing
    r2 = LK.import_all(con, root, by="tester")
    assert all(v["created"] == 0 and v.get("updated", 0) == 0 for v in r2["records"].values()), r2["records"]
    assert con.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM lakshya_imports").fetchone()[0] == 2


def test_context_is_labelled_historical_and_search_finds_records(con, tmp_path):
    root = _handover(tmp_path / "h")
    LK.import_all(con, root)
    cid = con.execute("SELECT id FROM companies").fetchone()[0]
    ctx = LK.context(con, cid, "PRD-SETU")
    assert "historical context, not current buying intent" in ctx and "Move OBIEE to Power BI" in ctx
    assert "has passed" in ctx  # the act-by date is in the past and is flagged, not presented as live
    assert LK.context(con, 9999) == ""
    hits = LK.search(con, "obiee")
    assert {h["kind"] for h in hits} >= {"lead", "signal", "setu_part"}
    assert all(h["historical"] for h in hits)
    assert LK.search(con, "kpi", kind="competitor")[0]["title"] == "KPI Partners"
    s = LK.summary(con)
    assert s["counts"]["lead"] == 2 and s["counts"]["signal"] == 1 and s["people"] == 2
    assert s["last_import"]["validation"]["ok"]


def test_changed_source_updates_in_place(con, tmp_path):
    root = _handover(tmp_path / "h")
    LK.import_all(con, root)
    p = root / "02_Deliverables" / "Setu_leads.xlsx"
    wb = openpyxl.load_workbook(p)
    wb["Setu leads"].cell(3, 18).value = "In progress"
    wb.save(p)
    r = LK.import_all(con, root)
    assert r["records"]["lead"] == {"created": 0, "updated": 1, "unchanged": 1}
    assert con.execute("SELECT COUNT(*) FROM lakshya_records WHERE kind='lead'").fetchone()[0] == 2


def test_missing_folder_is_an_error_not_a_silent_success(con, tmp_path):
    with pytest.raises(FileNotFoundError):
        LK.import_all(con, tmp_path / "nope")
