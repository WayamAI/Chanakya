import asyncio
import json

import httpx
import pytest

from chanakya.catalog.models import ICP, Criterion, Evidence
from chanakya.clay import policy
from chanakya.scout.agent import ClayGateway, ClaySearchProvider, SpendBlocked, run_discovery
from chanakya.scout.discovery import DiscoveryBlocked, DiscoveryProvider, DiscoveryRequest
from chanakya.scout.dsl import ClaySchema
from chanakya.scout.providers_wikidata import WikidataMappings, WikidataProvider, build_sparql, propose_qids

E = [Evidence(source_path="x", quote="some verbatim quote")]
ICP1 = ICP(industries=Criterion(values=["Healthcare"], evidence=E), countries=Criterion(values=["India"], evidence=E))
APPROVED = WikidataMappings({"industries": {"Healthcare": {"qids": ["Q111"], "approved_by": "rev"}},
                             "countries": {"India": {"qids": ["Q222"], "approved_by": "rev"}}})


def test_providers_satisfy_interface():
    assert isinstance(ClaySearchProvider(None, ClaySchema.from_capabilities()), DiscoveryProvider)
    assert isinstance(WikidataProvider(APPROVED), DiscoveryProvider)


def test_scout_cannot_call_people_tools():
    assert "search-contacts" not in policy.COMPANY_SCOUT and "search-contacts" in policy.PEOPLE_FINDER
    with pytest.raises(SpendBlocked):
        asyncio.run(ClayGateway(object()).call("search-contacts", {}))


def test_scout_package_does_not_import_people():
    import pathlib
    src = pathlib.Path("src/chanakya/scout")
    assert not any("chanakya.people" in p.read_text(encoding="utf-8") for p in src.glob("*.py"))


def test_wikidata_blocked_without_approved_qids():
    unapproved = WikidataMappings({"industries": {"Healthcare": {"qids": ["Q111"]}}})  # no approved_by
    p = WikidataProvider(unapproved).prepare(DiscoveryRequest(ICP1, None, 1))
    assert not p.ready and p.text == "" and any("Healthcare" in i for i in p.issues)


def test_sparql_shape_and_qid_validation():
    q = build_sparql(["Q111"], ["Q222"], 100, None, 1)
    assert "VALUES ?industry { wd:Q111 }" in q and "VALUES ?country { wd:Q222 }" in q
    assert "?item wdt:P17 ?country ." in q and "OPTIONAL { ?item wdt:P17" not in q
    assert "?employees >= 100" in q and q.rstrip().endswith("LIMIT 1")
    with pytest.raises(ValueError):
        build_sparql(["Q1 } . ?x ?y ?z"], [], None, None, 1)  # injection attempt


def _wd_transport(rows, status=200):
    def h(req):
        assert req.url.host == "query.wikidata.org"
        return httpx.Response(status, json={"results": {"bindings": rows}})
    return httpx.MockTransport(h)


def b(item, label, site, emp=None):
    row = {"item": {"value": f"http://www.wikidata.org/entity/{item}"}, "itemLabel": {"value": label},
           "website": {"value": site}, "industryLabel": {"value": "health care"}, "countryLabel": {"value": "India"}}
    if emp: row["employees"] = {"value": str(emp)}
    return row


def test_wikidata_discovery_persists_with_provenance(con):
    rows = [b("Q1", "Acme Health", "https://www.acmehealth.in/", 300), b("Q1", "Acme Health", "https://acmehealth.in"),
            b("Q2", "NoSite", "not a url")]
    http = httpx.AsyncClient(transport=_wd_transport(rows))
    req = DiscoveryRequest(ICP1, None, 1)
    r = asyncio.run(run_discovery(WikidataProvider(APPROVED, http), req, con, allow_live=True))
    assert r.provider == "wikidata" and r.inserted == 1
    row = con.execute("SELECT s.source, s.tool, s.source_url, c.domain FROM company_sources s "
                      "JOIN companies c ON c.id=s.company_id").fetchone()
    assert tuple(row) == ("wikidata", "wikidata-sparql", "http://www.wikidata.org/entity/Q1", "acmehealth.in")


def test_wikidata_rate_limit_blocked(con):
    http = httpx.AsyncClient(transport=_wd_transport([], status=429))
    with pytest.raises(DiscoveryBlocked):
        asyncio.run(run_discovery(WikidataProvider(APPROVED, http), DiscoveryRequest(ICP1, None, 1), con,
                                  allow_live=True))
    assert con.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0


def test_dry_run_makes_no_http_calls(con):
    def h(req): raise AssertionError("network call in dry run")
    http = httpx.AsyncClient(transport=httpx.MockTransport(h))
    r = asyncio.run(run_discovery(WikidataProvider(APPROVED, http), DiscoveryRequest(ICP1, None, 1), con))
    assert r.dry_run and "wd:Q111" in r.query


def test_propose_qids_are_suggestions_only():
    def h(req):
        return httpx.Response(200, json={"search": [{"id": "Q31207", "label": "health care", "description": "d"},
                                                    {"id": "P31", "label": "not an item"}]})
    out = asyncio.run(propose_qids("healthcare", httpx.AsyncClient(transport=httpx.MockTransport(h))))
    assert out == [{"qid": "Q31207", "label": "health care", "description": "d"}]


def test_clay_provider_flags_paid_risk_filters():
    from chanakya.scout import agent
    assert agent._PAID_RISK.search('select from companies where people.exists(job_title = "x")')
    assert not agent._PAID_RISK.search('select from companies where industry in ("Banking") limit 1')
