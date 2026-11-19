import hashlib
import json
import os
from pathlib import Path

import pytest

from chanakya.catalog.products import (CatalogueError, build_product_icp, load_catalogue, run_catalog_yaml)
from tests.conftest import FakeLLM

REAL = Path(os.getenv("WAYAM_PRODUCTS_YAML", r"C:/Users/LOQ/Desktop/MCP-CHANAKYA/knowledge/wayam_products.yaml"))

FIXTURE = """
catalogue_version: "t1"
company: Test Co
unknown_marker: unknown
products:
  - id: P-A
    name: Alpha
    status: existing_offering
    capability_ids: [CAP-1]
    summary: "Live console for compliance and security with a live risk register."
    problems_solved: unknown
    target_industries: {stated: [Healthcare, Public Sector], note: "Listed on site."}
    buyer_roles: unknown
    integrations: unknown
    deployment: unknown
    differentiators: unknown
    caveats: ["verify frameworks"]
    evidence: [{url: "https://example.test/a", supports: x, product_specific: true}]
    review: {status: unreviewed}
  - id: P-B
    name: Beta
    status: mentioned_only
    summary: unknown
    target_industries: {stated: [], note: "the description concerns solar plants"}
    buyer_roles: unknown
"""


@pytest.fixture
def yfile(tmp_path):
    p = tmp_path / "products.yaml"
    p.write_text(FIXTURE, encoding="utf-8")
    return p


def test_unknown_marker_becomes_none_and_missing_reported(yfile):
    cat = load_catalogue(yfile)
    a, b = cat.products
    assert a.buyer_roles is None and a.target_industries == ["Healthcare", "Public Sector"]
    assert b.summary is None and b.target_industries == []
    assert set(b.missing_fields()) >= {"summary", "buyer_roles", "target_industries"}
    assert all(p.verification == "unverified" for p in cat.products)
    assert cat.unresolved_refs  # capability KB not provided


def test_note_is_never_a_criterion(yfile):
    cat = load_catalogue(yfile)
    icp_b = build_product_icp(cat.products[1], cat)
    assert icp_b.industries is None and "industries" in icp_b.missing
    assert any("solar" in n for n in icp_b.notes)


def test_icp_provenance_and_unverified(yfile):
    cat = load_catalogue(yfile)
    icp = build_product_icp(cat.products[0], cat)
    ev = icp.industries.evidence[0]
    assert ev.field == "products[P-A].target_industries.stated" and ev.url == "https://example.test/a"
    assert icp.verification == "unverified" and icp.buyer_roles is None
    assert {"countries", "buyer_roles", "employee_range"} <= set(icp.missing)


def test_run_stores_products_needs_and_only_searchable_icps(yfile, con):
    llm = FakeLLM({"products": {
        "P-A": [{"need": "compliance readiness", "quote": "compliance and security"},
                {"need": "invented ROI", "quote": "cuts costs by 40%"}],
        "P-ZZ": [{"need": "x", "quote": "y"}]}})
    r = run_catalog_yaml(llm, con, yfile)
    assert list(r.icps) == ["P-A"]  # Beta has no stated criteria -> no ICP
    row = con.execute("SELECT * FROM products WHERE id='P-A'").fetchone()
    needs = json.loads(row["needs_json"])
    assert [n["need"] for n in needs] == ["compliance readiness"] and needs[0]["verification"] == "unverified"
    assert row["verification"] == "unverified" and row["source_sha256"] == r.catalogue.sha256
    assert json.loads(row["buyer_roles_json"]) is None
    assert any("40%" in d for d in r.dropped_needs) and any("P-ZZ" in d for d in r.dropped_needs)
    run_catalog_yaml(FakeLLM({"products": {}}), con, yfile)  # re-import: upsert, no duplicates
    assert con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


def test_malformed_catalogue(tmp_path, con):
    p = tmp_path / "bad.yaml"
    p.write_text("products:\n  - {name: NoId}\n", encoding="utf-8")
    with pytest.raises(CatalogueError):
        run_catalog_yaml(None, con, p)
    p.write_text("products:\n  - {id: X, name: A}\n  - {id: X, name: B}\n", encoding="utf-8")
    with pytest.raises(CatalogueError):
        load_catalogue(p)


@pytest.mark.skipif(not REAL.exists(), reason="provisional Wayam catalogue not present")
def test_real_catalogue_read_only(con):
    before = hashlib.sha256(REAL.read_bytes()).hexdigest()
    r = run_catalog_yaml(None, con, REAL)
    assert hashlib.sha256(REAL.read_bytes()).hexdigest() == before  # never modified
    assert len(r.catalogue.products) == 25
    assert sorted(r.icps) == ["PRD-RAKSHA", "PRD-SUTRADHAR", "PRD-VEDHA"]
    assert all(p.buyer_roles is None for p in r.catalogue.products)


def test_needs_batch_failure_falls_back_per_product(yfile, con):
    # batch call fails twice, then per-product call succeeds
    llm = FakeLLM("bad", "bad", {"products": {"P-A": [{"need": "n", "kind": "capability", "quote": "live risk register"}]}})
    r = run_catalog_yaml(llm, con, yfile)
    assert r.catalogue.products[0].needs[0]["quote"] == "live risk register" and r.dropped_needs == []


def test_needs_total_failure_reported_not_fatal(yfile, con):
    r = run_catalog_yaml(FakeLLM("bad", "bad", "bad", "bad"), con, yfile)
    assert r.catalogue.products[0].needs == [] and any("P-A" in d for d in r.dropped_needs)


def test_reimport_identical_is_idempotent(yfile, con):
    llm = FakeLLM({"products": {"P-A": [{"need": "n", "kind": "capability", "quote": "live risk register"}]}})
    r1 = run_catalog_yaml(llm, con, yfile)
    snap = [tuple(r) for r in con.execute("SELECT id, imported_at, needs_json FROM products ORDER BY id")]
    llm2 = FakeLLM()  # any call would raise IndexError
    r2 = run_catalog_yaml(llm2, con, yfile)
    assert llm2.calls == 0
    assert r1.stats["icps_created"] == 1 and r2.stats["icps_created"] == 0 and r2.stats["icps_reused"] == 1
    assert r2.icps["P-A"][0] == r1.icps["P-A"][0]
    assert r2.stats["unchanged"] == 2 and r2.stats["inserted"] == r2.stats["updated"] == 0
    assert con.execute("SELECT COUNT(*) FROM icps").fetchone()[0] == 1
    assert [tuple(r) for r in con.execute("SELECT id, imported_at, needs_json FROM products ORDER BY id")] == snap
    assert {r[0] for r in con.execute("SELECT verification FROM products")} == {"unverified"}


def test_changed_summary_reanalyses_only_that_product(yfile, con):
    run_catalog_yaml(FakeLLM({"products": {"P-A": [{"need": "n", "kind": "capability", "quote": "live risk register"}]}}),
                     con, yfile)
    yfile.write_text(yfile.read_text(encoding="utf-8").replace("live risk register", "live risk ledger"), encoding="utf-8")
    llm = FakeLLM({"products": {"P-A": [{"need": "n2", "kind": "capability", "quote": "live risk ledger"}]}})
    r = run_catalog_yaml(llm, con, yfile)
    assert llm.calls == 1 and r.stats["updated"] == 2  # file hash changed -> provenance refreshed on both
    assert r.catalogue.products[0].needs[0]["quote"] == "live risk ledger"
    assert r.stats["icps_created"] == 1  # new source hash -> new ICP version, old one kept for provenance


@pytest.mark.parametrize("n,quote,kind", [
    ({"kind": "stated_problem", "problem_phrase": "alert noise"}, "compress alert noise into incidents", "stated_problem"),
    ({"kind": "stated_problem", "problem_phrase": "predictive maintenance"}, "predictive maintenance", "capability"),
    ({"kind": "stated_problem"}, "threat detection", "capability"),
    ({"kind": "stated_problem", "problem_phrase": "outages"}, "threat detection", "capability"),  # phrase not in quote
    ({"kind": "capability"}, "alert noise", "capability"),
])
def test_classify_need(n, quote, kind):
    from chanakya.catalog.products import classify_need
    assert classify_need(n, quote)[0] == kind


def test_no_llm_import_then_llm_import_analyses_needs(yfile, con):
    run_catalog_yaml(None, con, yfile)
    run_catalog_yaml(None, con, yfile)  # still no LLM: must stay un-analysed
    llm = FakeLLM({"products": {"P-A": [{"need": "n", "kind": "capability", "quote": "live risk register"}]}})
    r = run_catalog_yaml(llm, con, yfile)
    assert llm.calls == 1 and r.catalogue.products[0].needs[0]["quote"] == "live risk register"
    assert r.stats["icps_created"] == 0  # ICP content unaffected by needs


@pytest.mark.skipif(not REAL.exists(), reason="provisional Wayam catalogue not present")
def test_real_catalogue_double_import_no_duplicates(con):
    a = run_catalog_yaml(None, con, REAL)
    b = run_catalog_yaml(None, con, REAL)
    assert b.stats["icps_created"] == 0 and b.stats["unchanged"] == 25
    assert {k: v[0] for k, v in a.icps.items()} == {k: v[0] for k, v in b.icps.items()}
    assert con.execute("SELECT COUNT(*), COUNT(DISTINCT id) FROM products").fetchone()[:] == (25, 25)
    assert con.execute("SELECT COUNT(*), COUNT(DISTINCT content_sha256) FROM icps").fetchone()[:] == (3, 3)
