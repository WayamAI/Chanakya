import json
import sqlite3

import httpx
import pytest

from chanakya.storage.db import connect


def _tr(handler):
    return httpx.MockTransport(handler)


def test_workbook_export_matches_db(tmp_path):
    from openpyxl import load_workbook
    from chanakya.outreach import workbook as WB
    con = connect(str(tmp_path / "t.db"))
    con.execute("INSERT INTO companies(domain,name,data_json,first_seen,last_seen) VALUES('a.com','=Acme','{}','t','t')")
    con.commit()
    r = WB.export(con, tmp_path / "x.xlsx")
    wb = load_workbook(tmp_path / "x.xlsx")
    assert set(WB.SHEETS) <= set(wb.sheetnames) and r["companies"] == 1
    ws = wb["Companies"]
    assert ws.cell(2, 2).value == "'=Acme"  # formula injection neutralised
    assert r["counts"]["Companies"] == con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]


def test_acronym_resolution_and_safe_merge(tmp_path):
    from chanakya.intel import entities as E
    con = connect(str(tmp_path / "a.db"))
    fca = E.resolve_company(con, name="FCA", direction=E.TOP, source="tenders")["company_id"]
    full = E.resolve_company(con, name="Financial Conduct Authority", domain="fca.org.uk", direction=E.TOP,
                             source="website")["company_id"]
    # the expansion with its domain already absorbed the unresolved acronym record, or the scan merges it
    E.auto_merge(con)
    left = con.execute("SELECT id, name, domain FROM companies").fetchall()
    assert len(left) == 1 and left[0]["domain"] == "fca.org.uk"
    assert left[0]["name"] == "Financial Conduct Authority"
    # a later "FCA" sighting resolves to the same record by acronym + domain stem
    assert E.resolve_company(con, name="FCA", direction=E.BOTTOM, source="x")["company_id"] == left[0]["id"]
    # no domain confirmation -> never merged automatically
    E.resolve_company(con, name="FCB", direction=E.TOP, source="t")
    E.resolve_company(con, name="First Citizens Bank", domain="firstcitizens.com", direction=E.TOP, source="t")
    c = [x for x in E.duplicate_candidates(con) if x["reason"] == "acronym"]
    assert c and not c[0]["safe"]
    assert fca and full


def test_ch_officers(tmp_path, monkeypatch):
    from chanakya.intel import companies_house as CH
    con = connect(str(tmp_path / "c.db"))
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "ch")
    cid = con.execute("INSERT INTO companies(domain,name,data_json,first_seen,last_seen) VALUES('w.com','W','{}','t','t')"
                      ).lastrowid

    def h(req):
        if req.url.path.endswith("/officers"):
            return httpx.Response(200, json={"items": [
                {"name": "DOE, Jane Mary", "officer_role": "director", "occupation": "Chief Executive"},
                {"name": "OLD, Bob", "officer_role": "director", "resigned_on": "2020-01-01"}]})
        return httpx.Response(200, json={"company_name": "W LTD", "company_status": "active"})
    t = httpx.MockTransport(h)
    r = CH.import_officers(con, cid, "01234567", by="t", transport=t)
    assert [o["name"] for o in r["officers"]] == ["Jane Mary Doe"]
    again = CH.import_officers(con, cid, "01234567", by="t", transport=t)
    assert again["officers"][0]["created"] is False  # dedupe


def test_clay_enrichment_email_parsing():
    from chanakya.emailhunter import clay as EC
    p = {"contacts": [{"entityId": "394", "enrichments": [{"name": "Work Email", "state": "completed",
                                                           "value": "{\"Work Email\":\"a.tester@fca.org.uk\"}",
                                                           "runnerStatus": "SUCCESS"}]}]}
    assert EC.find_email_for(p, "394")[0] == "a.tester@fca.org.uk"
    assert EC.enrichment_state(p, "394") == "completed"
    p["contacts"][0]["enrichments"][0].update(state="in-progress", value=None)
    assert EC.find_email_for(p, "394")[0] is None and EC.enrichment_state(p, "394") == "in-progress"


def test_clay_resolver_match_requires_name_and_domain():
    from chanakya.resolver.clay_resolver import match
    cs = [{"name": "Jane Roe", "domain": "fca.org.uk", "url": "https://www.linkedin.com/in/jr/"},
          {"name": "Jane Roe", "domain": "other.com", "url": "https://www.linkedin.com/in/jr2/"}]
    assert match("Ms Jane Roe", "fca.org.uk", cs)["url"].endswith("/jr/")
    assert match("Jane Roe", "bank.com", cs) is None
    assert match("John Doe", "fca.org.uk", cs) is None


def test_persona_proposals_need_a_verbatim_catalogue_quote_and_stay_pending(tmp_path):
    import json as _j
    from chanakya.people.persona_proposals import propose_missing
    con = connect(str(tmp_path / "p.db"))
    con.execute("INSERT INTO products(id,name,summary,status,verification,source_path,source_sha256,raw_json,imported_at) "
                "VALUES('PRD-X','X','Cuts unplanned downtime in plants.','active','test','t','t','{}','t')")
    con.commit()

    class LLM:
        def chat(self, msgs, **kw):
            class R:
                text = _j.dumps({"personas": [
                    {"persona": "Plant leader", "titles": ["Plant Manager"], "seniorities": ["Head"],
                     "quote": "Cuts unplanned downtime in plants."},
                    {"persona": "Invented", "titles": ["CFO"], "seniorities": ["C-suite"], "quote": "Saves 40% cost"}]})
                content = text
                model = "fake"
                usage = {}
            return R()
    out = propose_missing(con, LLM())
    props = out[0]["proposals"]
    assert any(p.get("mapping_id") for p in props) and any(p.get("dropped") for p in props)
    assert [tuple(r) for r in con.execute("SELECT status, persona_source FROM persona_role_mappings")] == [("pending", "ai_proposal")]


def test_safe_escapes_formulas_but_not_blanks():
    from chanakya.outreach.workbook import _safe
    assert _safe("") == "" and _safe(None) is None and _safe(5) == 5
    assert _safe("=SUM(A1)") == "'=SUM(A1)" and _safe("-1+2") == "'-1+2" and _safe("ok") == "ok"
