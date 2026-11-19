import json

from chanakya.linkedin import pipeline as PL
from chanakya.storage.db import connect


class FakeLLM:
    model = "fake-sonnet"

    def chat(self, msgs, json_format=True, temperature=0.0):
        from chanakya.llm.ollama_client import ChatResult
        return ChatResult("fake", json.dumps({"subject": "Legacy BI at Acme Foods",
                                       "body": "Dear Jane, as CFO at Acme Foods you see reporting costs first hand. "
                                               "Is moving older dashboards to a modern platform on your list this "
                                               "year? Would a 20-minute conversation be useful?\n\nKrisha\nWayam AI",
                                       "personalisation": ["CFO", "Acme Foods"]}), [], {}, 0.0, 0, 0)


def test_works_at_filters_strangers():
    assert PL.works_at({"current": "CFO at Acme Foods Ltd", "lines": []}, "Acme Foods")
    assert not PL.works_at({"current": "CFO at Other Corp", "lines": ["CFO | finance"]}, "Acme Foods")


def test_company_person_draft_flow(tmp_path, monkeypatch):
    con = connect(str(tmp_path / "p.db"))
    con.execute("INSERT INTO products(id,name,status,summary,verification,source_path,source_sha256,catalogue_version,"
                "raw_json,imported_at) VALUES('PRD-SETU','Setu','active','BI migration','ok','x','x','1','{}','t')")
    con.commit()
    cid = PL.add_company(con, {"title": "Acme Foods", "url": "https://www.linkedin.com/company/acme-foods",
                               "lines": ["Food Production", "Leeds"], "website": "acmefoods.co.uk",
                               "domain": "acmefoods.co.uk"}, "PRD-SETU")
    pid = PL.add_person(con, {"title": "Jane Roe", "url": "https://www.linkedin.com/in/jane-roe",
                              "current": "CFO at Acme Foods", "lines": []}, cid, "CFO Acme Foods")
    assert con.execute("SELECT title FROM person_roles WHERE person_id=?", (pid,)).fetchone()[0] == "CFO"
    monkeypatch.setattr("chanakya.llm.structured._log", lambda *a, **k: None, raising=False)
    try:
        d = PL.write_draft(con, pid, "PRD-SETU", FakeLLM(), "Krisha\nWayam AI")
    except Exception as e:  # the fake client shape may differ from the real one; surface clearly
        raise AssertionError(f"draft failed: {e}")
    assert d["status"] in ("generated", "needs_editing")
    assert PL.write_draft(con, pid, "PRD-SETU", FakeLLM(), "x")["reused"] is True


def test_is_leader():
    assert PL.LEADER_RE.search("Group CFO") and PL.LEADER_RE.search("Chief Executive Officer")
    assert not PL.LEADER_RE.search("Head of Operations")


def test_real_company_and_strict_match():
    assert not PL.real_company({"title": "Women in Manufacturing"}, "manufacturing")
    assert not PL.real_company({"title": "Medical Device Manufacturing"}, "medical device manufacturing")
    assert PL.real_company({"title": "Rheem Manufacturing Company"}, "manufacturing")
    assert not PL.works_at({"current": "Head of Manufacturing at Penta Orthodontics"}, "Medical Device Manufacturing")
    assert PL.works_at({"current": "CFO at Rheem Manufacturing"}, "Rheem Manufacturing Company")


def test_leader_at_people_tab():
    s = "STRATA Manufacturing PJSC"
    assert PL.leader_at({"lines": ["Group CFO | Experienced Finance Professional"]}, s, "CFO") == (True, False)
    assert PL.leader_at({"lines": ["CEO of Khalifa University Enterprises Company"]}, s, "CEO") == (False, False)
    assert PL.leader_at({"lines": ["Chief Executive Officer at STRATA Manufacturing"]}, s, "CEO") == (True, True)
    assert PL.leader_at({"lines": ["Retired"]}, s, "CEO") == (False, False)


def test_score_totals_are_computed_from_parts():
    from chanakya.linkedin.claude_research import score
    out = score({"website": "a.com", "ceo": {"name": "x"}, "cfo": None,
                 "fit": {"industry": 35, "problem": 30, "size_region": 15},
                 "buying": {"signals": 40, "signals_found": [{"what": "hiring", "source_url": "https://a.com/jobs"}],
                            "recency": 25}})
    assert out["match_percent"] == 80 and out["buy_percent"] == 40 + 25 + 18
    nosrc = score({"fit": {}, "buying": {"signals": 50, "signals_found": [{"what": "x"}], "recency": 25}})
    assert nosrc["buy_percent"] == 0  # unsourced signals earn nothing


def test_leader_at_rejects_comma_other_company():
    assert PL.leader_at({"lines": ["CEO, Mariko Plus Pvt Ltd"]}, "GE Capital", "CEO") == (False, False)
    assert PL.leader_at({"lines": ["Experienced Financial Services CFO"]}, "GE Capital", "CFO") == (True, False)


def test_score_multi_picks_best_accelerator():
    from chanakya.linkedin.claude_research import score_multi
    out = score_multi({"website": "a.com", "ceo": None, "cfo": None,
                       "fits": [{"accelerator": "Setu", "industry": 20, "problem": 10, "size_region": 10},
                                {"accelerator": "Samyak", "industry": 35, "problem": 30, "size_region": 15},
                                {"accelerator": "Invented", "industry": 40, "problem": 40, "size_region": 20}],
                       "buying": {"signals": 0, "signals_found": [], "recency": 0}}, ["Setu", "Samyak"])
    assert out["best_accelerator"] == "Samyak" and out["match_percent"] == 80
    assert [m["accelerator"] for m in out["matches"]] == ["Samyak", "Setu"]  # unknown names ignored


def test_clean_company_name():
    assert PL.clean_company_name("Page by Nexent Bank") == "Nexent Bank"
    assert PL.clean_company_name("Acme Showcase") == "Acme"
    assert PL.clean_company_name("Wells Fargo") == "Wells Fargo"
