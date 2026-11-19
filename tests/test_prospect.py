"""Prospect Intelligence: one named person -> LinkedIn profile -> sourced accelerator-fit report. Data SYNTHETIC."""
import json
from types import SimpleNamespace

from chanakya.linkedin import prospect as P
from chanakya.storage.db import connect

ACCS = ["Arjuna", "Purti", "Sanchay"]


def _raw(**over):
    raw = {"person": {"name": "Asha Test", "title": "VP Operations", "tenure": "2 yrs",
                      "responsibilities": ["Plants"], "linkedin_url": "https://www.linkedin.com/in/asha-test"},
           "company": {"name": "Acme Auto", "website": "acme.example", "what_they_do": "Auto parts",
                       "industry": "Automotive", "hq": "Pune", "sources": ["https://acme.example/about"]},
           "fits": [{"accelerator": "Arjuna", "parts": {"role": 50, "need": 30, "evidence": 25}, "why": "cameras",
                     "evidence": [{"text": "runs 6 plants", "source": "LinkedIn profile"}]},
                    {"accelerator": "Purti", "parts": {"role": 20, "need": 20, "evidence": 10}, "why": "supply"},
                    {"accelerator": "Sanchay", "parts": {"role": 10, "need": 10, "evidence": 5}, "why": ""},
                    {"accelerator": "Nonexistent", "parts": {"role": 40, "need": 40, "evidence": 20}, "why": "x"}],
           "talking_points": ["Plant safety"], "caveats": []}
    raw.update(over)
    return raw


def test_match_candidates_normalises_company():
    items = [{"url": "u1", "title": "Asha", "lines": ["Head of Data at Tata Motors Limited"]},
             {"url": "u2", "title": "Asha", "lines": ["CFO, Infosys"]},
             {"url": "u3", "title": "Asha", "lines": ["Engineer", "Current: Manager at TATA MOTORS"]}]
    assert [i["url"] for i in P.match_candidates(items, "tata motors")] == ["u1", "u3"]
    assert [i["url"] for i in P.match_candidates(items, "Tata Motors Pvt. Ltd.")] == ["u1", "u3"]
    assert P.match_candidates(items, "  ") == []


def test_build_report_scores_and_filters():
    r = P.build_report(_raw(), ACCS)
    assert [f["accelerator"] for f in r["fits"]] == ["Arjuna", "Purti"]  # empty why and unknown name dropped
    top = r["fits"][0]
    assert top["parts"] == {"role": 40, "need": 30, "evidence": 20} and top["match_percent"] == 90
    assert r["fits"][1]["match_percent"] == 50
    assert r["person"]["title"] == "VP Operations" and r["talking_points"] == ["Plant safety"]


def test_build_report_keeps_top_three():
    fits = [{"accelerator": a, "parts": {"role": n, "need": n, "evidence": 1}, "why": "w"}
            for a, n in (("Arjuna", 10), ("Purti", 30), ("Sanchay", 20), ("Yukti", 35))]
    r = P.build_report(_raw(fits=fits), ACCS + ["Yukti"])
    assert [f["accelerator"] for f in r["fits"]] == ["Yukti", "Purti", "Sanchay"]


def test_build_report_drops_unsourced_company():
    raw = _raw(company={"name": "Acme Auto", "what_they_do": "Auto parts", "industry": "Auto", "sources": ["n/a"]})
    r = P.build_report(raw, ACCS)
    assert r["company"]["name"] == "Acme Auto" and r["company"]["what_they_do"] is None
    assert any("company" in c.lower() for c in r["caveats"])


def _client(text):
    blocks = [SimpleNamespace(type="text", text=text)]
    msg = SimpleNamespace(content=blocks, stop_reason="end_turn")
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **k: msg))


def test_research_person_parses_reply():
    r = P.research_person("Asha Test", "Acme Auto", "https://www.linkedin.com/in/asha-test", "VP Operations at Acme",
                          [{"name": a} for a in ACCS], client=_client("Here: " + json.dumps(_raw())))
    assert r["fits"][0]["accelerator"] == "Arjuna" and not r.get("error")


def test_research_person_handles_bad_json():
    r = P.research_person("Asha", "Acme", "u", "text", [{"name": "Arjuna"}], client=_client("nope"))
    assert r["error"]


def test_research_person_handles_exception():
    def boom(**k):
        raise RuntimeError("down")
    client = SimpleNamespace(messages=SimpleNamespace(create=boom))
    assert "down" in P.research_person("A", "B", "u", "t", [{"name": "Arjuna"}], client=client)["error"]


def test_table_roundtrip(tmp_path):
    con = connect(str(tmp_path / "p.db"))
    rid = P.create(con, "Asha Test", "Acme Auto", "Krisha")
    P.update(con, rid, status="choose", candidates=[{"url": "u1", "title": "Asha"}])
    got = P.get(con, rid)
    assert got["status"] == "choose" and got["candidates"] == [{"url": "u1", "title": "Asha"}]
    P.update(con, rid, status="ready", report={"fits": [{"accelerator": "Arjuna", "match_percent": 90}]})
    assert P.get(con, rid)["report"]["fits"][0]["accelerator"] == "Arjuna"
    assert [r["id"] for r in P.recent(con)] == [rid] and P.recent(con)[0]["top_fit"] == "Arjuna 90%"
    assert P.get(con, 999) is None


# ---------- live session jobs ----------
import time  # noqa: E402
from contextlib import contextmanager  # noqa: E402

from chanakya.linkedin import live as L  # noqa: E402

PROFILE = "Asha Test\nVP Operations at Acme Auto\nAbout: runs six plants"


class _Page:
    def __init__(self, cards):
        self.url = "https://www.linkedin.com/feed/"
        self.cards = cards
        self.mouse = SimpleNamespace(wheel=lambda *a: None)
        self.visits = []

    def goto(self, url, **k):
        self.visits.append(url)
        self.url = url

    def screenshot(self, **k):
        return b"jpg"

    def evaluate(self, js, *a):
        if js == P.PROFILE_JS:
            return PROFILE
        return self.cards if a and a[0] == "person" else []


def _session(tmp_path, monkeypatch, cards, limit=100):
    seen = {}
    monkeypatch.setattr(L, "USAGE_FILE", tmp_path / "u.json")
    monkeypatch.setenv("CHANAKYA_LINKEDIN_PAGES_PER_DAY", str(limit))
    monkeypatch.setattr(L.LiveSession, "_wait", lambda self, p, s: None)

    def fake_research(name, company, url, text, accs, client=None, role=""):
        seen.update(name=name, url=url, text=text)
        return {"person": {"name": name}, "company": {}, "fits": [{"accelerator": "Arjuna", "match_percent": 80}],
                "talking_points": [], "caveats": []}
    monkeypatch.setattr(P, "research_person", fake_research)
    monkeypatch.setattr(P, "accelerator_briefs", lambda con: [{"name": "Arjuna"}])
    dbp = tmp_path / "s.db"
    connect(str(dbp)).close()

    @contextmanager
    def fac():
        con = connect(str(dbp))
        try:
            yield con
        finally:
            con.close()
    page = _Page(cards)
    s = L.LiveSession(fac, launcher=lambda: (None, None, page))
    s.start("K")
    for _ in range(300):
        if s.status()["linkedin_session"] == "signed_in":
            break
        time.sleep(0.01)
    return s, fac, seen, page


def _until(s, fac, rid, statuses):
    for _ in range(500):
        with fac() as con:
            r = P.get(con, rid)
        if r and r["status"] in statuses:
            return r
        time.sleep(0.01)
    return r


CARD = lambda u, line: {"url": f"https://www.linkedin.com/in/{u}", "title": "Asha Test", "lines": [line]}  # noqa: E731


def test_person_single_match_researches(tmp_path, monkeypatch):
    s, fac, seen, page = _session(tmp_path, monkeypatch, [CARD("a1", "VP Operations at Acme Auto"),
                                                          CARD("a2", "Teacher at Some School")])
    rid = s.enqueue_person("Asha Test", "Acme Auto", by="K")
    r = _until(s, fac, rid, ("ready", "failed", "choose", "not_found"))
    s.stop()
    assert r["status"] == "ready", r
    assert r["linkedin_url"] == "https://www.linkedin.com/in/a1" and r["report"]["fits"][0]["accelerator"] == "Arjuna"
    assert seen["text"] == PROFILE and r["profile_text"] == PROFILE
    assert page.visits[-1] == "https://www.linkedin.com/in/a1"


def test_person_ambiguous_waits_for_pick(tmp_path, monkeypatch):
    s, fac, seen, _ = _session(tmp_path, monkeypatch, [CARD("a1", "VP at Acme Auto"), CARD("a2", "CFO at Acme Auto Ltd")])
    rid = s.enqueue_person("Asha Test", "Acme Auto")
    r = _until(s, fac, rid, ("choose", "ready", "failed", "not_found"))
    assert r["status"] == "choose" and [c["url"] for c in r["candidates"]] == [
        "https://www.linkedin.com/in/a1", "https://www.linkedin.com/in/a2"]
    assert s.status()["prospect"] == {"report_id": rid, "status": "choose"}
    s.enqueue_person_profile(rid, "https://www.linkedin.com/in/a2")
    r = _until(s, fac, rid, ("ready", "failed"))
    s.stop()
    assert r["status"] == "ready" and seen["url"] == "https://www.linkedin.com/in/a2"


def test_person_no_company_match_offers_others(tmp_path, monkeypatch):
    s, fac, _, _ = _session(tmp_path, monkeypatch, [CARD("a1", "Teacher at School")])
    rid = s.enqueue_person("Asha Test", "Acme Auto")
    r = _until(s, fac, rid, ("choose", "ready", "failed", "not_found"))
    s.stop()
    assert r["status"] == "choose" and len(r["candidates"]) == 1  # never auto-picked without a company match


def test_person_not_found(tmp_path, monkeypatch):
    s, fac, _, _ = _session(tmp_path, monkeypatch, [])
    rid = s.enqueue_person("Nobody Here", "Acme Auto")
    r = _until(s, fac, rid, ("not_found", "failed", "choose", "ready"))
    s.stop()
    assert r["status"] == "not_found" and "no linkedin profile" in r["error"].lower()


def test_person_budget_exhausted_fails(tmp_path, monkeypatch):
    s, fac, _, _ = _session(tmp_path, monkeypatch, [CARD("a1", "VP at Acme Auto")], limit=1)
    rid = s.enqueue_person("Asha Test", "Acme Auto")
    r = _until(s, fac, rid, ("failed", "not_found", "choose", "ready"))
    s.stop()
    assert r["status"] == "failed" and "budget" in r["error"].lower()


def test_enqueue_person_requires_name_and_company(tmp_path):
    import pytest
    s = L.LiveSession(None)
    with pytest.raises(ValueError):
        s.enqueue_person(" ", "Acme")
    with pytest.raises(ValueError):
        s.enqueue_person("Asha", "")


def test_profile_navigation_error_fails_report_keeps_session(tmp_path, monkeypatch):
    s, fac, _, page = _session(tmp_path, monkeypatch, [CARD("a1", "VP at Acme Auto")])
    real = page.goto

    def goto(url, **k):
        if "/in/" in url:
            raise RuntimeError("net::ERR_ABORTED")
        return real(url, **k)
    page.goto = goto
    rid = s.enqueue_person("Asha Test", "Acme Auto")
    r = _until(s, fac, rid, ("failed", "ready", "choose", "not_found"))
    alive = s.status()["browser"]
    s.stop()
    assert r["status"] == "failed" and "ERR_ABORTED" in r["error"] and alive == "connected"


# ---------- API + Excel ----------
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _FakeSession:
    def __init__(self, alive=True):
        self.thread = SimpleNamespace(is_alive=lambda: alive)
        self.picked = []
        self.queued = []

    def enqueue_person(self, name, company, by="", role=""):
        self.queued.append((name, company, by) + ((role,) if role else ()))
        return 7

    def enqueue_person_profile(self, rid, url):
        self.picked.append((rid, url))


@pytest.fixture
def api(tmp_path, monkeypatch):
    from chanakya.api import linkedin_live_api as A
    dbp = tmp_path / "a.db"
    connect(str(dbp)).close()

    @contextmanager
    def fac():
        con = connect(str(dbp))
        try:
            yield con
        finally:
            con.close()
    sess = _FakeSession()
    monkeypatch.setattr(A, "db", fac)
    monkeypatch.setattr(A, "_s", lambda: sess)
    monkeypatch.setattr(A.auth, "current_user", lambda req: {"name": "Krisha", "id": 1})
    monkeypatch.setenv("CHANAKYA_EXPORT_DIR", str(tmp_path / "exports"))
    return A, fac, sess


def test_api_person_start_and_validation(api):
    A, _, sess = api
    assert A.person_start(None, {"name": "Asha Test", "company": "Acme Auto"}) == {"report_id": 7}
    assert sess.queued == [("Asha Test", "Acme Auto", "Krisha")]
    with pytest.raises(HTTPException) as e:
        A.person_start(None, {"name": "", "company": "Acme"})
    assert e.value.status_code == 400
    sess.thread = SimpleNamespace(is_alive=lambda: False)
    with pytest.raises(HTTPException) as e:
        A.person_start(None, {"name": "Asha", "company": "Acme"})
    assert e.value.status_code == 409


def test_api_pick_validates_state_and_url(api):
    A, fac, sess = api
    with fac() as con:
        rid = P.create(con, "Asha Test", "Acme Auto", "K")
        P.update(con, rid, status="choose", candidates=[{"url": "https://www.linkedin.com/in/a1", "title": "Asha"}])
    with pytest.raises(HTTPException) as e:
        A.person_pick({"report_id": rid, "url": "https://www.linkedin.com/in/evil"})
    assert e.value.status_code == 400
    assert A.person_pick({"report_id": rid, "url": "https://www.linkedin.com/in/a1"})["status"] == "researching"
    assert sess.picked == [(rid, "https://www.linkedin.com/in/a1")]
    with pytest.raises(HTTPException) as e:  # no longer in 'choose'
        A.person_pick({"report_id": rid, "url": "https://www.linkedin.com/in/a1"})
    assert e.value.status_code == 400


def test_api_get_list_and_xlsx(api):
    from openpyxl import load_workbook
    A, fac, _ = api
    with pytest.raises(HTTPException) as e:
        A.person_get(404)
    assert e.value.status_code == 404
    with fac() as con:
        rid = P.create(con, "Asha Test", "Acme Auto", "K")
    with pytest.raises(HTTPException) as e:  # not ready yet
        A.person_xlsx(rid)
    assert e.value.status_code == 404
    rep = P.build_report(_raw(), ACCS)
    with fac() as con:
        P.update(con, rid, status="ready", linkedin_url="https://www.linkedin.com/in/asha-test", report=rep)
    assert A.person_get(rid)["report"]["fits"][0]["accelerator"] == "Arjuna"
    assert A.persons()["reports"][0]["top_fit"] == "Arjuna 90%"
    resp = A.person_xlsx(rid)
    ws = load_workbook(resp.path)["Prospect"]
    rows = [tuple(c for c in r) for r in ws.iter_rows(values_only=True)]
    assert ("Name", "Asha Test") in [r[:2] for r in rows]
    fit = next(r for r in rows if r[0] == "Arjuna")
    assert fit[1:6] == (90, 40, 30, 20, "cameras") and "runs 6 plants" in fit[6]


def test_person_search_redirected_away_fails_instead_of_reading(tmp_path, monkeypatch):
    s, fac, _, page = _session(tmp_path, monkeypatch, [CARD("x1", "wants to connect / Ignore / Accept")])
    real = page.goto

    def goto(url, **k):
        real(url, **k)
        if "/search/results/people" in url:  # LinkedIn bounced the search to My Network
            page.url = "https://www.linkedin.com/mynetwork/grow/"
    page.goto = goto
    rid = s.enqueue_person("Asha Test", "Acme Auto")
    r = _until(s, fac, rid, ("failed", "choose", "ready", "not_found"))
    s.stop()
    assert r["status"] == "failed" and "search results" in r["error"] and r["candidates"] == []


def test_match_requires_name_and_current_company():
    # same shapes as a live LinkedIn people search (names and companies are synthetic)
    items = [{"url": "u1", "title": "Ravi Testname", "lines": ["Chairman and CEO at Contoso", "Pune, India"]},
             {"url": "u2", "title": "Ravi Kumar Testname", "lines": ["Enterprise Sales", "United Kingdom",
                                                                     "Certifications: Contoso Certified Cloud"]},
             {"url": "u3", "title": "Ravi Testname", "lines": ["Technical Support Specialist at Fabrikam Inc.",
                                                               "Edmonton", "Summary: ...Contoso certified"]},
             {"url": "u4", "title": "John Doe", "lines": ["VP at Contoso", "United States"]},
             {"url": "u5", "title": "Ravi T", "lines": ["Engineer", "India", "Current: Principal PM at Contoso"]}]
    assert [i["url"] for i in P.match_candidates(items, "Contoso", "Ravi Testname")] == ["u1"]
    assert [i["url"] for i in P.match_candidates(items, "Contoso")] == ["u1", "u4", "u5"]  # current company only


def test_research_person_sends_every_accelerator():
    sent = {}

    def create(**k):
        sent["ask"] = k["messages"][0]["content"]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(_raw()))], stop_reason="end_turn")
    long = "x" * 900
    accs = [{"name": f"Acc{i:02d}", "summary": long, "problems_solved": [long] * 5, "target_industries": [long] * 6,
             "buyer_roles": [long] * 6, "use_cases": [long] * 8} for i in range(29)]
    P.research_person("A", "B", "u", "t", accs, client=SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert all(f'"Acc{i:02d}"' in sent["ask"] for i in range(29))


# ---------- optional role ----------
def test_role_matches_handles_abbreviations():
    assert P.role_matches("VP Operations", "Vice President, Operations at Acme Auto")
    assert P.role_matches("Chief Financial Officer", "CFO at Acme Auto")
    assert P.role_matches("Head of Strategic Sourcing", "Head - Strategic Sourcing & Procurement at Acme")
    assert not P.role_matches("CFO", "Head of Data at Acme Auto")
    assert P.role_matches("", "anything")  # no role given: no filter


def test_pick_by_role_breaks_a_tie():
    items = [{"url": "u1", "title": "Asha Test", "lines": ["Head of Data at Acme Auto"]},
             {"url": "u2", "title": "Asha Test", "lines": ["CFO at Acme Auto"]}]
    hits = P.match_candidates(items, "Acme Auto", "Asha Test")
    assert [i["url"] for i in P.by_role(hits, "Chief Financial Officer")] == ["u2"]
    assert [i["url"] for i in P.by_role(hits, "")] == ["u1", "u2"]


def test_person_role_auto_picks_among_namesakes(tmp_path, monkeypatch):
    s, fac, seen, _ = _session(tmp_path, monkeypatch, [CARD("a1", "Head of Data at Acme Auto"),
                                                       CARD("a2", "CFO at Acme Auto")])
    rid = s.enqueue_person("Asha Test", "Acme Auto", role="Chief Financial Officer")
    r = _until(s, fac, rid, ("ready", "choose", "failed", "not_found"))
    s.stop()
    assert r["status"] == "ready" and r["role"] == "Chief Financial Officer"
    assert seen["url"] == "https://www.linkedin.com/in/a2"


def test_research_person_includes_role():
    sent = {}

    def create(**k):
        sent["ask"] = k["messages"][0]["content"]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(_raw()))], stop_reason="end_turn")
    P.research_person("A", "B", "u", "t", [{"name": "Arjuna"}], role="VP Operations",
                      client=SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert "Role given by the user: VP Operations" in sent["ask"]


def test_api_person_start_passes_role(api):
    A, _, sess = api
    sess.enqueue_person = lambda name, company, by="", role="": sess.queued.append((name, company, by, role)) or 9
    assert A.person_start(None, {"name": "Asha", "company": "Acme", "role": " VP Ops "}) == {"report_id": 9}
    assert sess.queued[-1] == ("Asha", "Acme", "Krisha", "VP Ops")


def test_xlsx_shows_role(tmp_path):
    from openpyxl import load_workbook
    rec = {"name": "Asha Test", "company": "Acme Auto", "role": "VP Operations", "updated_at": "t",
           "linkedin_url": "u", "report": P.build_report(_raw(), ACCS)}
    ws = load_workbook(P.report_xlsx(rec, tmp_path / "r.xlsx"))["Prospect"]
    assert ("Searched as (role)", "VP Operations") in [r[:2] for r in ws.iter_rows(values_only=True)]
