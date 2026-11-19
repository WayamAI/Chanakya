"""Automation additions: tender source, auto domain resolution, leadership-page agent, scheduler, suppression,
export of account-based prospects, gate scoping. Synthetic fixtures only (no network)."""
import datetime as dt
import json

from chanakya.emailhunter.checks import Page
from chanakya.intel import auto_resolve as AR
from chanakya.intel.sources import tenders_uk as T
from chanakya.outreach import suppression as SUP
from chanakya.people import website_people as WP
from chanakya.qa import agent as Q
from chanakya.storage.db import jdump, now
from chanakya.workflow import orchestrator as W
from chanakya.workflow import scheduler as SCH
from tests.helpers import FakeSite, SeqLLM, seed_company, seed_person, seed_product


def _notice(i, title, status, buyer="Example Health Trust", desc=""):
    return {"item": {"id": f"n{i}", "title": title, "description": desc, "noticeStatus": status,
                     "organisationName": buyer, "publishedDate": "2026-10-01T10:00:00Z",
                     "deadlineDate": "2026-12-01T12:00:00Z", "end": "2027-09-30T00:00:00Z",
                     "awardedSupplier": "Supplier Ltd", "awardedValue": 29000}}


def fake_post(notices):
    calls = []

    def post(url, body):
        calls.append(body)
        return {"hitCount": len(notices), "noticeList": notices}
    post.calls = calls
    return post


def test_tender_sweep_filters_phrase_and_labels_status(con):
    seed_product(con, "PRD-SETU", "Setu")
    post = fake_post([_notice(1, "Power BI migration support", "Open"),
                      _notice(2, "LED screens and staging", "Open"),  # loose API hit: phrase absent -> dropped
                      _notice(3, "Tableau cloud licences", "Awarded", buyer="Example Regulator"),
                      _notice(4, "Power BI service", "Withdrawn")])
    r = T.sweep(con, keywords={"PRD-SETU": ["Power BI", "Tableau"]}, since=dt.date(2026, 6, 1), post=post)
    assert r["errors"] == [] and r["created"] == 2 and r["skipped"] == 1
    rows = {s["external_id"]: dict(s) for s in con.execute("SELECT * FROM signals")}
    assert rows["cf:n1"]["type"] == "tender" and rows["cf:n1"]["summary"].startswith("OPEN tender")
    assert rows["cf:n3"]["type"] == "news" and "not an open opportunity" in rows["cf:n3"]["summary"]
    assert rows["cf:n1"]["source_date"] == "2026-10-01"
    assert rows["cf:n1"]["source_url"].endswith("/Notice/n1")
    assert con.execute("SELECT COUNT(*) FROM companies WHERE identity_status='unresolved'").fetchone()[0] == 2
    again = T.sweep(con, keywords={"PRD-SETU": ["Power BI"]}, since=dt.date(2026, 6, 1), post=post)
    assert again["created"] == 0 and again["updated"] == 1  # idempotent by notice id


def test_tender_sweep_reports_errors(con):
    seed_product(con, "PRD-SETU", "Setu")

    def boom(url, body):
        raise ConnectionError("down")
    r = T.sweep(con, keywords={"PRD-SETU": ["Power BI"]}, post=boom)
    assert r["created"] == 0 and "ConnectionError" in r["errors"][0]


def _unresolved(con, name):
    from chanakya.intel import entities as E
    return E.resolve_company(con, name=name, direction=E.TOP, source="test")["company_id"]


def test_auto_resolve_strict_rules(con):
    a, b, c = _unresolved(con, "Example Health Trust"), _unresolved(con, "Other Body"), _unresolved(con, "Third Org")
    sugg = {"Example Health Trust": [{"domain": "examplehealth.example", "website": "https://examplehealth.example",
                                      "qid": "Q1", "label": "Example Health Trust", "description": "trust",
                                      "search_rank": 1, "source_url": "https://www.wikidata.org/wiki/Q1", "warnings": []}],
            "Other Body": [{"domain": "other.example", "website": "https://other.example", "qid": "Q2",
                            "label": "Other Body Holdings", "description": "x", "search_rank": 1,
                            "source_url": "u", "warnings": []}],
            "Third Org": []}
    site = FakeSite({"https://examplehealth.example/": "<title>Example Health Trust</title> welcome"})
    r = AR.auto_resolve(con, fetcher=site, suggest=lambda n: sugg[n])
    assert [x["company_id"] for x in r["resolved"]] == [a]
    assert {x["company_id"] for x in r["left_unresolved"]} == {b, c}
    row = con.execute("SELECT domain, identity_status, qualification_source FROM companies WHERE id=?", (a,)).fetchone()
    assert tuple(row) == ("examplehealth.example", "resolved", "system")  # qualification untouched


def test_auto_resolve_rejects_homepage_without_name(con):
    _unresolved(con, "Example Health Trust")
    sugg = [{"domain": "eh.example", "website": "https://eh.example", "qid": "Q1", "label": "Example Health Trust",
             "description": "d", "search_rank": 1, "source_url": "u", "warnings": []}]
    r = AR.auto_resolve(con, fetcher=FakeSite({"https://eh.example/": "unrelated parked page"}), suggest=lambda n: sugg)
    assert r["resolved"] == [] and "does not mention" in r["left_unresolved"][0]["reason"]


BOARD = """<html><body><a href="/about/our-board">Our board</a><h1>Our board</h1>
<p>Jane Example - Chief Executive</p><p>Raj Sample, Chief Digital Officer</p></body></html>"""


def test_website_people_keeps_only_verbatim(con):
    seed_product(con)
    cid = seed_company(con, "acme.example", "Acme")
    site = FakeSite({"https://acme.example/": '<a href="/about/our-board">Our board</a>',
                     "https://acme.example/about/our-board": BOARD})
    llm = SeqLLM({"people": [{"name": "Jane Example", "title": "Chief Executive"},
                             {"name": "Raj Sample", "title": "Chief Digital Officer"},
                             {"name": "Invented Person", "title": "CFO"}]},
                 *[{"people": []}] * 6)
    r = WP.find_people(con, cid, site, llm=llm)
    assert r["kept"] == 2 and r["rejected"][0]["name"] == "Invented Person"
    roles = con.execute("SELECT r.title, r.review_status, s.source FROM person_roles r JOIN person_sources s ON "
                        "s.person_id=r.person_id").fetchall()
    assert {(x[0], x[1], x[2]) for x in roles} == {("Chief Executive", "needs_manual_review", "company-website"),
                                                    ("Chief Digital Officer", "needs_manual_review", "company-website")}


def test_website_people_without_model_extracts_nothing(con):
    cid = seed_company(con, "acme.example", "Acme")
    r = WP.find_people(con, cid, FakeSite({}), llm=None)
    assert "no model" in r["skipped"]


class FakeJobs:
    def __init__(self):
        self.started, self.busy = [], False

    def start(self, wid, stage=None):
        self.started.append(wid)


def test_scheduler_starts_due_and_not_early(con):
    seed_product(con)
    sid = SCH.create(con, name="daily", kind="top_down", params={"product_id": "PRD-TEST"}, every_minutes=60, by="t")
    jobs = FakeJobs()
    did = SCH.tick(con, jobs)
    assert len(did) == 1 and jobs.started == [did[0]["started"]]
    assert SCH.tick(con, jobs) == []  # next run is an hour away
    s = SCH.listing(con)[0]
    assert s["last_workflow_id"] == jobs.started[0] and s["events"][0]["action"] == "started"
    assert json.loads(con.execute("SELECT params_json FROM workflows").fetchone()[0])["schedule_id"] == sid


def test_scheduler_waits_while_busy(con):
    seed_product(con)
    SCH.create(con, name="d", kind="top_down", params={"product_id": "PRD-TEST"}, every_minutes=60, by="t")
    jobs = FakeJobs()
    jobs.busy = True
    assert SCH.tick(con, jobs) == [] and jobs.started == []


def test_scheduler_resumes_only_when_gate_cleared(con):
    icp = seed_product(con)
    seed_company(con, "unrelated.example", "Unrelated")  # qualified, but outside this run's scope
    sid = SCH.create(con, name="d", kind="top_down", params={"product_id": "PRD-TEST"}, every_minutes=600, by="t")
    jobs = FakeJobs()
    wid = SCH.tick(con, jobs)[0]["started"]
    con.execute("UPDATE workflows SET status='waiting_human' WHERE id=?", (wid,))
    con.execute("UPDATE workflow_stages SET status='waiting_human' WHERE workflow_id=? AND stage='company_gate'", (wid,))
    con.commit()
    assert SCH.tick(con, jobs) == []  # nobody in scope qualified yet: gate still closed
    seed_company(con, "acme.example", "Acme", icp_id=icp)  # a human qualifies a company in the run's scope
    did = SCH.tick(con, jobs)
    assert did == [{"schedule_id": sid, "resumed": wid}]


def test_scheduler_rejects_tiny_interval(con):
    import pytest
    with pytest.raises(ValueError):
        SCH.create(con, name="x", kind="top_down", params={}, every_minutes=1, by="t")


def test_suppression_blocks_account_draft(con):
    import pytest
    from chanakya.outreach.account_drafts import generate_for_person
    seed_product(con)
    cid = seed_company(con)
    pid = seed_person(con, cid)
    SUP.add(con, by="t", reason="objected (Art. 21)", person_id=pid)
    with pytest.raises(SUP.Suppressed):
        generate_for_person(con, pid, by="t")
    assert SUP.reason_for(con, pid) == "objected (Art. 21)"


def test_suppression_by_domain(con):
    seed_product(con)
    cid = seed_company(con, "acme.example")
    pid = seed_person(con, cid)
    SUP.add(con, by="t", reason="company opted out", domain="acme.example")
    assert SUP.reason_for(con, pid) == "company opted out"


def test_export_rows_accept_account_based_prospect(con):
    """Regression: POST /api/outreach/export failed with KeyError 'research' on account-based prospects."""
    seed_product(con)
    cid = seed_company(con)
    pid = seed_person(con, cid)
    rec = {"person": {"id": pid, "name": "Asha"}, "company": {"id": cid, "name": "Acme", "domain": "acme.example"},
           "role": {"title": "CFO"}, "email": None, "opportunity": {"score": 59}}
    con.execute("INSERT INTO prospects(person_id,company_id,record_json,score,tier,issues_json,conflicts_json,qa_status,"
                "review_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pid, cid, jdump(rec), 59, "account_based", "[]", "[]", "account_based", "approved", now()))
    out = Q.rows(con, only_approved=True)
    assert out[0]["name"] == "Asha" and out[0]["research_confidence"] is None and out[0]["accelerators"] == ""
    assert Q.rows(con, min_tier="High") == []


def test_email_gate_scoped_to_workflow(con):
    seed_product(con)
    other = seed_company(con, "other.example", "Other")
    p = seed_person(con, other, name="Unrelated Person")
    con.execute("INSERT INTO email_records(person_id,company_id,email,status,method,confidence,evidence_json,review_status,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?)", (p, other, "u@other.example", "unverified", "pattern", 0.1, "[]",
                                                         "pending_review", now()))
    con.commit()
    wid = W.create_workflow(con, "bottom_up", {"query": "x"})
    wf = dict(con.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone())
    o = W.st_email_gate(W.Ctx(con, wf, W.Services(), live=True))
    assert o.status == "done"  # another run's pending email no longer blocks this run


def test_site_people_stage_in_tail():
    assert W.TOP_DOWN.index("site_people") == W.TOP_DOWN.index("company_gate") + 1
    assert "site_people" in W.BOTTOM_UP


def test_website_people_rejects_other_org_and_ranks_board_first(con):
    seed_product(con)
    cid = seed_company(con, "acme.example", "Acme Services Authority")
    page = ("<p>Jane Example, Chief Executive</p><p>Bob Vendor, Chief Executive Officer at Bigvendor</p>"
            '<a href="/what-we-do/our-board">Board</a>')
    site = FakeSite({"https://acme.example/": '<a href="/news/acme-awards-contract-for-workforce-management-tool">n</a>'
                                              '<a href="/what-we-do">What we do</a>',
                     "https://acme.example/what-we-do": '<a href="/what-we-do/our-board">Our board</a>',
                     "https://acme.example/what-we-do/our-board": page})
    class PageLLM:  # answers from the page it is shown, like a real extraction model
        model = "fake"

        def chat(self, messages, **kw):
            from types import SimpleNamespace
            on_board = "Jane Example" in messages[-1]["content"]
            out = {"people": [{"name": "Jane Example", "title": "Chief Executive", "organisation": "Acme"},
                              {"name": "Bob Vendor", "title": "Chief Executive Officer at Bigvendor",
                               "organisation": "Bigvendor"}] if on_board else []}
            return SimpleNamespace(content=json.dumps(out), message={}, model="fake", prompt_tokens=1, output_tokens=1)
    llm = PageLLM()
    r = WP.find_people(con, cid, site, llm=llm)
    assert site.calls.index("https://acme.example/what-we-do/our-board") < \
        site.calls.index("https://acme.example/news/acme-awards-contract-for-workforce-management-tool")
    assert [p["name"] for p in r["people"]] == ["Jane Example"]
    assert r["rejected"][0]["why"].startswith("the page says this person works for another")


def test_interrupted_run_is_recovered_at_startup_and_can_resume(con):
    """A stage left 'running' by a crash is marked failed with the reason, the run stops hanging, and the next run()
    continues from that stage."""
    from chanakya.workflow import orchestrator as W
    wid = W.create_workflow(con, "top_down", {"product_id": "PRD-SETU"})
    con.execute("UPDATE workflow_stages SET status='done' WHERE workflow_id=? AND stage='catalog'", (wid,))
    con.execute("UPDATE workflow_stages SET status='running', attempts=1 WHERE workflow_id=? AND stage='signal_sweep'",
                (wid,))
    con.execute("UPDATE workflows SET status='running', current_stage='signal_sweep' WHERE id=?", (wid,))
    con.commit()
    out = W.recover_interrupted(con)
    assert out == [{"workflow_id": wid, "stage": "signal_sweep"}]
    st = con.execute("SELECT status, error FROM workflow_stages WHERE workflow_id=? AND stage='signal_sweep'",
                     (wid,)).fetchone()
    assert st["status"] == "failed" and "interrupted" in st["error"]
    wf = W.get_workflow(con, wid)
    assert wf["status"] == "failed" and wf["current_stage"] == "signal_sweep"
    assert con.execute("SELECT COUNT(*) FROM workflow_events WHERE workflow_id=? AND level='error'", (wid,)).fetchone()[0]
    assert W.recover_interrupted(con) == []  # nothing left to recover
    W.run(con, wid, W.Services(), live=True)  # resumes from the interrupted stage
    assert con.execute("SELECT status FROM workflow_stages WHERE workflow_id=? AND stage='signal_sweep'",
                       (wid,)).fetchone()[0] == "done"
