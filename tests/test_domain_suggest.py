"""Wikidata website suggestions: parsing, ordering, error surfacing, API is suggestion-only (no writes)."""
import httpx
import pytest

from chanakya.intel import domain_suggest as D


def transport(search, entities, status=200):
    def h(req):
        if status != 200:
            return httpx.Response(status)
        if req.url.params.get("action") == "wbsearchentities":
            return httpx.Response(200, json={"search": search})
        return httpx.Response(200, json={"entities": entities})
    return httpx.Client(transport=httpx.MockTransport(h))


def site(url):
    return {"claims": {"P856": [{"mainsnak": {"datavalue": {"value": url}}}]}}


def test_returns_only_candidates_with_official_website_in_rank_order():
    http = transport([{"id": "Q1", "label": "A", "description": "town"}, {"id": "Q2", "label": "A Corp"}],
                     {"Q1": {"claims": {}}, "Q2": site("https://www.acorp.example/en")})
    rows = D.suggest_domains("A", http=http)
    assert [(r["domain"], r["qid"], r["search_rank"]) for r in rows] == [("acorp.example", "Q2", 2)]
    assert rows[0]["source_url"] == "https://www.wikidata.org/wiki/Q2" and "human" in rows[0]["status"]


def test_weak_matches_carry_warnings():
    http = transport([{"id": "Q9", "label": "Omega Healthcare Investors"}], {"Q9": site("https://omegahealthcare.com")})
    w = D.suggest_domains("Omega Healthcare", http=http)[0]["warnings"]
    assert any("no description" in x for x in w)
    http = transport([{"id": "Q8", "label": "Fractal (video game)", "description": "2010 game"}],
                     {"Q8": site("https://cipherprime.com")})
    assert D.suggest_domains("Fractal Analytics", http=http)[0]["warnings"]


def test_no_hits_and_blank_name():
    assert D.suggest_domains("zzz", http=transport([], {})) == []
    assert D.suggest_domains("  ") == []


def test_http_failure_is_raised_not_swallowed():
    with pytest.raises(D.SuggestError, match="403"):
        D.suggest_domains("A", http=transport([], {}, status=403))
