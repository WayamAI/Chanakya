"""Unified workspace API (offline, temp DB, synthetic data; no live calls)."""
import json

import pytest
from fastapi.testclient import TestClient

from chanakya.api.app import create_app
from chanakya.intel import entities as E
from chanakya.intel import signals as SG
from chanakya.storage.db import connect
from chanakya.workflow.orchestrator import Services
from tests.helpers import seed_product

PW = "Correct-horse-9"


@pytest.fixture
def api(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    monkeypatch.setenv("CHANAKYA_DB_PATH", str(db))
    monkeypatch.delenv("CHANAKYA_AUTH", raising=False)
    monkeypatch.delenv("CHANAKYA_SIMULATED_EXTERNALS", raising=False)
    con = connect(db)
    seed_product(con)
    # top-down: a signal names "Beta Foods" (no domain); bottom-up: the same company arrives with its domain
    SG.store_signal(con, type="hiring", summary="Beta Foods hiring a finance automation lead", direction=E.TOP,
                    provenance="test", company={"name": "Beta Foods"}, source_url="https://jobs.example/1",
                    source_date="2026-10-20", roles=["Head of Finance"], fits=[{"product_id": "PRD-TEST", "score": 75}])
    E.resolve_company(con, name="Beta Foods", domain="betafoods.example", direction=E.BOTTOM, source="manual-import")
    SG.store_signal(con, type="news", summary="Gamma Ltd opens a plant", direction=E.TOP, provenance="test",
                    company={"name": "Gamma Ltd"}, fits=[{"product_id": "PRD-TEST", "score": 45}], is_gap=True,
                    gap_theme="Plant analytics")
    con.commit()
    con.close()
    c = TestClient(create_app(services_factory=lambda live: Services()))
    return c, db


def signin(c):
    assert c.post("/api/auth/setup", json={"name": "Krisha", "email": "k@w.example", "password": PW}).status_code == 200


def test_requires_auth(api):
    c, _ = api
    for p in ("/api/intel/dashboard", "/api/intel/accounts", "/api/intel/signals", "/api/intel/gaps",
              "/api/intel/accelerators", "/api/intel/settings", "/api/intel/integrations"):
        assert c.get(p).status_code == 401


def test_dashboard_origin_filter_and_convergence(api):
    c, _ = api
    signin(c)
    c.post("/api/intel/refresh", json={})
    d = c.get("/api/intel/dashboard").json()
    assert d["counts"]["accounts"] == 2 and d["counts"]["origins"]["both"] == 1
    assert d["top_opportunities"][0]["company"] == "Beta Foods" and d["top_opportunities"][0]["origin"] == "both"
    assert c.get("/api/intel/dashboard?origin=both").json()["counts"]["accounts"] == 1
    assert c.get("/api/intel/dashboard?origin=bottom_up").json()["counts"]["accounts"] == 0
    rows = c.get("/api/intel/accounts?origin=both").json()["rows"]
    assert [r["domain"] for r in rows] == ["betafoods.example"]


def test_account_workspace_and_human_resolution(api):
    c, db = api
    signin(c)
    c.post("/api/intel/refresh", json={})
    beta = [r for r in c.get("/api/intel/accounts").json()["rows"] if r["domain"] == "betafoods.example"][0]
    a = c.get(f"/api/intel/accounts/{beta['id']}").json()
    assert a["identity"]["origin"] == "both" and len(a["signals"]) == 1 and a["opportunities"][0]["score"] > 0
    assert a["buying_committee"]["roles_named_in_signals_without_a_person"][0]["role"] == "Head of Finance"
    assert a["opportunities"][0]["explanation"]["components"]["need_fit"] == 30
    gamma = [r for r in c.get("/api/intel/accounts").json()["rows"] if r["name"] == "Gamma Ltd"][0]
    assert gamma["identity_status"] == "unresolved"
    assert c.post(f"/api/intel/accounts/{gamma['id']}/resolve", json={"domain": "gamma.example"}).status_code == 400
    r = c.post(f"/api/intel/accounts/{gamma['id']}/resolve", json={"domain": "gamma.example", "reason": "website"})
    assert r.status_code == 200
    con = connect(db)
    assert con.execute("SELECT identity_status, domain FROM companies WHERE id=?", (gamma["id"],)).fetchone()[:] == \
        ("resolved", "gamma.example")


def test_signal_radar_review_gaps_settings(api):
    c, _ = api
    signin(c)
    s = c.get("/api/intel/signals?type=hiring").json()
    assert len(s["rows"]) == 1 and s["rows"][0]["basis"] == "source_date" and s["rows"][0]["weight"] == 0.8
    sid = s["rows"][0]["id"]
    assert c.post(f"/api/intel/signals/{sid}/review", json={"status": "verified"}).status_code == 400
    assert c.post(f"/api/intel/signals/{sid}/review", json={"status": "verified", "reason": "opened"}).status_code == 200
    assert c.get("/api/intel/signals?verification=verified").json()["rows"][0]["id"] == sid
    g = c.get("/api/intel/gaps").json()["rows"]
    assert g[0]["name"] == "Plant analytics" and g[0]["verdict"] == "early_signal"
    assert c.post(f"/api/intel/gaps/{g[0]['id']}/decide", json={"status": "parked", "reason": "one account"}).status_code == 200
    w = c.get("/api/intel/settings").json()
    assert w["settings"]["signal_weights"]["value"]["tender"] == 1.0 and w["linkedin_automation_enabled"] is False
    assert c.put("/api/intel/settings/signal_weights", json={"value": {"hiring": 0.9}}).status_code == 400
    assert c.put("/api/intel/settings/recency_basis", json={"value": "capture_date", "reason": "test"}).status_code == 200
    assert c.get("/api/intel/settings").json()["settings"]["recency_basis"]["value"] == "capture_date"


def test_linkedin_check_reports_policy_not_success(api):
    c, _ = api
    signin(c)
    r = c.post("/api/intel/integrations/linkedin/test").json()
    assert r["mode"] == "disabled_by_policy"
    st = c.get("/api/intel/integrations").json()["integrations"]
    assert st["linkedin"]["mode"] == "disabled_by_policy" and st["clay"]["mode"] == "unverified"
    links = c.get("/api/intel/linkedin-links?q=Head of IT&company=Lytx").json()
    assert links["links"][0]["url"].startswith("https://www.linkedin.com/search/results/people/")


def test_accelerators_show_proof_and_demand(api):
    c, _ = api
    signin(c)
    rows = c.get("/api/intel/accelerators").json()["rows"]
    assert rows[0]["id"] == "PRD-TEST" and rows[0]["demand"]["signals"] >= 1 and rows[0]["url"].endswith("/test")
    assert json.dumps(rows)  # serialisable
