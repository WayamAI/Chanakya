"""Demonstration: Offering DB -> Needs Analyzer -> ICP Builder -> Company Scout -> SQLite.

Safe by construction:
  * Uses a separate demo database (default data/demo/chanakya_demo.db), never the working DB.
  * NO Clay or Wikidata calls. Real providers only `prepare` (dry run) their queries.
  * The persistence step uses SyntheticFixtureProvider: 3 obviously fake records on the reserved
    `.example` TLD, stored with provider='synthetic-fixture'. They are demo data, not research.
  * `--demo-approve` approves industry mappings ONLY inside the demo DB, as reviewer 'demo-only'.

Usage:
  python scripts/demo_pipeline.py <wayam_products.yaml> [--no-llm] [--demo-approve] [--db PATH]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chanakya.catalog.products import run_catalog_yaml  # noqa: E402
from chanakya.scout.agent import ClaySearchProvider, run_discovery  # noqa: E402
from chanakya.scout.discovery import (CompanyCandidate, DiscoveryRequest, DiscoveryResult,  # noqa: E402
                                      PreparedQuery)
from chanakya.scout.dsl import ClaySchema  # noqa: E402
from chanakya.scout.industry_map import map_industries, review  # noqa: E402
from chanakya.scout.providers_wikidata import WikidataMappings, WikidataProvider  # noqa: E402
from chanakya.storage.db import connect  # noqa: E402


class SyntheticFixtureProvider:
    """Demo-only provider. Returns fixed fake companies; makes no network calls."""
    name = "synthetic-fixture"
    cost_note = "No external calls. Synthetic demo data."

    def prepare(self, req: DiscoveryRequest) -> PreparedQuery:
        return PreparedQuery(self.name, "<fixture: returns 3 synthetic records>", ready=True)

    async def discover(self, prepared, req) -> DiscoveryResult:
        ind = req.icp.industries.values[0] if req.icp.industries else None
        recs = [
            {"name": "Demo Gov Agency A (SYNTHETIC)", "domain": "demo-agency-a.example", "industry": ind,
             "estimated_employee_count": 1200, "synthetic": True},
            {"name": "Demo Gov Agency A duplicate (SYNTHETIC)", "domain": "https://www.demo-agency-a.example/",
             "synthetic": True},
            {"name": "Demo Unknowns B (SYNTHETIC)", "domain": "demo-b.example", "synthetic": True},
        ]
        from chanakya.scout.agent import record_domain
        return DiscoveryResult(prepared, [CompanyCandidate(record_domain(r), r["name"], r, self.name, "fixture",
                                                           f"https://{record_domain(r)}") for r in recs])


def hdr(t):
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


def counts(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("products", "icps", "industry_mappings", "companies", "company_sources", "runs")}


async def main(a) -> None:
    db = Path(a.db)
    if db.exists():
        db.unlink()  # demo DB only (path printed below); the working DB is never touched
    con = connect(db)
    print(f"Demo DB: {db}  (fresh)")
    llm = None
    if not a.no_llm:
        from chanakya.config import load_settings
        from chanakya.llm.ollama_client import OllamaClient
        s = load_settings()
        llm = OllamaClient(s)
        print(f"LLM: {s.ollama_model} (fallback {s.ollama_fallback_model})")
    else:
        print("LLM: disabled (--no-llm): needs analysis and non-exact industry mapping are skipped")

    hdr("1. OFFERING DB: import provisional catalogue (read-only, unverified)")
    r = run_catalog_yaml(llm, con, a.catalogue)
    cat = r.catalogue
    print(f"source={cat.path}\nsha256={cat.sha256}\nversion={cat.version}  products={len(cat.products)}  "
          f"stats={r.stats}")
    print("verification values in DB:", {x[0] for x in con.execute("SELECT verification FROM products")})
    for u in cat.unresolved_refs:
        print("unresolved:", u[:140])

    hdr("2. NEEDS ANALYZER: verbatim-quoted, stated_problem vs capability")
    kinds = {}
    for p in cat.products:
        for n in p.needs:
            kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
    print("needs by kind:", kinds or "(none: --no-llm)", "| dropped:", len(r.dropped_needs))
    for p in cat.products:
        for n in p.needs:
            if n["kind"] == "stated_problem":
                print(f"  {p.name:12s} problem='{n.get('problem_phrase')}'  quote=\"{n['quote'][:60]}\"")

    hdr("3. ICP BUILDER: one ICP per product with STATED criteria only")
    for pid, (icp_id, icp) in r.icps.items():
        print(f"  {pid} (icp {icp_id}): industries={icp.industries.values}  missing={icp.missing}")
    print(f"  not searchable ({len(cat.products) - len(r.icps)} products): no stated industry/country/size")

    hdr("4. COMPANY SCOUT: industry mapping -> provider queries (dry run, no external calls)")
    schema = ClaySchema.from_capabilities(ROOT / "docs/clay_capabilities.json")
    terms = sorted({t for _, icp in r.icps.values() for t in icp.industries.values})
    maps = {m.original_term: m for m in map_industries(con, None, terms, schema.enums["industry"], llm)}
    for m in maps.values():
        print(f"  #{m.id} '{m.original_term}' -> {m.canonical} [{m.status}, {m.confidence:.2f}]")
    if a.demo_approve:
        for m in maps.values():
            if m.canonical:
                review(con, m.id, "approved", "demo-only", taxonomy=schema.enums["industry"])
        maps = {t: m for t, m in zip(maps, map_industries(con, None, list(maps), schema.enums["industry"], llm))}
        print("  (--demo-approve: mappings approved in the DEMO DB only, reviewer='demo-only')")
    clay = ClaySearchProvider(None, schema)
    wd = WikidataProvider(WikidataMappings.load(ROOT / "data/wikidata_mappings.json"))
    for pid, (icp_id, icp) in r.icps.items():
        req = DiscoveryRequest(icp, icp_id, 1, [maps[t] for t in icp.industries.values])
        for prov in (clay, wd):
            res = await run_discovery(prov, req, con, allow_live=False)
            q = res.query.replace("\n", " ")[:110] if res.query else "<blocked>"
            print(f"  {pid:14s} {prov.name:8s} ready={not res.pending_review!s:5s} query={q}")
            for i in res.pending_review[:2]:
                print(f"  {'':14s} {'':8s} - {i[:110]}")

    hdr("5. SQLITE: persistence, dedupe and provenance (SYNTHETIC provider, no network)")
    pid, (icp_id, icp) = next(iter(r.icps.items()))
    req = DiscoveryRequest(icp, icp_id, 5, [maps[t] for t in icp.industries.values])
    res = await run_discovery(SyntheticFixtureProvider(), req, con, allow_live=True)
    print(f"  inserted={res.inserted} updated={res.updated} skipped={res.skipped}")
    for c in res.companies:
        fm = c["search_filter_match"]
        print(f"  {c['domain']:24s} search_filter_match={fm['score']}  unknown={fm['unknown']}  "
              f"status={c['qualification_status']} ({c['qualification_source']})")
    for row in con.execute("SELECT c.domain, s.source, s.tool, s.source_url FROM company_sources s "
                           "JOIN companies c ON c.id=s.company_id"):
        print("  provenance:", tuple(row))

    hdr("6. IDEMPOTENCY: re-run import and discovery")
    before = counts(con)
    r2 = run_catalog_yaml(None, con, a.catalogue)  # no LLM needed: cached needs are reused
    await run_discovery(SyntheticFixtureProvider(), req, con, allow_live=True)
    after = counts(con)
    print("  re-import stats:", r2.stats)
    print("  before:", before)
    print("  after: ", after)
    same = {k: before[k] == after[k] for k in ("products", "icps", "industry_mappings", "companies")}
    print("  unchanged (products/icps/mappings/companies):", same,
          "| company_sources +", after["company_sources"] - before["company_sources"], "(one row per observation)")
    dup_p = con.execute("SELECT COUNT(*) - COUNT(DISTINCT id) FROM products").fetchone()[0]
    dup_i = con.execute("SELECT COUNT(*) - COUNT(DISTINCT content_sha256) FROM icps").fetchone()[0]
    print(f"  duplicate products={dup_p}  duplicate ICPs={dup_i}")
    if llm:
        llm.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("catalogue")
    p.add_argument("--db", default=str(ROOT / "data/demo/chanakya_demo.db"))
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--demo-approve", action="store_true")
    asyncio.run(main(p.parse_args()))
