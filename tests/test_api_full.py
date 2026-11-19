"""Full UI-API flows with SIMULATED external services (CHANAKYA_SIMULATED_EXTERNALS=1): both workflows driven only
through HTTP endpoints, every review gate, restart persistence, spend controls. Real agents, orchestrator, SQLite.
Nothing live is called; data is synthetic."""
import time

import pytest
from fastapi.testclient import TestClient

from chanakya.api.app import create_app

PW = "Correct-horse-9"


@pytest.fixture
def sim(tmp_path, monkeypatch):
    monkeypatch.setenv("CHANAKYA_SIMULATED_EXTERNALS", "1")
    monkeypatch.setenv("CHANAKYA_DB_PATH", str(tmp_path / "sim.db"))
    monkeypatch.setenv("CHANAKYA_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("CHANAKYA_AUTH", raising=False)
    monkeypatch.delenv("CHANAKYA_PAID_EMAIL_APPROVAL", raising=False)
    import tempfile
    from pathlib import Path
    (Path(tempfile.gettempdir()) / "chanakya-simulated.lock").unlink(missing_ok=True)

    def make():
        app = create_app()
        c = TestClient(app)
        return app, c
    return make


def signin(c, setup=True):
    if setup:
        assert c.post("/api/auth/setup", json={"name": "Krisha", "email": "k@w.example", "password": PW}).status_code == 200
    else:
        assert c.post("/api/auth/login", json={"email": "k@w.example", "password": PW}).status_code == 200


def wait(app):
    app.state.jobs.join(20)


def current(c):
    return c.get("/api/runs").json()["current"]


def decide(c, gate, rid, decision, **kw):
    r = c.post(f"/api/review/{gate}/{rid}", json={"decision": decision, "reason": "checked evidence", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_top_down_through_ui_api(sim):
    app, c = sim()
    signin(c)
    opts = c.get("/api/workflow-options").json()
    assert opts["simulated"] and opts["default_product"] == "PRD-SIM-FINOPS"
    assert c.get("/api/summary").json()["demo"] is True  # UI shows "Demo data"
    wid = c.post("/api/runs", json={"kind": "top_down", "params": {"product_id": "PRD-SIM-FINOPS"}}).json()["run_id"]
    wait(app)
    # scout blocked until the exact Clay query is confirmed in the UI
    run = current(c)
    assert run["stage"] == "scout" and "Blocked" in run["reason"]
    pend = c.get("/api/review/clay").json()["pending"]
    assert pend[0]["stage"] == "scout" and not pend[0]["confirmed"]
    # a WRONG query does not unblock
    c.post(f"/api/runs/{wid}/confirm", json={"stage": "scout", "query": "select from companies where x"})
    c.post(f"/api/runs/{wid}/retry"); wait(app)
    assert current(c)["stage"] == "scout"
    assert c.post(f"/api/runs/{wid}/confirm", json={"stage": "scout", "query": pend[0]["query"]}).status_code == 200
    c.post(f"/api/runs/{wid}/retry"); wait(app)
    run = current(c)
    assert run["status"] == "awaiting_companies"
    comp = [x for x in c.get("/api/review/companies").json()["rows"] if x["domain"] == "acme-analytics.example"][0]
    assert comp["sources"][0]["tool"] == "search-companies" and comp["assessment"]["search_filter_match"]
    # reason is mandatory
    assert c.post(f"/api/review/companies/{comp['id']}", json={"decision": "qualified"}).status_code == 400
    decide(c, "companies", comp["id"], "qualified")
    c.post(f"/api/runs/{wid}/resume"); wait(app)
    pend = [p for p in c.get("/api/review/clay").json()["pending"] if p["stage"] == "people"]
    c.post(f"/api/runs/{wid}/confirm", json={"stage": "people", "domain": pend[0]["domain"], "query": pend[0]["query"]})
    c.post(f"/api/runs/{wid}/retry"); wait(app)
    assert current(c)["stage"] == "role_gate"
    role = c.get("/api/review/roles").json()["rows"][0]
    assert role["full_name"] == "Asha Simulated" and role["sources"][0]["tool"] == "search-contacts"
    decide(c, "roles", role["id"], "approve")
    c.post(f"/api/runs/{wid}/resume"); wait(app)
    li = c.get("/api/review/linkedin").json()["rows"][0]
    assert li["confidence"] == "high" and len(li["candidate_urls"]) == 2
    # a URL that is not a stored candidate is refused by the resolver's own rule
    r = c.post(f"/api/review/linkedin/{li['id']}", json={"decision": "approve", "reason": "x",
                                                          "url": "https://www.linkedin.com/in/someone-else"})
    assert r.status_code == 400
    decide(c, "linkedin", li["id"], "approve")
    c.post(f"/api/runs/{wid}/resume"); wait(app)
    em = c.get("/api/review/emails").json()["rows"][0]
    assert em["status"] == "valid" and em["method"] == "website" and em["pages"]
    decide(c, "emails", em["id"], "approve")
    c.post(f"/api/runs/{wid}/resume"); wait(app)
    rs = c.get("/api/review/research").json()["rows"][0]
    assert rs["status"] == "researched" and rs["facts"]["activity"] and rs["model"] is None  # no LLM configured
    decide(c, "research", rs["id"], "approve")
    c.post(f"/api/runs/{wid}/resume"); wait(app)
    pr = c.get("/api/review/prospects").json()["rows"][0]
    assert pr["tier"] in ("High", "Medium") and pr["checks"]
    decide(c, "prospects", pr["id"], "approve")
    c.post(f"/api/runs/{wid}/resume"); wait(app)
    run = current(c)
    assert run["status"] == "awaiting_emails"  # completed: drafts await the human
    d = c.get("/api/drafts").json()["rows"][0]
    full = c.get(f"/api/drafts/{d['id']}").json()
    assert "[PERSONALISE" in full["body"] and full["history"][0]["action"] == "create"
    assert c.post(f"/api/drafts/{d['id']}", json={"action": "ready"}).status_code == 400  # markers left
    c.post(f"/api/drafts/{d['id']}", json={"action": "edit", "subject": "Close in 3 days", "body": "Hi Asha, ..."})
    assert c.post(f"/api/drafts/{d['id']}", json={"action": "ready"}).json()["status"] == "ready"
    assert [h["action"] for h in c.get(f"/api/drafts/{d['id']}").json()["history"]] == ["create", "edited", "ready"]
    eml = c.get(f"/api/drafts/{d['id']}/eml")
    assert eml.status_code == 200 and b"NOT SENT" in eml.content
    assert c.post(f"/api/drafts/{d['id']}", json={"action": "send"}).status_code == 400
    assert c.get("/api/exports/prospects.csv").status_code == 200
    assert c.get("/api/exports/prospects.xlsx").content[:2] == b"PK"
    prog = c.get(f"/api/runs/{wid}/progress").json()["agents"]
    assert all(a["state"] == "done" for a in prog)

    # restart: a NEW app instance on the same DB sees the same state and can still sign in
    app2, c2 = sim()
    signin(c2, setup=False)
    assert c2.get("/api/runs").json()["current"]["status"] == "awaiting_emails"
    assert len(c2.get("/api/audit").json()["rows"]) >= 8


def test_bottom_up_through_ui_api(sim):
    app, c = sim()
    signin(c)
    wid = c.post("/api/runs", json={"kind": "bottom_up", "params": {"query": "Head of Finance"}}).json()["run_id"]
    wait(app)
    run = current(c)
    assert run["kind"] == "bottom_up" and run["stage"] == "company_gate"
    leads = c.get("/api/review/leads").json()["rows"]
    linked = [x for x in leads if x["status"] == "linked"][0]
    assert linked["company_domain"] == "betafoods.example"
    unclear = [x for x in leads if x["status"] == "unclear"][0]
    decide(c, "leads", unclear["id"], "not_relevant")
    comp = [x for x in c.get("/api/review/companies").json()["rows"] if x["domain"] == "betafoods.example"][0]
    decide(c, "companies", comp["id"], "qualified")
    c.post(f"/api/runs/{wid}/resume"); wait(app)
    role = [r for r in c.get("/api/review/roles").json()["rows"] if r["review_status"] == "needs_manual_review"][0]
    decide(c, "roles", role["id"], "approve")
    c.post(f"/api/runs/{wid}/resume"); wait(app)
    em = c.get("/api/review/emails").json()["rows"][0]
    assert em["status"] == "unverified"  # no named evidence on the synthetic site -> prior pattern only
    decide(c, "emails", em["id"], "approve")
    c.post(f"/api/runs/{wid}/resume"); wait(app)
    assert current(c)["stage"] == "research_gate"


def test_pause_cancel_reset_and_paid_spend_controls(sim):
    app, c = sim()
    signin(c)
    wid = c.post("/api/runs", json={"kind": "top_down"}).json()["run_id"]
    wait(app)
    assert c.post(f"/api/runs/{wid}/pause").json()["status"] == "paused"
    assert current(c)["status"] == "paused" and current(c)["reason"] == "Paused by you"
    assert c.post(f"/api/runs/{wid}/reset/scout").status_code == 200
    assert c.post(f"/api/runs/{wid}/reset/bu_search").status_code == 400
    assert c.post(f"/api/runs/{wid}/cancel").json()["status"] == "cancelled"
    assert c.post(f"/api/runs/{wid}/retry").status_code == 409
    # spend approval needs a reason and a cap of 1..5; a new one revokes the old one
    assert c.post("/api/review/spend-approvals", json={"max_records": 2}).status_code == 400
    assert c.post("/api/review/spend-approvals", json={"max_records": 9, "reason": "r"}).status_code == 400
    a1 = c.post("/api/review/spend-approvals", json={"max_records": 1, "reason": "pilot"}).json()["id"]
    c.post("/api/review/spend-approvals", json={"max_records": 2, "reason": "pilot 2"})
    rows = {r["id"]: r for r in c.get("/api/review/clay").json()["spend_approvals"]}
    assert rows[a1]["status"] == "revoked" and rows[a1]["revoked_by"] == "Krisha"


def test_simulated_mode_refuses_real_database(tmp_path, monkeypatch):
    from chanakya.storage.db import connect
    from tests.helpers import seed_product
    db = tmp_path / "real.db"
    con = connect(db)
    seed_product(con)  # a "real" product
    con.close()
    monkeypatch.setenv("CHANAKYA_SIMULATED_EXTERNALS", "1")
    monkeypatch.setenv("CHANAKYA_DB_PATH", str(db))
    with pytest.raises(RuntimeError):
        create_app()


def test_review_endpoints_need_auth(sim):
    app, c = sim()
    for path in ("/api/review/summary", "/api/review/companies", "/api/drafts", "/api/integrations/harness"):
        assert c.get(path).status_code == 401
    signin(c)
    gates = {g["key"] for g in c.get("/api/review/summary").json()["gates"]}
    assert {"companies", "personas", "roles", "linkedin", "emails", "research", "prospects", "clay", "drafts",
            "leads", "mappings"} <= gates
    h = c.get("/api/integrations/harness").json()
    assert h["simulated"] and h["allowed"]["resolver"] == ["resolve", "status"]
    assert c.get("/api/integrations/ollama").json()["configured"] is False
    time.sleep(0)


def test_needs_fix_actions_use_review_paths(sim, tmp_path):
    from chanakya.storage.db import connect
    from tests.helpers import seed_company, seed_person
    app, c = sim()
    signin(c)
    con = connect(tmp_path / "sim.db")
    cid = seed_company(con, "fix.example", "Fix Co")
    pid = seed_person(con, cid, "Nina Fix")
    con.close()
    r = c.post(f"/api/leaders/{pid}/fix", json={"linkedin_url": "https://www.linkedin.com/in/nina-fix/"})
    assert r.status_code == 200 and r.json()["leader"]["linkedin_source"] == "Resolver (human-approved)"
    hist = c.get("/api/review/linkedin").json()["rows"][0]["history"]
    assert hist[0]["reviewer"] == "Krisha" and hist[0]["action"] == "approve"
    assert c.post(f"/api/leaders/{pid}/fix", json={"linkedin_url": "https://example.com/x"}).status_code == 400
    r = c.post(f"/api/leaders/{pid}/fix", json={"email": "nina@fix.example"})
    assert r.json()["leader"]["email"] == "nina@fix.example"
    assert c.post(f"/api/leaders/{pid}/fix", json={"drop": True}).status_code == 200
    assert c.get("/api/review/roles").json()["rows"][0]["review_status"] == "rejected"
    # "Research again" re-reads the company website (simulated here) and says LinkedIn research did not run
    r = c.post(f"/api/leaders/{pid}/research")
    assert r.status_code == 200 and "LinkedIn automation is off" in r.json()["linkedin_profile_research"]


def test_gates_wait_for_this_runs_own_discoveries(sim, tmp_path):
    """Earlier approvals (another company, another person) must not let a new run skip its own reviews."""
    from chanakya.storage.db import connect
    from tests.helpers import seed_company, seed_person
    app, c = sim()
    signin(c)
    con = connect(tmp_path / "sim.db")
    seed_person(con, seed_company(con, "old.example", "Old Co"), "Already Approved")  # qualified + approved
    con.close()
    wid = c.post("/api/runs", json={"kind": "bottom_up", "params": {"query": "Head of Finance"}}).json()["run_id"]
    wait(app)
    run = current(c)
    assert run["status"] == "awaiting_companies"
    gate = [x for x in c.get(f"/api/runs/{wid}/stages").json()["stages"] if x["stage"] == "company_gate"][0]
    assert gate["output"]["pending"] == ["betafoods.example"]
    comp = [x for x in c.get("/api/review/companies").json()["rows"] if x["domain"] == "betafoods.example"][0]
    decide(c, "companies", comp["id"], "qualified")
    c.post(f"/api/runs/{wid}/resume"); wait(app)
    assert current(c)["stage"] == "role_gate"  # this run's person still needs review
