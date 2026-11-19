import asyncio

import pytest

from chanakya.catalog.models import ICP, Criterion, Evidence
from chanakya.scout.agent import SpendBlocked, run_scout
from chanakya.scout.dsl import ClaySchema, build_company_query
from chanakya.scout.industry_map import map_industries, map_term, review
from tests.conftest import FakeLLM

REAL = ClaySchema.from_capabilities()
TAX = REAL.enums["industry"]
E = [Evidence(source_path="x.md", quote="some verbatim quote")]


def icp(terms):
    return ICP(industries=Criterion(values=terms, evidence=E), countries=Criterion(values=["India"], evidence=E))


def test_exact_match_no_llm_call():
    llm = FakeLLM()
    m = map_term("financial services", TAX, llm)
    assert (m.status, m.canonical, m.confidence, llm.calls) == ("exact", ["Financial Services"], 1.0, 0)


def test_high_confidence_is_only_proposed():
    m = map_term("fintech", TAX, FakeLLM({"canonical": ["Financial Services"], "confidence": 0.9,
                                           "ambiguous": False, "rationale": "fintech is financial services"}))
    assert m.status == "proposed" and m.original_term == "fintech" and m.rationale


def test_ambiguous_needs_review_and_keeps_alternatives():
    m = map_term("fintech", TAX, FakeLLM({"canonical": ["Financial Services", "Banking"], "alternatives": ["Insurance"],
                                           "confidence": 0.7, "ambiguous": True, "rationale": "spans"}))
    assert m.status == "needs_review" and m.alternatives == ["Insurance"]


def test_invented_value_discarded_and_reported():
    m = map_term("fintech", TAX, FakeLLM({"canonical": ["Fintech", "Financial Services"], "confidence": 0.95,
                                           "ambiguous": False, "rationale": "r"}))
    assert m.canonical == ["Financial Services"] and m.rejected_llm_values == ["Fintech"]
    assert m.status == "needs_review"  # invention lowers trust -> human review


def test_no_match_is_unmapped():
    m = map_term("space mining", TAX, FakeLLM({"canonical": [], "confidence": 0.1, "rationale": "none"}))
    assert m.status == "unmapped" and m.canonical == []


def test_bad_json_needs_review():
    assert map_term("fintech", TAX, FakeLLM("nope", "still nope")).status == "needs_review"


def test_pending_mapping_excluded_and_blocks_live_search(con):
    ms = map_industries(con, None, ["fintech"], TAX, FakeLLM(
        {"canonical": ["Financial Services"], "confidence": 0.9, "ambiguous": False, "rationale": "r"}))
    q = build_company_query(icp(["fintech"]), REAL, industry_mappings=ms)
    assert "industry" not in q.text and not q.ready and "fintech" in q.pending_review[0]
    with pytest.raises(SpendBlocked):
        asyncio.run(run_scout(icp(["fintech"]), None, con, object(), REAL, allow_live_search=True,
                              industry_mappings=ms))


def test_approval_enables_and_is_reused(con):
    ms = map_industries(con, None, ["fintech"], TAX, FakeLLM(
        {"canonical": ["Financial Services"], "confidence": 0.7, "ambiguous": True, "rationale": "r"}))
    review(con, ms[0].id, "approved", "reviewer", canonical=["Financial Services", "Banking"], taxonomy=TAX)
    llm = FakeLLM()
    ms2 = map_industries(con, None, ["Fintech"], TAX, llm)  # decision reused, no LLM call
    assert llm.calls == 0 and ms2[0].status == "approved"
    q = build_company_query(icp(["fintech"]), REAL, industry_mappings=ms2)
    assert 'industry in ("Financial Services", "Banking")' in q.text and q.ready


def test_review_rejects_non_taxonomy_values(con):
    ms = map_industries(con, None, ["fintech"], TAX, FakeLLM({"canonical": [], "confidence": 0}))
    with pytest.raises(ValueError):
        review(con, ms[0].id, "approved", "r", canonical=["Fintech"], taxonomy=TAX)


def test_all_pending_gives_blocked_query_not_unfiltered(con):
    ms = map_industries(con, None, ["Public Sector"], TAX, FakeLLM(
        {"canonical": ["Government Administration"], "confidence": 0.8, "ambiguous": True, "rationale": "r"}))
    only_ind = ICP(industries=Criterion(values=["Public Sector"], evidence=E))
    q = build_company_query(only_ind, REAL, industry_mappings=ms)
    assert q.text == "" and not q.ready
    with pytest.raises(SpendBlocked):
        asyncio.run(run_scout(only_ind, None, con, object(), REAL, allow_live_search=True, industry_mappings=ms))


def test_same_term_mapped_once_across_icps(con):
    llm = FakeLLM({"canonical": ["Government Administration"], "confidence": 0.8, "ambiguous": True, "rationale": "r"})
    a = map_industries(con, None, ["Public Sector"], TAX, llm)
    b = map_industries(con, None, ["public sector"], TAX, llm)
    assert llm.calls == 1 and a[0].id == b[0].id


def test_invalid_json_retried_once():
    m = map_term("fintech", TAX, FakeLLM("bad", {"canonical": ["Financial Services"], "confidence": 0.9,
                                                 "ambiguous": False, "rationale": "r"}))
    assert m.status == "proposed"


def test_no_llm_unmapped_is_retried_when_llm_available(con):
    first = map_industries(con, None, ["Healthcare"], TAX, None)
    assert first[0].status == "unmapped" and first[0].method == "none"
    llm = FakeLLM({"canonical": ["Hospitals and Health Care"], "confidence": 0.9, "ambiguous": False, "rationale": "r"})
    second = map_industries(con, None, ["Healthcare"], TAX, llm)
    assert llm.calls == 1 and second[0].status == "proposed"
