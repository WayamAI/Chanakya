"""Hiring-signal discovery from public job APIs: exact need phrases only, dated, idempotent, errors reported."""
import datetime as dt

from chanakya.intel import entities as E
from chanakya.intel.sources import jobs_public as J
from chanakya.storage.db import connect

TODAY = dt.date.today()
TS = int(dt.datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=dt.timezone.utc).timestamp())


def _con(tmp_path):
    c = connect(tmp_path / "t.db")
    for pid, name in (("PRD-SETU", "Setu"), ("PRD-VEDHA", "Vedha")):
        c.execute("INSERT INTO products(id,name,status,verification,source_path,source_sha256,raw_json,imported_at) "
                  "VALUES(?,?,'existing_offering','unverified','t','t','{}','t')", (pid, name))
    return c


def _get(url):
    if "arbeitnow" in url:
        return {"links": {"next": None}, "data": [
            {"slug": "a1", "company_name": "Acme Logistics", "title": "BI Engineer (Tableau to Power BI)",
             "description": "<p>Lead our move.</p>", "url": "https://www.arbeitnow.com/jobs/a1", "location": "Berlin",
             "created_at": TS},
            {"slug": "a2", "company_name": "Other GmbH", "title": "Data Analyst",
             "description": "<p>You build dashboards in Power BI.</p>", "url": "https://www.arbeitnow.com/jobs/a2",
             "location": "Munich", "created_at": TS},
            {"slug": "a3", "company_name": "Old Co", "title": "Power BI migration lead", "description": "x",
             "url": "https://www.arbeitnow.com/jobs/a3", "location": "", "created_at": TS - 400 * 86400},
            {"slug": "a4", "company_name": "Sec Co", "title": "Engineer", "url": "https://www.arbeitnow.com/jobs/a4",
             "description": "We run a red team and need BusinessObjects skills.", "location": "", "created_at": TS}]}
    raise RuntimeError("remotive is down")


def test_only_need_phrases_match_and_title_beats_description():
    job = {"title": "Power BI developer", "text": "we use power bi daily"}
    assert J.match(job, J.DEFAULT_KEYWORDS) == []  # a tool name alone is not a need
    job = {"title": "Engineer", "text": "plan the migration to power bi from tableau"}
    assert J.match(job, J.DEFAULT_KEYWORDS)[0] | {} == {"product_id": "PRD-SETU", "phrase": "migration to Power BI",
                                                        "where": "description", "score": 38}
    assert J.match({"title": "Cognosphere artist", "text": ""}, J.DEFAULT_KEYWORDS) == []  # whole phrase only


def test_sweep_creates_dated_hiring_signals_and_companies_and_reports_feed_errors(tmp_path):
    con = _con(tmp_path)
    r = J.sweep(con, get=_get)
    assert r["created"] == 2 and len(r["errors"]) == 1 and "remotive" in r["errors"][0]
    assert {f["feed"]: f["matched"] for f in r["feeds"]} == {"arbeitnow": 2, "remotive": 0}
    rows = {s["company_name"]: s for s in con.execute("SELECT * FROM signals")}
    assert set(rows) == {"Acme Logistics", "Sec Co"}  # a3 is too old, a2 names no need
    s = rows["Acme Logistics"]
    assert s["type"] == "hiring" and s["source_date"] == TODAY.isoformat() and s["direction"] == E.TOP
    assert s["source_url"] == "https://www.arbeitnow.com/jobs/a1" and s["provenance"] == "job-board:arbeitnow"
    fits = {f["product_id"]: f["score"] for f in con.execute("SELECT * FROM signal_fits WHERE signal_id=?", (s["id"],))}
    assert fits == {"PRD-SETU": 50}
    multi = {f["product_id"]: f["score"] for f in con.execute("SELECT * FROM signal_fits WHERE signal_id=?",
                                                              (rows["Sec Co"]["id"],))}
    assert multi == {"PRD-SETU": 38, "PRD-VEDHA": 38}  # below the 60 strong-fit line: a lead to check
    cid = s["company_id"]
    assert con.execute("SELECT identity_status, qualification_status FROM companies WHERE id=?", (cid,)).fetchone()[:] \
        == ("unresolved", "needs_manual_review")
    assert E.sources(con, "company", cid) == ["jobs"] and E.origin(con, "company", cid) == "top_down"
    r2 = J.sweep(con, get=_get)  # same postings again: nothing new
    assert r2["created"] == 0 and r2["updated"] == 2
    assert con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2


def test_stage_is_blocked_when_every_external_source_fails(tmp_path):
    from chanakya.workflow import orchestrator as W
    con = _con(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("network down")
    wid = W.create_workflow(con, "top_down", {"product_id": "PRD-SETU", "tender_sweep": True, "job_sweep": True})
    ctx = W.Ctx(con, W.get_workflow(con, wid), W.Services(tender_post=boom, jobs_get=boom), True)
    out = W.st_signal_sweep(ctx)
    assert out.status == "blocked" and "unreachable" in out.message
    # one source up, one down: the stage completes and says which source failed
    ctx = W.Ctx(con, W.get_workflow(con, wid), W.Services(tender_post=boom, jobs_get=_get), True)
    out = W.st_signal_sweep(ctx)
    assert out.status == "done" and out.output["jobs"]["created"] == 2 and out.output["warnings"]
    assert out.output["new_companies_this_run"] == 2
