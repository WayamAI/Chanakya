"""Orchestrator - full top-down and bottom-up workflows end to end, offline (fake Clay, harness, website, DNS, LLM).
Every human gate is exercised through the real review functions. All data SYNTHETIC."""
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chanakya.bottomup import agent as B
from chanakya.emailhunter import agent as E
from chanakya.people.role_review import review_role
from chanakya.qa import agent as Q
from chanakya.research import agent as R
from chanakya.resolver import review as RR
from chanakya.scout.qualification import record_human_decision
from chanakya.workflow import orchestrator as W
from tests.helpers import (FakeHarness, FakeSite, SeqLLM, company_about_data, company_search_data, harness, mx_ok, ok,
                           people_search_data, profile_data, research_data, seed_company, seed_product)
from tests.test_research import GOOD

URL = "https://www.linkedin.com/in/synthetic-asha-001"


class FakePeopleClay:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def call_tool(self, name, args):
        self.calls.append(name)
        payload = {"get-current-workspace": {"workspaceId": 1461095}, "get-credits-available": {"hasCredits": True},
                   "search-contacts": {"taskId": "mcp-task_SYN", "results": [
                       {"full_name": "Asha Testperson", "job_title": "Chief Financial Officer",
                        "company_domain": "acme.example", "entityId": "ent-1"}]}}[name]
        return SimpleNamespace(is_error=False, structured_content=payload, content=[])


def resolve_resp(argv):
    return ok({"input": {}, "status": "found", "profile_url": URL + "/", "confidence": "high",
               "best": {"url": URL + "/", "title": "Asha Testperson", "lines": [], "score": 1.0},
               "alternatives": [], "searches": []})


def last(rep):
    return rep["steps"][-1]


def test_top_down_end_to_end(con, tmp_path):
    icp = seed_product(con)
    seed_company(con, "acme.example", icp_id=icp, qualify=False)
    clay = FakePeopleClay()
    fh = FakeHarness({"resolve": resolve_resp, "research": ok(research_data(URL))})
    svc = W.Services(llm=SeqLLM(GOOD, {"subject": "Month-end close", "body": "Hi Asha, close takes 9 days?",
                                       "hook_ids": [2]}),
                     harness=harness(tmp_path, fh), clay_people_factory=lambda: clay, mx_lookup=mx_ok,
                     fetcher=FakeSite({"https://acme.example/team": "asha.testperson@acme.example"}),
                     lock_path=tmp_path / "lock")
    out = tmp_path / "exports"
    wid = W.create_workflow(con, "top_down", {"product_id": "PRD-TEST", "out_dir": str(out), "sender": "Krisha"})

    dry = W.run(con, wid, svc)  # dry run: whole plan, nothing done, nothing called
    assert not dry["live"] and fh.calls == [] and clay.calls == []
    assert all(s["status"] != "done" or s["stage"] in ("catalog", "signal_sweep", "scout", "resolve", "linkedin_gate", "email",
                                                       "email_gate", "research", "research_gate")
               for s in dry["steps"])
    assert {r["status"] for r in con.execute("SELECT status FROM workflow_stages WHERE workflow_id=?", (wid,))} == \
        {"pending"}

    r = W.run(con, wid, svc, live=True)
    assert [s["stage"] for s in r["steps"]] == ["catalog", "signal_sweep", "scout", "company_signals", "intel",
                                                "company_gate"]
    assert last(r)["status"] == "waiting_human" and r["status"] == "waiting_human"

    record_human_decision(con, "acme.example", "qualified", "krisha", "fits ICP")
    r = W.run(con, wid, svc, live=True)
    assert last(r)["stage"] == "people" and last(r)["status"] == "blocked"  # needs exact query confirmation
    dsl = last(r)["output"]["acme.example"]
    W.update_params(con, wid, confirm_people_query={"acme.example": dsl})
    r = W.run(con, wid, svc, live=True)
    assert [s["stage"] for s in r["steps"]] == ["people", "role_gate"] and last(r)["status"] == "waiting_human"
    assert clay.calls == ["get-current-workspace", "get-credits-available", "search-contacts"]

    role = con.execute("SELECT id FROM person_roles").fetchone()[0]
    review_role(con, role, "approved", reviewer="krisha", reason="CFO")
    r = W.run(con, wid, svc, live=True)
    assert [s["stage"] for s in r["steps"]] == ["role_gate", "resolve", "linkedin_gate"]
    RR.approve(con, con.execute("SELECT id FROM linkedin_resolutions").fetchone()[0], by="krisha", reason="match")

    r = W.run(con, wid, svc, live=True)
    assert [s["stage"] for s in r["steps"]] == ["linkedin_gate", "email", "email_gate"]
    E.approve(con, con.execute("SELECT id FROM email_records").fetchone()[0], by="krisha", reason="on site")

    r = W.run(con, wid, svc, live=True)
    assert [s["stage"] for s in r["steps"]] == ["email_gate", "research", "research_gate"]
    R.review(con, con.execute("SELECT id FROM profile_research").fetchone()[0], "approved", by="krisha",
             reason="evidence ok")

    r = W.run(con, wid, svc, live=True)
    assert [s["stage"] for s in r["steps"]] == ["research_gate", "qa", "qa_gate"]
    p = con.execute("SELECT * FROM prospects").fetchone()
    assert (p["tier"], p["qa_status"]) == ("High", "passed")
    Q.review_prospect(con, p["id"], "approved", by="krisha", reason="ready")

    r = W.run(con, wid, svc, live=True)
    assert last(r)["stage"] == "outreach" and last(r)["status"] == "done" and r["status"] == "completed"
    d = con.execute("SELECT * FROM outreach_drafts").fetchone()
    assert d["generator"] == "llm" and d["status"] == "draft"
    assert list(Path(out, "drafts").glob("*.eml"))
    rows = list(csv.DictReader(open(out / "prospects.csv", encoding="utf-8")))
    assert rows[0]["email"] == "asha.testperson@acme.example" and rows[0]["tier"] == "High"
    assert (out / "prospects.xlsx").exists()
    st = W.status(con, wid)
    assert all(s["status"] == "done" for s in st["stages"])
    assert next(x for x in st["stages"] if x["stage"] == "people")["run_ids"]  # people stage recorded its run id
    assert [c[3] for c in fh.calls] == ["status", "resolve", "status", "research"]


def test_bottom_up_end_to_end(con, tmp_path):
    seed_product(con)
    fh = FakeHarness({"people": ok(people_search_data()), "profile": ok(profile_data()),
                      "companies": ok(company_search_data()), "company": ok(company_about_data()),
                      "research": ok(research_data("https://www.linkedin.com/in/synthetic-ravi-002"))})
    svc = W.Services(harness=harness(tmp_path, fh), mx_lookup=mx_ok, fetcher=FakeSite({}), lock_path=tmp_path / "l")
    wid = W.create_workflow(con, "bottom_up", {"query": "Head of Finance", "out_dir": str(tmp_path / "x")})
    r = W.run(con, wid, svc, live=True)
    assert [s["stage"] for s in r["steps"]][:9] == ["bu_search", "bu_relevance", "bu_relevance_gate", "bu_profile",
                                                    "bu_company", "bu_match", "company_signals", "intel",
                                                    "company_gate"]
    assert last(r)["status"] == "waiting_human"
    # people came first: the company only exists because of the person's profile
    lead = con.execute("SELECT * FROM bottomup_leads WHERE status='linked'").fetchone()
    assert lead["company_domain"] == "betafoods.example"
    assert [c[3] for c in fh.calls][:2] == ["status", "people"]
    record_human_decision(con, "betafoods.example", "qualified", "krisha", "good fit")
    r = W.run(con, wid, svc, live=True)
    assert last(r)["stage"] == "role_gate"
    review_role(con, con.execute("SELECT id FROM person_roles WHERE person_id=?", (lead["person_id"],)).fetchone()[0],
                "approved", reviewer="krisha", reason="Head of Finance")
    r = W.run(con, wid, svc, live=True)
    assert [s["stage"] for s in r["steps"]] == ["role_gate", "email", "email_gate"]  # no resolver: URL from LinkedIn
    E.approve(con, con.execute("SELECT id FROM email_records").fetchone()[0], by="k", reason="pattern accepted")
    r = W.run(con, wid, svc, live=True)
    assert last(r)["stage"] == "research_gate"
    R.review(con, con.execute("SELECT id FROM profile_research").fetchone()[0], "approved", by="k", reason="ok")
    r = W.run(con, wid, svc, live=True)
    assert last(r)["stage"] == "qa_gate"
    rec = json.loads(con.execute("SELECT record_json FROM prospects").fetchone()[0])
    assert rec["origin"] == "bottom_up" and rec["person"]["linkedin_source"].startswith("linkedin people search")
    assert any(a["via"].startswith("bottom_up") for a in rec["accelerators"])


def test_failed_stage_keeps_progress_and_retries_only_itself(con, tmp_path, monkeypatch):
    seed_product(con)
    wid = W.create_workflow(con, "top_down", {"product_id": "PRD-TEST"})
    boom = {"n": 0}

    def flaky(c):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("clay timeout")
        return W.Outcome("done", {"ok": True})
    monkeypatch.setitem(W.STAGES, "scout", flaky)
    r = W.run(con, wid, W.Services(), live=True)
    assert [(s["stage"], s["status"]) for s in r["steps"]] == [("catalog", "done"), ("signal_sweep", "done"),
                                                                 ("scout", "failed")]
    assert W.get_workflow(con, wid)["status"] == "failed"
    assert any(e["level"] == "error" for e in W.events(con, wid))
    r = W.run(con, wid, W.Services(), live=True)
    assert r["steps"][0]["stage"] == "scout" and r["steps"][0]["status"] == "done"  # catalog not redone
    stg = {s["stage"]: s for s in W.status(con, wid)["stages"]}
    assert stg["scout"]["attempts"] == 2 and stg["catalog"]["attempts"] == 1


def test_missing_harness_blocks_with_instructions(con, tmp_path):
    seed_product(con)
    wid = W.create_workflow(con, "bottom_up", {"query": "CFO"})
    r = W.run(con, wid, W.Services(), live=True)
    # no harness: the compliant manual route (LinkedIn search links + import) instead of automation
    assert last(r)["status"] == "waiting_human" and "Import people" in last(r)["message"]
    assert last(r)["output"]["links"][0]["url"].startswith("https://www.linkedin.com/search/results/people/")


def test_run_single_stage_and_reset(con, tmp_path):
    seed_product(con)
    wid = W.create_workflow(con, "top_down", {"product_id": "PRD-TEST"})
    assert W.run_stage(con, wid, "catalog", W.Services(), live=True)["status"] == "done"
    with pytest.raises(ValueError):
        W.run_stage(con, wid, "bu_search", W.Services())
    W.reset_from(con, wid, "catalog")
    assert W.status(con, wid)["stages"][0]["status"] == "pending"


def test_scout_live_needs_confirmed_query(con):
    seed_product(con)
    wid = W.create_workflow(con, "top_down", {"product_id": "PRD-TEST", "new_company_search": True})
    r = W.run_stage(con, wid, "scout", W.Services(), live=True)
    assert r["status"] == "blocked"  # the synthetic ICP has no industries -> not ready / not confirmed


def test_cli_dry_run_end_to_end(tmp_path, monkeypatch, capsys):
    import importlib.util
    db = tmp_path / "c.db"
    monkeypatch.setenv("CHANAKYA_DB_PATH", str(db))
    monkeypatch.delenv("LINKEDIN_HARNESS_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("cli", Path(__file__).resolve().parents[1] / "scripts/chanakya.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "load_env", lambda: None)
    assert m.main(["workflow", "new", "bottom_up", "--param", "query=Head of Finance"]) == 0
    assert m.main(["workflow", "run", "1"]) == 0
    assert '"bu_search"' in capsys.readouterr().out
    assert m.main(["harness", "status"]) == 1
    assert m.main(["email", "approve", "99", "--by", "k", "--reason", "r"]) == 2
