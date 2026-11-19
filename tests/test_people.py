"""Agent 3 (People Finder) groundwork. All offline: fake MCP client and a SYNTHETIC fixture; no network."""
import asyncio
import json
import pathlib

import pytest

from chanakya.catalog.models import ICP, Criterion, Evidence
from chanakya.people import CompanyRef
from chanakya.people.finder import (ClayContactsProvider, PeopleBlocked, PeopleGateway, PeopleRequest,
                                    build_contact_query, extract_people, load_company_refs, normalize_linkedin,
                                    run_people_finder, upsert_person)
from chanakya.people.personas import (NO_PERSONA_ISSUE, add_human_mapping, make_evidence, mappings_for, plan_personas,
                                      review_mapping)
from chanakya.scout.agent import SpendBlocked
from chanakya.scout.qualification import record_human_decision

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures/search_contacts_synthetic.json").read_text("utf-8"))
E = [Evidence(source_path="cat.yaml", quote="CISO", field="products[P].buyer_roles")]
EV = make_evidence("pilot planning notes, 2026-11-03")


class FakeMCP:
    def __init__(self, payload, workspace="1461095", fail_on=None):
        self.payload, self.ws, self.fail_on, self.calls = payload, workspace, fail_on, []

    async def call_tool(self, name, args):
        from types import SimpleNamespace
        self.calls.append((name, args))
        if name == self.fail_on:
            raise RuntimeError("boom")
        body = {"get-current-workspace": {"workspaceId": self.ws},
                "get-credits-available": {"hasWorkspaceCredits": True}}.get(name, self.payload)
        return SimpleNamespace(structured_content=body, content=[], is_error=False)


def _company(con, domain, qualify=True):
    con.execute("INSERT INTO companies(domain,name,data_json,first_seen,last_seen,qualification_status,"
                "qualification_source) VALUES(?,?,?,?,?,?,?)",
                (domain, domain.split(".")[0].title(), "{}", "t", "t", "needs_manual_review", "system"))
    con.commit()
    if qualify:
        record_human_decision(con, domain, "qualified", "Reviewer", "fits pilot")
    return con.execute("SELECT id FROM companies WHERE domain=?", (domain,)).fetchone()[0]


def _icp(con, roles=None):
    con.execute("INSERT INTO icps(id,version,icp_json,source_shas,created_at) VALUES(1,1,'{}','{}','t')")
    return ICP(buyer_roles=Criterion(values=roles, evidence=E) if roles else None)


def _approved_request(con, domains=("alpha.example", "beta.example"), n=1):
    icp = _icp(con)
    for persona, titles, sen in (("Security leader", ["Chief Information Security Officer"], ["C-suite"]),
                                 ("IT operations leader", ["Head of IT Operations"], None)):
        mid = add_human_mapping(con, 1, persona, titles=titles, seniorities=sen, reviewer="Reviewer",
                                reason="agreed in pilot planning", evidence=EV)
        review_mapping(con, mid, "approved", reviewer="Approver", reason="ok for pilot")
    for d in domains:
        _company(con, d)
    refs, issues = load_company_refs(con, list(domains))
    assert not issues
    return PeopleRequest(refs, plan_personas(con, 1, icp).mappings, n)


# ---- personas: never invented ----
def test_no_catalogue_persona_blocks_with_human_review(con):
    plan = plan_personas(con, 1, _icp(con))
    assert not plan.ready and plan.issues == [NO_PERSONA_ISSUE] and plan.mappings == []


def test_stated_persona_used_verbatim_once(con):
    icp = _icp(con, ["CISO"])
    plan = plan_personas(con, 1, icp)
    plan_personas(con, 1, icp)  # idempotent
    assert len(mappings_for(con, 1)) == 1
    m = plan.mappings[0]
    assert (m.persona, m.titles, m.seniorities, m.status, m.persona_source) == ("CISO", ["CISO"], [], "pending",
                                                                                "catalogue")
    assert m.evidence[0]["field"] == "products[P].buyer_roles"


def test_real_catalogue_has_no_personas(con):
    """The imported Wayam catalogue states buyer_roles for no product, so every ICP must block (checked offline)."""
    from chanakya.catalog.products import build_product_icp
    p = type("P", (), {"target_industries": ["Healthcare"], "buyer_roles": None, "evidence": [], "name": "X",
                       "id": "PRD-X", "target_industries_note": None, "caveats": []})()
    icp = build_product_icp(p, type("C", (), {"path": "cat.yaml"})())
    assert icp.buyer_roles is None and "buyer_roles" in icp.missing


def test_human_mapping_requires_reviewer_reason_and_valid_values(con):
    _icp(con)
    with pytest.raises(ValueError):
        add_human_mapping(con, 1, "X", titles=["CTO"], reviewer="", reason="r", evidence=EV)
    with pytest.raises(ValueError):
        add_human_mapping(con, 1, "X", titles=["CTO"], reviewer="a", reason=" ", evidence=EV)
    with pytest.raises(ValueError):
        add_human_mapping(con, 1, "X", titles=[], seniorities=["Supreme Leader"], reviewer="a", reason="r", evidence=EV)
    with pytest.raises(ValueError):
        add_human_mapping(con, 1, "X", titles=['CTO") or (x'], reviewer="a", reason="r", evidence=EV)
    with pytest.raises(ValueError):
        add_human_mapping(con, 1, "X", titles=[], reviewer="a", reason="r", evidence=EV)
    mid = add_human_mapping(con, 1, "Tech lead", titles=["CTO"], reviewer="a", reason="r", evidence=EV)
    m = mappings_for(con, 1)[0]
    assert (m.id, m.status, m.persona_source, m.reviewed_by) == (mid, "pending", "human", "a")


def test_rejected_mapping_not_usable(con):
    icp = _icp(con, ["CISO"])
    m = plan_personas(con, 1, icp).mappings[0]
    review_mapping(con, m.id, "rejected", reviewer="a", reason="too broad")
    plan = plan_personas(con, 1, icp)
    assert not plan.ready and "rejected" in plan.issues[0]
    with pytest.raises(ValueError):
        review_mapping(con, m.id, "stated", reviewer="a", reason="r", evidence=EV)
    with pytest.raises(ValueError):
        review_mapping(con, 999, "approved", reviewer="a", reason="r")


# ---- query ----
def test_query_uses_only_usable_mappings(con):
    req = _approved_request(con, n=2)
    review_mapping(con, req.mappings[1].id, "rejected", reviewer="a", reason="out of scope")
    req.mappings = mappings_for(con, 1)
    q = build_contact_query(req)
    assert q.ready and q.mapping_ids == [req.mappings[0].id]
    assert q.dsl == ('select from people where experiences.any(is_current = true and '
                     '((job_title is_similar_to ("Chief Information Security Officer") and seniority in ("C-suite"))))'
                     ' limit 2 by clay_company_id')
    assert "Head of IT" not in q.dsl and "company." not in q.dsl
    assert q.arguments()["companyIdentifiers"] == ["alpha.example", "beta.example"]


@pytest.mark.parametrize("n", [0, 4, "1"])
def test_query_limits(con, n):
    req = _approved_request(con)
    req.limit_per_company = n
    assert not build_contact_query(req).ready


def test_query_blocked_without_companies_or_mappings(con):
    q = build_contact_query(PeopleRequest([], []))
    assert not q.ready and len(q.issues) == 2
    many = [CompanyRef(i, f"c{i}.example", None) for i in range(11)]
    req = PeopleRequest(many, [type("M", (), {"status": "approved", "titles": ["CTO"], "seniorities": [], "id": 1})()])
    assert not build_contact_query(req).ready


# ---- input companies ----
def test_only_human_qualified_scout_companies_accepted(con):
    _company(con, "alpha.example")
    _company(con, "pending.example", qualify=False)
    refs, issues = load_company_refs(con, ["alpha.example", "pending.example", "missing.example"])
    assert [r.domain for r in refs] == ["alpha.example"]
    assert any("pending.example" in i and "human must qualify" in i for i in issues)
    assert any("missing.example" in i for i in issues)
    refs2, _ = load_company_refs(con, ["pending.example"], require_qualified=False)
    assert refs2[0].domain == "pending.example"


# ---- gateway / policy ----
@pytest.mark.parametrize("tool", ["search-companies", "add-contact-data-points", "search-contacts-by-name",
                                  "run_subroutine"])
def test_gateway_refuses_non_people_tools(tool):
    with pytest.raises(SpendBlocked):
        asyncio.run(PeopleGateway(FakeMCP({})).call(tool, {}))


def test_wrong_workspace_blocks_before_search(con):
    req = _approved_request(con)
    mcp = FakeMCP(FIXTURE, workspace="999")
    with pytest.raises(SpendBlocked):
        asyncio.run(run_people_finder(ClayContactsProvider(PeopleGateway(mcp), 1461095), req, con, allow_live=True))
    assert "search-contacts" not in [c[0] for c in mcp.calls]
    assert con.execute("SELECT status FROM runs").fetchone()[0] == "error"


# ---- runner ----
def test_dry_run_by_default_makes_no_calls(con):
    req = _approved_request(con)
    mcp = FakeMCP(FIXTURE)
    res = asyncio.run(run_people_finder(ClayContactsProvider(PeopleGateway(mcp), 1461095), req, con))
    assert res.dry_run and res.query.ready and mcp.calls == []
    assert con.execute("SELECT status FROM runs").fetchone()[0] == "dry_run"
    assert con.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0


def test_blocked_run_recorded_and_raises_when_live(con):
    _icp(con)
    req = PeopleRequest([], plan_personas(con, 1, ICP()).mappings)
    assert asyncio.run(run_people_finder(None, req, con)).dry_run
    with pytest.raises(PeopleBlocked):
        asyncio.run(run_people_finder(ClayContactsProvider(PeopleGateway(FakeMCP({})), 1), req, con, allow_live=True))
    assert [r[0] for r in con.execute("SELECT status FROM runs ORDER BY id")] == ["blocked", "blocked"]


def test_live_run_with_fixture_dedupes_and_keeps_provenance(con):
    req = _approved_request(con)
    mcp = FakeMCP(FIXTURE)
    res = asyncio.run(run_people_finder(ClayContactsProvider(PeopleGateway(mcp), 1461095), req, con, allow_live=True))
    assert [c[0] for c in mcp.calls] == ["get-current-workspace", "get-credits-available", "search-contacts"]
    assert mcp.calls[2][1]["companyIdentifiers"] == ["alpha.example", "beta.example"]
    # Asha appears twice (same LinkedIn profile, different URL forms) -> 1 person; limit 1 per company skips dup.
    assert (res.inserted, res.updated) == (2, 0)
    assert any("unrelated" in s or "Nobody" in s for s in res.skipped)
    people = con.execute("SELECT full_name, dedupe_basis, linkedin_url, verification FROM people ORDER BY id").fetchall()
    assert [tuple(p) for p in people] == [
        ("Asha Example", "linkedin_url", "https://www.linkedin.com/in/asha-example-123", "unverified"),
        ("Ravi Sample", "name_and_company", None, "unverified")]
    roles = con.execute("SELECT title, seniority, is_current, role_match_json, review_status FROM person_roles "
                        "ORDER BY id").fetchall()
    assert roles[0]["title"] == "Chief Information Security Officer" and roles[0]["is_current"] == 1
    assert json.loads(roles[0]["role_match_json"])["status"] == "matches_approved_role"
    assert {r["review_status"] for r in roles} == {"needs_manual_review"}
    raw = con.execute("SELECT provider, tool, task_id, phase, response_json FROM raw_responses").fetchall()
    assert len(raw) == 1 and raw[0]["task_id"] == "mcp-task_SYNTHETIC0001" and raw[0]["phase"] == "initial"
    assert json.loads(raw[0]["response_json"]) == FIXTURE
    src = con.execute("SELECT run_id, raw_response_id, source, tool, raw_json FROM person_sources ORDER BY id").fetchall()
    assert all(s["run_id"] == res.run_id and s["raw_response_id"] for s in src)
    assert json.loads(src[0]["raw_json"]) == FIXTURE["results"][0]  # verbatim, including past experience
    # Re-run: no new people, a new observation row each time.
    res2 = asyncio.run(run_people_finder(ClayContactsProvider(PeopleGateway(FakeMCP(FIXTURE)), 1461095),
                                         req, con, allow_live=True))
    assert (res2.inserted, res2.updated) == (0, 2)
    assert con.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM person_roles").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM person_sources").fetchone()[0] == 4


def test_failed_search_keeps_nothing_partial_but_logs(con):
    req = _approved_request(con)
    mcp = FakeMCP(FIXTURE, fail_on="search-contacts")
    with pytest.raises(RuntimeError):
        asyncio.run(run_people_finder(ClayContactsProvider(PeopleGateway(mcp), 1461095), req, con, allow_live=True))
    run = con.execute("SELECT status, detail_json FROM runs").fetchone()
    assert run["status"] == "error" and json.loads(run["detail_json"])["error_type"] == "RuntimeError"
    assert con.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0


# ---- dedupe rules ----
def test_same_name_different_companies_without_linkedin_not_merged(con):
    a, b = _company(con, "alpha.example"), _company(con, "beta.example")
    kw = dict(mappings=[], run_id=None, raw_response_id=None, tool="t", task_id=None, query="q")
    p1, _ = upsert_person(con, {"full_name": "Sam Lee", "title": "CTO"}, a, **kw)
    p2, _ = upsert_person(con, {"full_name": "Sam Lee", "title": "CTO"}, b, **kw)
    p3, new = upsert_person(con, {"full_name": "sam  lee.", "title": "CTO"}, a, **kw)
    assert p1 != p2 and p3 == p1 and not new


def test_linkedin_merges_across_companies_and_name_fallback_attaches(con):
    a, b = _company(con, "alpha.example"), _company(con, "beta.example")
    kw = dict(mappings=[], run_id=None, raw_response_id=None, tool="t", task_id=None, query="q")
    p1, _ = upsert_person(con, {"full_name": "Sam Lee", "linkedin_url": "linkedin.com/in/samlee"}, a, **kw)
    p2, new = upsert_person(con, {"full_name": "Samuel Lee", "linkedin_url": "https://uk.linkedin.com/in/SamLee/"},
                            b, **kw)
    assert p1 == p2 and not new
    p3, new = upsert_person(con, {"full_name": "Sam Lee", "title": "CTO"}, a, **kw)  # no URL, same company
    assert p3 == p1 and not new
    p4, new = upsert_person(con, {"full_name": "Sam Lee", "linkedin_url": "linkedin.com/in/other-sam"}, a, **kw)
    assert p4 != p1 and new  # different profile at same company stays separate


def test_extract_people_attribution():
    recs = extract_people(FIXTURE, ["alpha.example", "beta.example"])
    assert [r.company_domain for r in recs] == ["alpha.example", "alpha.example", "beta.example", None]
    assert extract_people({"rows": [{"full_name": "X", "title": "CTO"}]}, ["solo.example"])[0].company_domain == \
        "solo.example"
    assert extract_people({"companies": [{"name": "Acme", "domain": "acme.example"}]}, ["acme.example"]) == []
    assert normalize_linkedin("https://in.linkedin.com/in/A-b_1/?x=1") == "https://www.linkedin.com/in/a-b_1"
    assert normalize_linkedin("https://linkedin.com/company/acme") is None


def test_people_package_does_not_touch_other_agents():
    src = pathlib.Path("src/chanakya/people")
    text = " ".join(p.read_text(encoding="utf-8") for p in src.glob("*.py")).lower()
    for banned in ("browser_use", "import azure", "email"):
        assert banned not in text
