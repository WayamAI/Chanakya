import asyncio
from types import SimpleNamespace

import pytest

from chanakya.catalog.models import ICP, Criterion, Evidence
from chanakya.scout.agent import (ClayGateway, ClayToolError, SpendBlocked, normalize_domain, result_payload,
                                  run_scout)
from chanakya.scout.dsl import ClaySchema, DSLError, build_company_query

E = [Evidence(source_path="x.md", quote="some verbatim quote")]
REAL = ClaySchema.from_capabilities()  # parsed from the real tools/list output


def icp(**kw):
    base = dict(industries=Criterion(values=["Financial Services"], evidence=E),
                countries=Criterion(values=["India"], evidence=E),
                employee_min=100, employee_max=800, employee_evidence=E)
    base.update(kw)
    return ICP(**base)


def test_dsl_uses_real_schema_and_caps_limit():
    q = build_company_query(icp(industries=Criterion(values=["Financial Services", "Fintech Wizardry"], evidence=E)), REAL)
    assert q.text.startswith("select from companies where ")
    assert 'industry in ("Financial Services")' in q.text
    assert 'locations.any(country_name in ("India"))' in q.text
    assert "estimated_employee_count >= 100" in q.text and q.text.endswith("limit 5")
    assert any("Fintech Wizardry" in u for u in q.unmapped)
    with pytest.raises(DSLError):
        build_company_query(icp(), REAL, limit=6)
    with pytest.raises(DSLError):
        build_company_query(ICP(), REAL)  # never an unfiltered search


def test_dsl_escapes_quotes():
    q = build_company_query(icp(keywords=Criterion(values=['a"b'], evidence=E)), REAL)
    assert 'description contains ("a\\"b")' in q.text


@pytest.mark.parametrize("v,exp", [("https://www.PaySetu.in/about", "paysetu.in"), ("paysetu.in", "paysetu.in"),
                                   ("not a domain", None), (None, None)])
def test_normalize_domain(v, exp):
    assert normalize_domain(v) == exp


class FakeClay:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append(name)
        r = self.responses[name]
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(is_error=False, structured_content=r, content=[])


def test_gateway_blocks_held_and_unknown_tools():
    g = ClayGateway(FakeClay({}))
    for t in ["add-company-data-points", "run_subroutine_direct", "search-contacts-by-name", "made-up"]:
        with pytest.raises(SpendBlocked):
            asyncio.run(g.call(t, {}))


def test_dry_run_makes_no_clay_calls(con):
    fc = FakeClay({})
    r = asyncio.run(run_scout(icp(), None, con, ClayGateway(fc), REAL))
    assert r.dry_run and fc.calls == []


def test_credit_exhausted_blocks_search(con):
    fc = FakeClay({"get-credits-available": {"hasWorkspaceCredits": False, "hasSalesRepCredits": True}})
    with pytest.raises(SpendBlocked):
        asyncio.run(run_scout(icp(), None, con, ClayGateway(fc), REAL, allow_live_search=True))
    assert "search-companies" not in fc.calls
    assert con.execute("SELECT status FROM runs").fetchone()[0] == "error"


SEARCH = {"taskId": "mcp-task-1", "hasMore": True, "results": [
    {"name": "PaySetu", "domain": "https://www.paysetu.in", "industry": "Financial Services", "country": "India",
     "estimated_employee_count": 420, "linkedin_url": "https://www.linkedin.com/company/paysetu"},
    {"name": "PaySetu dup", "domain": "paysetu.in"},
    {"name": "BigBank", "domain": "bigbank.com", "industry": "Banking", "country": "India",
     "estimated_employee_count": 9000},
    {"name": "NoData", "domain": "nodata.io"}]}


def test_live_flow_dedupe_provenance_fit(con):
    fc = FakeClay({"get-credits-available": {"hasWorkspaceCredits": True, "hasSalesRepCredits": True},
                   "search-companies": SEARCH})
    con.execute("INSERT INTO icps(id,version,icp_json,source_shas,created_at) VALUES(1,1,'{}','{}','t')")
    r = asyncio.run(run_scout(icp(), 1, con, ClayGateway(fc), REAL, allow_live_search=True))
    assert fc.calls == ["get-credits-available", "search-companies"]
    assert r.inserted == 3 and len(r.skipped) == 1 and r.task_id == "mcp-task-1"
    fits = {c["domain"]: c["search_filter_match"] for c in r.companies}
    assert fits["paysetu.in"]["score"] == 100 and fits["bigbank.com"]["score"] < 100
    assert fits["nodata.io"]["score"] is None
    assert set(fits["nodata.io"]["unknown"]) == {"industry", "country", "employee_count"}
    src = con.execute("SELECT source_url, clay_task_id, tool FROM company_sources s JOIN companies c "
                      "ON c.id=s.company_id WHERE c.domain='paysetu.in'").fetchone()
    assert tuple(src) == ("https://www.linkedin.com/company/paysetu", "mcp-task-1", "search-companies")
    # second run: no duplicate company rows, but a new provenance row per observation
    r2 = asyncio.run(run_scout(icp(), 1, con, ClayGateway(fc), REAL, allow_live_search=True))
    assert r2.inserted == 0 and r2.updated == 3
    assert con.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM company_sources").fetchone()[0] == 6


def test_async_search_polls_get_task(con):
    fc = FakeClay({"get-credits-available": {"hasWorkspaceCredits": True, "hasSalesRepCredits": True},
                   "search-companies": {"taskId": "mcp-task-9", "status": "running"},
                   "get-task": {"taskId": "mcp-task-9", "results": SEARCH["results"][:1]}})
    r = asyncio.run(run_scout(icp(), None, con, ClayGateway(fc), REAL, allow_live_search=True))
    assert fc.calls[-1] == "get-task" and r.inserted == 1


def test_tool_failure_rolls_back_and_logs(con):
    fc = FakeClay({"get-credits-available": {"hasWorkspaceCredits": True, "hasSalesRepCredits": True},
                   "search-companies": RuntimeError("503 upstream")})
    with pytest.raises(RuntimeError):
        asyncio.run(run_scout(icp(), None, con, ClayGateway(fc), REAL, allow_live_search=True))
    assert con.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
    assert "503" in con.execute("SELECT detail_json FROM runs").fetchone()[0]


def test_is_error_result_raises():
    with pytest.raises(ClayToolError):
        result_payload(SimpleNamespace(is_error=True, content=[SimpleNamespace(text="bad dsl")]))


def test_results_read_via_get_task_context_first(con):
    fc = FakeClay({"get-credits-available": {"hasWorkspaceCredits": True, "hasSalesRepCredits": True},
                   "search-companies": {"taskId": "mcp-task-7"},
                   "get-task-context": {"taskId": "mcp-task-7", "results": SEARCH["results"][:1]}})
    r = asyncio.run(run_scout(icp(), None, con, ClayGateway(fc), REAL, allow_live_search=True))
    assert fc.calls == ["get-credits-available", "search-companies", "get-task-context"] and r.inserted == 1


def test_workspace_pin_blocks_wrong_workspace(con):
    from chanakya.scout.agent import ClaySearchProvider, run_discovery
    from chanakya.scout.discovery import DiscoveryRequest
    fc = FakeClay({"get-current-workspace": {"workspaceId": 999},
                   "get-credits-available": {"hasWorkspaceCredits": True, "hasSalesRepCredits": True},
                   "search-companies": SEARCH})
    prov = ClaySearchProvider(ClayGateway(fc), REAL, expected_workspace_id=1461095)
    with pytest.raises(SpendBlocked):
        asyncio.run(run_discovery(prov, DiscoveryRequest(icp(), None, 1), con, allow_live=True))
    assert "search-companies" not in fc.calls


def test_workspace_pin_allows_expected_workspace(con):
    from chanakya.scout.agent import ClaySearchProvider, run_discovery
    from chanakya.scout.discovery import DiscoveryRequest
    fc = FakeClay({"get-current-workspace": {"workspaceId": 1461095},
                   "get-credits-available": {"hasWorkspaceCredits": True, "hasSalesRepCredits": True},
                   "search-companies": SEARCH})
    prov = ClaySearchProvider(ClayGateway(fc), REAL, expected_workspace_id="1461095")
    r = asyncio.run(run_discovery(prov, DiscoveryRequest(icp(), None, 1), con, allow_live=True))
    assert fc.calls[:3] == ["get-current-workspace", "get-credits-available", "search-companies"] and r.inserted == 1


@pytest.mark.parametrize("q,risky", [
    ('select from companies where industry in ("Government Administration") limit 1', False),
    ('select from companies where locations.any(country_name in ("India")) limit 1', False),
    ('select from companies where people.exists(job_title = "CIO") limit 1', True),
    ('select from companies where jobs.count(job_still_open = true) > 3', True),
    ('select from people where experiences.any(company.industry = "Banking")', True),
])
def test_cross_entity_detection(q, risky):
    from chanakya.scout import agent
    assert bool(agent._PAID_RISK.search(q)) is risky


def test_generated_queries_are_company_only():
    from chanakya.scout import agent
    for kw in [dict(), dict(keywords=Criterion(values=["payments"], evidence=E))]:
        q = build_company_query(icp(**kw), REAL)
        assert q.text and not agent._PAID_RISK.search(q.text)


def test_fit_uses_approved_mapping_values():
    from chanakya.scout.agent import assess_fit
    from chanakya.scout.industry_map import IndustryMapping
    rec = {"industry": "Government Administration", "country": "India"}
    i = ICP(industries=Criterion(values=["Public Sector"], evidence=E), countries=Criterion(values=["India"], evidence=E))
    assert assess_fit(rec, i)["score"] == 50  # raw term only: no match
    ok = IndustryMapping("Public Sector", ["Government Administration"], "approved", "llm", 0.75, "r")
    pend = IndustryMapping("Public Sector", ["Government Administration"], "needs_review", "llm", 0.75, "r")
    from chanakya.scout.agent import approved_industry_values
    assert assess_fit(rec, i, approved_industry_values([ok]))["score"] == 100
    assert assess_fit(rec, i, approved_industry_values([pend]))["score"] == 50  # unapproved never counts
