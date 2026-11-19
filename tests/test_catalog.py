import pytest

from chanakya.catalog.agent import NoOfferingSources, load_sources, run_catalog
from tests.conftest import FakeLLM


def ev(p, q):
    return [{"source_path": p, "quote": q}]


def test_no_sources_stops(tmp_path, con):
    with pytest.raises(NoOfferingSources):
        run_catalog(FakeLLM(), con, tmp_path)
    assert con.execute("SELECT status FROM runs").fetchone()[0] == "error"


def test_grounded_extraction_and_hallucination_dropped(offerings_dir, con):
    p = load_sources(offerings_dir)[0].path
    llm = FakeLLM(
        {"offerings": [
            {"name": "Acme Assist", "description": "support automation", "problems_solved": ["ticket backlog"],
             "evidence": ev(p, "support automation product that reduces ticket backlog")},
            {"name": "Invented CRM", "description": "x", "evidence": ev(p, "a CRM for enterprise banks")}]},
        {"industries": {"values": ["Financial Services"], "evidence": ev(p, "We sell to fintech companies in India")},
         "countries": {"values": ["India"], "evidence": ev(p, "fintech companies in India")},
         "buyer_roles": {"values": ["VP Customer Support"], "evidence": ev(p, "Typical buyer: VP Customer Support")},
         "keywords": {"values": ["lending"], "evidence": ev(p, "we love lending companies")},  # fabricated quote
         "employee_min": 100, "employee_max": 800, "employee_evidence": ev(p, "with 100 to 800 employees")})
    r = run_catalog(llm, con, offerings_dir)
    assert [o.name for o in r.offerings] == ["Acme Assist"]
    assert any("Invented CRM" in d for d in r.dropped) and any("keywords" in d for d in r.dropped)
    assert r.icp.countries.values == ["India"] and r.icp.employee_min == 100
    assert set(r.icp.missing) == {"keywords", "disqualifiers"}
    assert con.execute("SELECT COUNT(*) FROM offerings").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM icps").fetchone()[0] == 1


def test_size_without_evidence_dropped(offerings_dir, con):
    p = load_sources(offerings_dir)[0].path
    llm = FakeLLM({"offerings": [{"name": "Acme Assist", "description": "d",
                                  "evidence": ev(p, "Acme Assist is a support automation")}]},
                  {"employee_min": 50, "employee_max": 5000})
    r = run_catalog(llm, con, offerings_dir)
    assert r.icp.employee_min is None and "employee_range" in r.icp.missing


def test_invalid_json_retried_then_fails(offerings_dir, con):
    with pytest.raises(ValueError):
        run_catalog(FakeLLM("not json", "still not json"), con, offerings_dir)
