import json
import time
from contextlib import contextmanager

from chanakya.linkedin import live as L
from chanakya.storage.db import connect


class FakeMouse:
    def wheel(self, *a): pass


class FakePage:
    def __init__(self):
        self.url = "https://www.linkedin.com/login"
        self.mouse = FakeMouse()
        self.visits = []

    def goto(self, url, **kw):
        self.visits.append(url)
        if "search" in url:
            self.url = url

    def screenshot(self, **kw): return b"jpg"

    def evaluate(self, js, *a):
        return [{"url": "https://www.linkedin.com/in/jane-doe", "title": "Jane Doe", "lines": ["Head of Data at FCA"]},
                {"url": "https://www.linkedin.com/in/x", "title": "LinkedIn Member", "lines": []}]


def test_waits_for_user_login_then_searches_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "USAGE_FILE", tmp_path / "u.json")
    monkeypatch.setattr(L.LiveSession, "_wait", lambda self, p, s: time.sleep(0.005))
    monkeypatch.setattr(L.random, "uniform", lambda a, b: 0)
    dbp = tmp_path / "t.db"
    connect(str(dbp)).close()

    @contextmanager
    def fac():
        con = connect(str(dbp))
        try:
            yield con
        finally:
            con.close()

    page = FakePage()
    s = L.LiveSession(fac, launcher=lambda: (None, None, page))
    s.start("Krisha")
    for _ in range(200):
        if s.status()["status"] == "waiting_for_user":
            break
        time.sleep(0.01)
    assert s.status()["status"] == "waiting_for_user"  # agent never touches the login page
    page.url = "https://www.linkedin.com/feed/"  # the user signs in by hand
    for _ in range(300):
        if s.status()["linkedin_session"] == "signed_in":
            break
        time.sleep(0.01)
    s.enqueue("Head of Data Analytics FCA", pages=1, by="Krisha")
    s.enqueue("Head of Data Analytics FCA", pages=1, by="Krisha")
    for _ in range(500):
        st = s.status()
        if st["last_search"] and st["pending_searches"] == 0 and st["status"] == "ready" and st["duplicates"]:
            break
        time.sleep(0.01)
    st = s.status()
    s.stop()
    assert st["duplicates"] == 1 and st["skipped"] == 1
    con = connect(str(dbp))
    rows = con.execute("SELECT name, company_observed FROM bottomup_leads").fetchall()
    assert [tuple(r) for r in rows] == [("Jane Doe", "FCA")]


def test_company_from_headline():
    assert L.company_from_headline("Head of Data at Financial Conduct Authority | AI") == "Financial Conduct Authority"
    assert L.company_from_headline("Consultant") is None


def test_draft_lifecycle(tmp_path):
    import pytest
    from chanakya.outreach import drafts as D
    con = connect(str(tmp_path / "d.db"))
    con.execute("INSERT INTO people(dedupe_key,dedupe_basis,full_name,verification,first_seen,last_seen) "
                "VALUES('k','name','Jane','x','t','t')")
    con.commit()
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO outreach_drafts(prospect_id,person_id,channel,subject,body,hooks_json,generator,status,created_at,"
                "updated_at) VALUES(1,1,'email','Data at FCA','Hello Jane, a short note.','[]','t','generated','t','t')")
    con.commit()
    D.request_changes(con, 1, by="K", reason="tighten")
    with pytest.raises(D.DraftError):
        D.mark_ready(con, 1, by="K", reason="x")
    D.edit(con, 1, by="K", reason="fixed", body="Hello Jane, a shorter note.")
    D.approve(con, 1, by="K", reason="ok")
    assert con.execute("SELECT status FROM outreach_drafts").fetchone()[0] == "approved"
    D.reject(con, 1, by="K", reason="no")
    with pytest.raises(D.DraftError):
        D.approve(con, 1, by="K", reason="again")
    assert D.LABELS["edited"] == "Pending review"


def test_accelerator_run_end_to_end(tmp_path, monkeypatch):
    from chanakya.linkedin import claude_research as CR
    from chanakya.linkedin import pipeline as PL
    monkeypatch.setattr(L, "USAGE_FILE", tmp_path / "u.json")
    monkeypatch.setattr(L.LiveSession, "_wait", lambda self, p, s: None)
    monkeypatch.setattr(CR, "research_all", lambda comps, **k: [
        {"website": "acmefoods.co.uk", "summary": "Makes food.", "ceo": {"name": "Ann Lee", "title": "CEO",
         "linkedin_url": None, "source_url": "https://acmefoods.co.uk/team"}, "cfo": None}])
    monkeypatch.setattr(PL, "find_email", lambda con, pid: {"email": None, "status": "not_found"})
    dbp = tmp_path / "r.db"
    connect(str(dbp)).close()

    @contextmanager
    def fac():
        con = connect(str(dbp))
        try:
            yield con
        finally:
            con.close()

    class P(FakePage):
        def __init__(self):
            super().__init__()
            self.url = "https://www.linkedin.com/feed/"

        def evaluate(self, js, *a):
            if a and a[0] == "company":
                return [{"url": "https://www.linkedin.com/company/acme-foods", "title": "Acme Foods Ltd",
                         "lines": ["Food Production", "Leeds"]}]
            return [{"url": "https://www.linkedin.com/in/bob", "title": "Bob Ray",
                     "lines": ["Group CFO at Acme Foods"]}]
    s = L.LiveSession(fac, launcher=lambda: (None, None, P()))
    plan = {"accelerator": "Setu", "product_id": "PRD-SETU", "source": "test",
            "company_queries": [{"query": "food"}], "buyer_titles": [{"query": "Head of Data"}]}
    s.start("K")
    for _ in range(300):
        if s.status()["linkedin_session"] == "signed_in":
            break
        time.sleep(0.01)
    s.enqueue_accelerator(plan, max_companies=3, titles_per_company=1, by="K")
    for _ in range(600):
        if (s.status().get("run") or {}).get("status") == "ready":
            break
        time.sleep(0.02)
    st = s.status()
    s.stop()
    assert st["run"]["status"] == "ready", st["events"][-5:]
    con = connect(str(dbp))
    names = {r[0] for r in con.execute("SELECT full_name FROM people")}
    assert names == {"Ann Lee", "Bob Ray"}
    run = con.execute("SELECT company_ids_json, person_ids_json, companies_found_json FROM linkedin_runs").fetchone()
    assert len(json.loads(run[1])) == 2 and len(json.loads(run[2])) == 1


def _run_with(tmp_path, monkeypatch, research, plan_extra, companies, multi_extras=None, seed=None):
    from chanakya.linkedin import claude_research as CR
    from chanakya.linkedin import pipeline as PL
    seen = {}
    monkeypatch.setattr(L, "USAGE_FILE", tmp_path / "u.json")
    monkeypatch.setattr(L.LiveSession, "_wait", lambda self, p, s: None)

    def fake_research(comps, **k):
        seen["extras"] = k.get("extras")
        return [research[c["title"]] for c in comps]
    monkeypatch.setattr(CR, "research_all", fake_research)
    monkeypatch.setattr(CR, "find_events", lambda acc, **k: [{"name": "Solar O&M Forum", "url": "https://x.example/e"}])
    monkeypatch.setattr(PL, "find_email", lambda con, pid: {"email": None, "status": "not_found"})
    dbp = tmp_path / "r.db"
    c0 = connect(str(dbp))
    if seed:
        seed(c0)
        c0.commit()
    c0.close()

    @contextmanager
    def fac():
        con = connect(str(dbp))
        try:
            yield con
        finally:
            con.close()

    urls = []

    class P(FakePage):
        def __init__(self):
            super().__init__()
            self.url = "https://www.linkedin.com/feed/"

        def goto(self, url, **k):
            urls.append(url)
            return super().goto(url, **k) if hasattr(super(), "goto") else None

        def evaluate(self, js, *a):
            if a and a[0] == "company":
                return companies
            return []
    s = L.LiveSession(fac, launcher=lambda: (None, None, P()))
    plan = {"accelerator": "Wayam Eye", "product_id": "PRD-WAYAM-EYE", "source": "test",
            "company_queries": [{"query": "solar"}], "buyer_titles": [], **plan_extra}
    s.start("K")
    for _ in range(300):
        if s.status()["linkedin_session"] == "signed_in":
            break
        time.sleep(0.01)
    if multi_extras is not None:
        plan2 = {**plan, "accelerator": "Setu", "product_id": "PRD-SETU", "company_queries": [{"query": "grid"}]}
        s.enqueue_multi([plan, plan2], max_companies=15, by="K", extras=multi_extras)
    elif plan_extra.get("_single_extras") is not None:
        s.enqueue_accelerator({k: v for k, v in plan.items() if k != "_single_extras"}, max_companies=20,
                              titles_per_company=1, by="K", extras=plan_extra["_single_extras"])
    else:
        s.enqueue_accelerator(plan, max_companies=20, titles_per_company=1, by="K")
    for _ in range(600):
        if (s.status().get("run") or {}).get("status") == "ready":
            break
        time.sleep(0.02)
    st = s.status()
    s.stop()
    assert st["run"]["status"] == "ready", st["events"][-5:]
    return connect(str(dbp)), seen, urls


def test_run_location_filter_buyer_roles_content_and_events(tmp_path, monkeypatch):
    sun = {"website": "sunco.in", "summary": "Solar O&M.", "ceo": None, "cfo": None, "hq": "Pune, India",
           "in_geography": True, "content_score": 8, "content_best": "Warranty-evidence guide for EPCs",
           "content_why": "runs EPC handovers",
           "buyers": [{"role": "Head of asset management", "name": "Ravi Rao", "title": "Head of Asset Management",
                       "linkedin_url": None, "source_url": "https://sunco.in/team"}]}
    far = {"website": "far.de", "summary": "Solar.", "ceo": None, "cfo": None, "hq": "Berlin, Germany",
           "in_geography": False, "buyers": []}
    comps = [{"url": "https://www.linkedin.com/company/sunco", "title": "SunCo Energy", "lines": ["Renewables", "Pune"]},
             {"url": "https://www.linkedin.com/company/far", "title": "Far Solar", "lines": ["Renewables", "Berlin"]}]
    con, seen, urls = _run_with(tmp_path, monkeypatch, {"SunCo Energy": sun, "Far Solar": far},
                                {"geography": "India", "roles": ["Head of asset management"],
                                 "authority_content": ["Warranty-evidence guide for EPCs"], "events": True}, comps)
    assert seen["extras"]["geography"] == "India" and seen["extras"]["roles"] == ["Head of asset management"]
    assert any("companyHqGeo=%5B%22102713980%22%5D" in u for u in urls)  # LinkedIn HQ filter for India
    assert {r[0] for r in con.execute("SELECT full_name FROM people")} == {"Ravi Rao"}  # Far Solar filtered out
    run = con.execute("SELECT companies_found_json, events_json, settings_json FROM linkedin_runs").fetchone()
    found = {c["name"]: c for c in json.loads(run[0])}
    assert found["Far Solar"]["filtered"].startswith("not in India")
    assert found["SunCo Energy"]["content_score"] == 8 and found["SunCo Energy"]["hq"] == "Pune, India"
    assert json.loads(run[1])[0]["name"] == "Solar O&M Forum"
    assert json.loads(run[2])["roles"] == ["Head of asset management"]


def test_extras_prompt_and_apply():
    from chanakya.linkedin import claude_research as CR
    p = CR.extras_prompt({"roles": ["CPO"], "geography": "Pune", "authority_content": ["Checklist"]})
    assert "BUYER ROLES" in p and "Pune" in p and "Checklist" in p
    assert CR.extras_prompt(None) == ""
    out = CR.apply_extras({"buyers": [{"role": "CPO", "name": "A", "source_url": "https://x"},
                                      {"role": "CIO", "name": "B", "source_url": None}],
                           "location": {"hq": "Pune", "in_geography": True}, "content_fit": {"best": "C", "score": 14}})
    assert [b["name"] for b in out["buyers"]] == ["A"] and out["in_geography"] is True and out["content_score"] == 10


def test_run_revenue_band_filter_and_multi_extras(tmp_path, monkeypatch):
    mid = {"website": "mid.in", "summary": "Auto parts.", "ceo": None, "cfo": None, "revenue_cr": 1800,
           "revenue_fy": "FY2025", "revenue_source": "https://mid.in/ar", "in_revenue_band": True,
           "buyers": [{"role": "VP Strategic Sourcing", "name": "Asha Iyer", "title": "VP Strategic Sourcing",
                       "linkedin_url": None, "source_url": "https://mid.in/team"}]}
    big = {"website": "big.in", "summary": "Conglomerate.", "ceo": None, "cfo": None, "revenue_cr": 90000,
           "revenue_fy": "FY2025", "revenue_source": "https://big.in/ar", "in_revenue_band": False, "buyers": []}
    unk = {"website": "unk.in", "summary": "Private.", "ceo": None, "cfo": None, "revenue_cr": None,
           "in_revenue_band": None, "buyers": []}
    comps = [{"url": f"https://www.linkedin.com/company/{k}", "title": t, "lines": ["Manufacturing", "India"]}
             for k, t in (("mid", "Mid Auto"), ("big", "Big Group"), ("unk", "Unk Pvt"))]
    band = {"min_cr": 500, "max_cr": 5000}
    con, seen, _ = _run_with(tmp_path, monkeypatch, {"Mid Auto": mid, "Big Group": big, "Unk Pvt": unk}, {}, comps,
                             multi_extras={"roles": ["VP Strategic Sourcing"], "revenue": band})
    assert seen["extras"]["roles"] == ["VP Strategic Sourcing"] and seen["extras"]["revenue"] == band
    assert {r[0] for r in con.execute("SELECT full_name FROM people")} == {"Asha Iyer"}
    run = con.execute("SELECT companies_found_json, settings_json FROM linkedin_runs").fetchone()
    found = {c["name"]: c for c in json.loads(run[0])}
    assert found["Big Group"]["filtered"].startswith("revenue")
    assert found["Mid Auto"]["revenue_cr"] == 1800 and not found["Mid Auto"].get("filtered")
    assert "not confirmed" in found["Unk Pvt"]["location_note"]
    st = json.loads(run[1])
    assert st["revenue"] == band and st["roles"] == ["VP Strategic Sourcing"]


def test_revenue_prompt_and_band():
    from chanakya.linkedin import claude_research as CR
    band = {"revenue": {"min_cr": 500, "max_cr": 5000}}
    p = CR.extras_prompt(band)
    assert "REVENUE" in p and "500" in p and "5,000" in p
    ok = CR.apply_extras({"revenue": {"inr_cr": 1200, "fiscal_year": "FY2025", "source_url": "https://x/ar"}}, band)
    assert ok["in_revenue_band"] is True and ok["revenue_cr"] == 1200 and ok["revenue_fy"] == "FY2025"
    out = CR.apply_extras({"revenue": {"inr_cr": 9000, "source_url": "https://x/ar"}}, band)
    assert out["in_revenue_band"] is False
    nosrc = CR.apply_extras({"revenue": {"inr_cr": 1200, "source_url": None}}, band)
    assert nosrc["in_revenue_band"] is None  # an unsourced figure never decides the filter
    assert "in_revenue_band" not in CR.apply_extras({}, None)


def test_multi_run_custom_company_queries_and_sizes(tmp_path, monkeypatch):
    acme = {"website": "acme.in", "summary": "Auto components.", "ceo": None, "cfo": None, "buyers": []}
    comps = [{"url": "https://www.linkedin.com/company/acme", "title": "Acme Auto", "lines": ["Auto parts", "Pune"]}]
    con, seen, urls = _run_with(tmp_path, monkeypatch, {"Acme Auto": acme}, {}, comps,
                                multi_extras={"company_queries": ["auto components", "engineering goods"],
                                              "company_sizes": "FGH"})
    searches = [u for u in urls if "/search/results/companies/" in u]
    assert searches and all("companySize=%5B%22F%22%2C%22G%22%2C%22H%22%5D" in u for u in searches)
    assert any("keywords=auto%20components" in u for u in searches)
    assert not any("keywords=solar" in u or "keywords=grid" in u for u in searches)  # plan queries replaced
    st = json.loads(con.execute("SELECT settings_json FROM linkedin_runs").fetchone()[0])
    assert st["company_queries"] == ["auto components", "engineering goods"] and st["company_sizes"] == "FGH"


def test_single_run_custom_company_queries(tmp_path, monkeypatch):
    acme = {"website": "acme.in", "summary": "Pumps.", "ceo": None, "cfo": None, "buyers": []}
    comps = [{"url": "https://www.linkedin.com/company/acme", "title": "Acme Pumps", "lines": ["Machinery", "Pune"]}]
    con, _, urls = _run_with(tmp_path, monkeypatch, {"Acme Pumps": acme},
                             {"_single_extras": {"company_queries": ["industrial pumps"]}}, comps)
    searches = [u for u in urls if "/search/results/companies/" in u]
    assert any("keywords=industrial%20pumps" in u for u in searches)
    assert not any("keywords=solar" in u for u in searches)


def test_round_robin_spreads_research_across_searches():
    comps = [{"title": f"a{i}", "query": "auto"} for i in range(5)] + [{"title": "c1", "query": "chem"},
                                                                       {"title": "s1", "query": "steel"}]
    assert [c["title"] for c in L._round_robin(comps, 4)] == ["a0", "c1", "s1", "a1"]
    assert len(L._round_robin(comps, 50)) == 7


def test_find_email_holds_no_write_lock_during_network_calls(tmp_path, monkeypatch):
    """A slow website fetch or web search must not hold SQLite's write lock (live run 18 Nov: 'database is locked')."""
    from chanakya.emailhunter.checks import MXResult, Page
    from chanakya.linkedin import claude_research as CR
    from chanakya.linkedin import pipeline as PL
    con = connect(str(tmp_path / "e.db"))
    cid = PL.add_company(con, {"title": "Acme", "url": "https://www.linkedin.com/company/acme", "domain": "acme.in"},
                         "PRD-X")
    pid = PL.add_person(con, {"title": "Ann Lee", "current": "CFO at Acme"}, cid, "q")
    con.commit()
    open_during = []

    def fetcher(url):
        open_during.append(con.in_transaction)
        return Page(url, 200, "no emails here", True)

    def web_email(name, company, domain):
        open_during.append(con.in_transaction)
        return {"email": None}
    monkeypatch.setattr(CR, "web_email", web_email)
    PL.find_email(con, pid, fetcher=fetcher, mx_lookup=lambda d: MXResult("ok", ["mx.acme.in"]))
    assert len(open_during) >= 3 and not any(open_during), open_during


def _seed_use_cases(con):
    from chanakya.catalog.master_xlsx import TABLES
    con.executescript(TABLES)
    for pid, name, titles in (("PRD-WAYAM-EYE", "Wayam Eye", ["Inspection-to-work-order checklist", "Warranty evidence"]),
                              ("PRD-SETU", "Setu", ["Grid outage replanning"])):
        con.execute("INSERT INTO products(id,name,verification,source_path,source_sha256,raw_json,imported_at) "
                    "VALUES(?,?,?,?,?,?,?)", (pid, name, "test", "t", "t", "{}", "t"))
        for t in titles:
            con.execute("INSERT INTO catalogue_use_cases(product_id,kind,title,imported_at) VALUES(?,?,?,?)",
                        (pid, "client", t, "t"))


def test_mastersheet_content_used_when_run_has_none(tmp_path, monkeypatch):
    acme = {"website": "acme.in", "summary": "Solar O&M.", "ceo": None, "cfo": None, "buyers": [],
            "content_score": 7, "content_best": "Warranty evidence", "content_why": "EPC handovers"}
    comps = [{"url": "https://www.linkedin.com/company/acme", "title": "Acme Solar", "lines": ["Solar", "Pune"]}]
    con, seen, _ = _run_with(tmp_path, monkeypatch, {"Acme Solar": acme}, {}, comps,
                             multi_extras={"geography": "India"}, seed=_seed_use_cases)
    assert seen["extras"]["authority_content"] == {"Wayam Eye": ["Inspection-to-work-order checklist",
                                                                 "Warranty evidence"],
                                                   "Setu": ["Grid outage replanning"]}
    st = json.loads(con.execute("SELECT settings_json FROM linkedin_runs").fetchone()[0])
    assert st["authority_content_source"] == "master sheet use cases"
    found = json.loads(con.execute("SELECT companies_found_json FROM linkedin_runs").fetchone()[0])
    assert found[0]["content_best"] == "Warranty evidence"


def test_run_content_list_overrides_mastersheet(tmp_path, monkeypatch):
    acme = {"website": "acme.in", "summary": "Solar.", "ceo": None, "cfo": None, "buyers": []}
    comps = [{"url": "https://www.linkedin.com/company/acme", "title": "Acme Solar", "lines": ["Solar", "Pune"]}]
    con, seen, _ = _run_with(tmp_path, monkeypatch, {"Acme Solar": acme}, {}, comps,
                             multi_extras={"authority_content": ["My guide"]}, seed=_seed_use_cases)
    assert seen["extras"]["authority_content"] == ["My guide"]  # combined runs keep a given list
    st = json.loads(con.execute("SELECT settings_json FROM linkedin_runs").fetchone()[0])
    assert st["authority_content_source"] == "set for this run"


def test_extras_prompt_per_accelerator_content():
    from chanakya.linkedin import claude_research as CR
    p = CR.extras_prompt({"authority_content": {"Purti": ["Steel volatility"], "Sanchay": ["Batch expiry"], "X": []}})
    assert "per accelerator" in p and "Steel volatility" in p and "Batch expiry" in p and "best match" in p
    assert '"X"' not in p
    assert CR.extras_prompt({"authority_content": {"X": []}}) == ""


def test_content_label():
    from chanakya.api.linkedin_live_api import _content_label
    assert _content_label({}) == "none"
    assert _content_label({"authority_content": ["A", "B"]}) == "A; B"
    lab = _content_label({"authority_content": {"Purti": ["S1", "S2"]},
                          "authority_content_source": "master sheet use cases"})
    assert lab == "Purti: S1; S2 (master sheet use cases)"
