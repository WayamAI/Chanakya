"""HTTP API (UI adapter) - offline. Real agents + orchestrator on a temp SQLite DB; external clients are the same
fakes as the orchestrator tests. Data SYNTHETIC."""
import pytest
from fastapi.testclient import TestClient

from chanakya.api.app import create_app
from chanakya.storage.db import connect
from chanakya.workflow.orchestrator import Services
from tests.helpers import FakeHarness, FakeSite, harness, mx_ok, seed_company, seed_product

PW = "Correct-horse-9"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "api.db"
    monkeypatch.setenv("CHANAKYA_DB_PATH", str(db))
    monkeypatch.setenv("CHANAKYA_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.delenv("LINKEDIN_HARNESS_DIR", raising=False)
    monkeypatch.delenv("CHANAKYA_AUTH", raising=False)
    con = connect(db)
    icp = seed_product(con)
    seed_company(con, "acme.example", icp_id=icp, qualify=False)
    con.close()
    fh = FakeHarness({"people": {"ok": False, "error": "RuntimeError: Daily page limit 200 reached."}})
    svc = lambda live: Services(harness=harness(tmp_path, fh), mx_lookup=mx_ok, fetcher=FakeSite({}),  # noqa: E731
                                lock_path=tmp_path / "lock")
    app = create_app(services_factory=svc)
    c = TestClient(app)
    return c, app, db


def login(c):
    assert c.get("/api/auth/status").json()["setup_required"] is True
    r = c.post("/api/auth/setup", json={"name": "Krisha", "email": "k@wayam.example", "password": PW})
    assert r.status_code == 200 and "chanakya_session" in r.cookies


def test_auth_required_and_lockout(env):
    c, _, _ = env
    assert c.get("/api/summary").status_code == 401
    login(c)
    assert c.get("/api/auth/me").json()["user"]["name"] == "Krisha"
    assert c.post("/api/auth/setup", json={"name": "x", "email": "x@y.z", "password": PW}).status_code == 409
    c.post("/api/auth/logout")
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": "k@wayam.example", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "k@wayam.example", "password": PW}).status_code == 429


def test_run_until_gate_approve_resume(env):
    c, app, db = env
    login(c)
    s = c.get("/api/summary").json()
    assert s["run"] is None and s["counts"]["found"] == 1 and s["busy"] is False
    r = c.post("/api/runs")
    assert r.status_code == 202
    wid = r.json()["run_id"]
    app.state.jobs.join(10)
    run = c.get("/api/runs").json()["current"]
    assert run["status"] == "awaiting_companies" and run["month"].startswith("Company search")
    prog = c.get(f"/api/runs/{wid}/progress").json()
    assert [a["state"] for a in prog["agents"]][:2] == ["done", "paused"] and len(prog["agents"]) == 7
    assert any("company_gate" in line["msg"] for line in prog["log"])
    comps = c.get(f"/api/runs/{wid}/companies").json()
    row = comps["rows"][0]
    assert row["domain"] == "acme.example" and row["selected"] == 0 and row["fit_score"] is None
    assert c.get("/api/needs").json()["awaiting_companies"] is True

    assert c.post(f"/api/runs/{wid}/approve", json={"keep_ids": [row["id"]]}).status_code == 202
    app.state.jobs.join(10)
    con = connect(db)
    hr = con.execute("SELECT reviewer, status FROM human_reviews").fetchone()
    assert (hr["reviewer"], hr["status"]) == ("Krisha", "qualified")  # signed-in user is the reviewer
    run = c.get("/api/runs").json()["current"]
    # People Finder needs the exact Clay query confirmed: stopped, nothing called
    assert run["status"] == "paused" and run["stage"] == "people" and "Blocked" in run["reason"]
    st = c.get(f"/api/runs/{wid}/stages").json()
    dsl = next(s for s in st["stages"] if s["stage"] == "people")["output"]["acme.example"]
    assert c.post(f"/api/runs/{wid}/confirm", json={"stage": "people", "domain": "acme.example",
                                                    "query": dsl}).status_code == 200
    ev = con.execute("SELECT message FROM workflow_events WHERE message LIKE 'query confirmed%'").fetchone()
    assert "Krisha" in ev[0]
    audit = c.get("/api/audit").json()["rows"]
    assert audit[0]["actor"] == "Krisha" and audit[0]["action"] == "company qualified"


def test_one_job_at_a_time_and_cancel(env):
    c, app, _ = env
    login(c)
    wid = c.post("/api/runs").json()["run_id"]
    app.state.jobs.join(10)
    assert c.post(f"/api/runs/{wid}/cancel").json()["status"] == "cancelled"
    assert c.post(f"/api/runs/{wid}/resume").status_code == 409
    assert c.get("/api/runs").json()["current"]["status"] == "cancelled"


def test_sending_is_impossible(env):
    c, _, _ = env
    login(c)
    assert c.post("/api/outreach/1/send", json={"confirm": True}).status_code == 410
    assert c.post("/api/leaders/1/email", json={"action": "sent"}).status_code == 400
    items = {i["name"]: i for i in c.get("/api/integrations").json()["items"]}
    assert items["Email sending"]["status"].startswith("Disabled")
    paths = {getattr(r, "path", "") for r in c.app.routes}
    assert not any("send" in p and p != "/api/outreach/{pid}/send" for p in paths)


def test_read_endpoints_shape(env):
    c, _, _ = env
    login(c)
    assert c.get("/api/home").json()["top"][0]["domain"] == "acme.example"
    assert c.get("/api/leaders").json()["filter_counts"]["all"] == 0
    assert c.get("/api/outreach").json()["status_counts"]["draft"] == 0
    assert c.get("/api/offerings").json()["rows"][0]["slug"] == "PRD-TEST"
    assert len(c.get("/api/metrics/pages").json()) == 30
    assert c.get("/api/search?q=acme").json()["companies"][0]["name"]
    assert c.get("/api/settings").json()["setup_complete"] is True
    assert c.get("/api/icp").status_code == 404  # not mapped yet -> UI shows "Not available yet"


def test_bottom_up_run_via_api(env):
    c, app, _ = env
    login(c)
    r = c.post("/api/runs", json={"kind": "bottom_up", "params": {"query": "Head of Finance"}})
    app.state.jobs.join(10)
    run = c.get("/api/runs").json()["current"]
    assert run["kind"] == "bottom_up" and run["status"] == "paused" and "page_limit" in run["reason"]  # stopped safely
    prog = c.get(f"/api/runs/{r.json()['run_id']}/progress").json()
    assert prog["agents"][3]["name"].endswith("(not used here)")  # no resolver in bottom-up


def test_leaders_excel_export_link_works(env):
    """The UI's 'Export to Excel' (Home, Companies, Leaders, Metrics) links to /api/leaders.xlsx; it returned 404."""
    import io

    from openpyxl import load_workbook
    from tests.helpers import seed_person
    c, _, db = env
    login(c)
    con = connect(db)
    cid = con.execute("SELECT id FROM companies WHERE domain='acme.example'").fetchone()[0]
    seed_person(con, cid, "Asha Rao", "Chief Data Officer", approve=False)
    con.close()
    r = c.get("/api/leaders.xlsx")
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml")
    ws = load_workbook(io.BytesIO(r.content)).active
    rows = list(ws.values)
    assert rows[0][:4] == ("Name", "Title", "Company", "Role review")
    assert rows[1][0] == "Asha Rao" and rows[1][1] == "Chief Data Officer"
    assert c.get("/api/leaders.xlsx?run=999999").status_code == 200  # unknown run: header-only workbook, not an error


def test_company_page_actions_work_or_explain(env):
    """UI buttons that previously hit missing routes (bare 404 'Not Found'): real behaviour where the backend has the
    logic, otherwise a 501 that says where the capability lives. Found by the UI-route audit, 9 Nov 2026."""
    c, _, db = env
    login(c)
    con = connect(db)
    cid = con.execute("SELECT id FROM companies WHERE domain='acme.example'").fetchone()[0]
    con.close()
    r = c.post(f"/api/companies/{cid}/rescore")
    assert r.status_code == 200 and r.json()["company"]["id"] == cid
    r = c.post(f"/api/companies/{cid}/exclude")
    assert r.status_code == 200
    con = connect(db)
    row = con.execute("SELECT qualification_status, qualification_source FROM companies WHERE id=?", (cid,)).fetchone()
    assert row[0] == "disqualified" and row[1] == "human"
    con.close()
    assert c.post("/api/companies/999999/rescore").status_code == 404
    r = c.put("/api/leaders/1/hooks", json={})  # the one control with no backend meaning: it says where to go
    assert r.status_code == 501 and len(r.json()["detail"]) > 20


def test_workspace_actions_do_real_work(env):
    """Controls that used to answer 'not available': manual company and leader entry, run defaults, users, profile,
    notification read state."""
    c, _, db = env
    login(c)
    # add a company by hand: resolved through the shared resolver, recorded with the user, still unqualified
    assert c.post("/api/companies", json={"name": "Beta Ltd"}).status_code == 400
    r = c.post("/api/companies", json={"name": "Beta Ltd", "domain": "https://www.beta.example/about"})
    assert r.status_code == 201 and r.json()["created"] is True
    bid = r.json()["company"]["id"]
    again = c.post("/api/companies", json={"name": "Beta Limited", "domain": "beta.example"})
    assert again.json()["company"]["id"] == bid and again.json()["created"] is False  # matched, not duplicated
    con = connect(db)
    row = con.execute("SELECT domain, qualification_status FROM companies WHERE id=?", (bid,)).fetchone()
    assert tuple(row) == ("beta.example", "needs_manual_review")
    assert con.execute("SELECT source FROM discovery_events WHERE entity='company' AND entity_id=? ORDER BY id LIMIT 1",
                       (bid,)).fetchone()[0] == "manual:Krisha"
    con.close()
    # add a leader by hand: manual import, role waits for review
    assert c.post(f"/api/companies/{bid}/leaders", json={"name": "Asha Rao"}).status_code == 400
    assert c.post(f"/api/companies/{bid}/leaders", json={"name": "Asha Rao", "title": "CDO",
                                                         "linkedin_url": "https://x.example/a"}).status_code == 400
    r = c.post(f"/api/companies/{bid}/leaders", json={"name": "Asha Rao", "title": "Chief Data Officer"})
    assert r.status_code == 201 and r.json()["leader"]["name"] == "Asha Rao"
    con = connect(db)
    assert con.execute("SELECT review_status FROM person_roles").fetchone()[0] == "needs_manual_review"
    assert con.execute("SELECT source FROM person_sources").fetchone()[0] == "manual-import"
    con.close()
    assert c.post("/api/companies/99999/leaders", json={"name": "A B", "title": "T"}).status_code == 404
    # run defaults are validated, stored with the user and used by the next run
    s = c.get("/api/settings").json()
    assert s["run_defaults"]["brief_limit"] == 2 and s["run_defaults_updated"] is None
    assert c.put("/api/settings", json={"run_defaults": {"brief_limit": 99}}).status_code == 400
    assert c.put("/api/settings", json={"run_defaults": {"nope": 1}}).status_code == 400
    assert c.put("/api/settings", json={"run_defaults": {"brief_limit": 4}}).json()["run_defaults"]["brief_limit"] == 4
    s = c.get("/api/settings").json()
    assert s["run_defaults"]["brief_limit"] == 4 and s["run_defaults_updated"]["by"] == "Krisha"
    # profile
    assert c.put("/api/auth/me", json={"name": "Krisha P"}).json()["user"]["name"] == "Krisha P"
    assert c.put("/api/auth/me", json={"email": "nope"}).status_code == 400


def test_user_administration_keeps_one_active_owner(env):
    c, app, _ = env
    login(c)
    me = c.get("/api/users").json()["users"][0]
    assert me["role"] == "owner"
    assert c.post("/api/users", json={"name": "Sam", "email": "s@wayam.example", "role": "member",
                                      "password": "short"}).status_code == 400
    r = c.post("/api/users", json={"name": "Sam", "email": "s@wayam.example", "role": "member", "password": PW})
    assert r.status_code == 201
    sam = r.json()["user"]
    assert c.post("/api/users", json={"name": "Sam2", "email": "s@wayam.example", "password": PW}).status_code == 409
    # the last active owner cannot be demoted or disabled
    assert c.put(f"/api/users/{me['id']}", json={"role": "member"}).status_code == 409
    assert c.put(f"/api/users/{me['id']}", json={"disabled": True}).status_code == 409
    # a member can sign in, cannot manage users, and is signed out when disabled
    c2 = TestClient(app)
    assert c2.post("/api/auth/login", json={"email": "s@wayam.example", "password": PW}).status_code == 200
    assert c2.get("/api/users").status_code == 403
    assert c.put(f"/api/users/{sam['id']}", json={"disabled": True}).json()["user"]["disabled"] is True
    assert c2.get("/api/auth/me").status_code == 401
    assert c.put(f"/api/users/{sam['id']}", json={"disabled": False, "role": "owner"}).json()["user"]["role"] == "owner"
    assert c.put(f"/api/users/{me['id']}", json={"role": "member"}).status_code == 200  # now there is another owner


def test_revoke_other_session_only(env):
    c, app, _ = env
    login(c)
    other = TestClient(app)
    assert other.post("/api/auth/login", json={"email": "k@wayam.example", "password": PW}).status_code == 200
    rows = c.get("/api/auth/sessions").json()["sessions"]
    mine = next(s for s in rows if s["current"])
    theirs = next(s for s in rows if not s["current"])
    assert c.delete(f"/api/auth/sessions/{mine['id']}").status_code == 400
    assert c.delete(f"/api/auth/sessions/{theirs['id']}").json() == {"ok": True}
    assert other.get("/api/auth/me").status_code == 401
    assert c.delete("/api/auth/sessions/999999").status_code == 404
