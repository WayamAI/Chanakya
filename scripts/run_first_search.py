"""First controlled company search for one stored ICP (default: Sutradhar), Clay workspace 1461095.

Default = PREPARE ONLY: prints the exact query and gate status; makes no Clay call.
Execution requires ALL of:
  --execute
  --confirm-query "<exact query text printed by the prepare step>"
  approved industry mapping(s) for the ICP (scripts/review_mappings.py)
  get-current-workspace == CLAY_WORKSPACE_ID (default 1461095)
  a company-only query (no cross-entity filters), limit 1
Usage:
  python scripts/run_first_search.py [--product PRD-SUTRADHAR]
  python scripts/run_first_search.py --execute --confirm-query "select from companies where ..."
"""
import argparse
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chanakya.catalog.models import ICP  # noqa: E402
from chanakya.clay import policy  # noqa: E402
from chanakya.scout.agent import ClayGateway, ClaySearchProvider, run_discovery  # noqa: E402
from chanakya.scout.discovery import DiscoveryRequest  # noqa: E402
from chanakya.scout.dsl import ClaySchema  # noqa: E402
from chanakya.scout.industry_map import map_industries  # noqa: E402
from chanakya.scout.pilot_scope import PilotScope  # noqa: E402
from chanakya.storage.db import connect  # noqa: E402

WORKSPACE_ID = os.getenv("CLAY_WORKSPACE_ID", "1461095")


async def main(a) -> int:
    con = connect(os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data/chanakya.db")))
    row = con.execute("SELECT id, icp_json FROM icps WHERE product_id=? AND content_sha256 IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1", (a.product,)).fetchone()
    if not row:
        print(f"No stored ICP for {a.product}. Run scripts/import_catalog.py first.")
        return 2
    product_icp = ICP.model_validate_json(row["icp_json"])
    scope = PilotScope.load(a.scope)
    icp = scope.apply(product_icp) if scope else product_icp  # in-memory copy; stored ICP untouched
    schema = ClaySchema.from_capabilities(ROOT / "docs/clay_capabilities.json")
    maps = map_industries(con, row["id"], icp.industries.values, schema.enums["industry"], llm=None)
    ctx = {"pilot_scope": ({"path": scope.path, "countries": scope.countries, "approved_by": scope.approved_by,
                            "approved_at": scope.approved_at} if scope else None),
           "product_id": a.product, "workspace_id": WORKSPACE_ID}
    req = DiscoveryRequest(icp, row["id"], 1, maps, context=ctx)
    provider = ClaySearchProvider(None, schema, expected_workspace_id=WORKSPACE_ID)
    prepared = provider.prepare(req)

    print(f"Product: {a.product}   ICP id: {row['id']}   verification: {product_icp.verification}")
    print(f"Workspace pin: {WORKSPACE_ID}")
    print(f"Product ICP countries (stored): {product_icp.countries}")
    if scope:
        print(f"Pilot scope ({scope.path}): countries={scope.countries}, approved by {scope.approved_by} "
              f"on {scope.approved_at} [query-time only]")
    for m in maps:
        print(f"Mapping #{m.id}: '{m.original_term}' -> {m.canonical} [{m.status}, conf {m.confidence:.2f}]")
    print(f"Constraints: industry=from mapping(s); country={icp.countries.values if icp.countries else 'none'}; "
          "company size=none (not stated); limit=1; company-only filters; enrichment disabled")
    status = "READY" if prepared.ready else "BLOCKED"
    print(f"Query [{status}]:", prepared.text if prepared.ready else "<not executable>")
    print("Issues:", prepared.issues or "none")
    if not prepared.ready:
        # In-memory preview: what the query becomes once the pending mapping(s) are approved. Nothing is stored.
        sim = [dataclasses.replace(m, status="approved") if m.status not in ("approved", "rejected") and m.canonical
               else m for m in maps]
        preview = provider.prepare(DiscoveryRequest(icp, row["id"], 1, sim))
        print("Query after approval of the pending mapping(s) [PREVIEW, not stored]:")
        print(" ", preview.text, "| company-only:", not preview.issues)
    if not a.execute:
        print("\nPREPARE ONLY: no Clay call made.")
        return 0

    if not prepared.ready:
        print("REFUSED: query not ready (approve mapping(s) first).")
        return 3
    if a.confirm_query != prepared.text:
        print("REFUSED: --confirm-query does not exactly match the prepared query.")
        return 3
    from chanakya.clay.mcp_client import ClayMCPClient
    async with ClayMCPClient(allowed_tools=set(policy.COMPANY_SCOUT)) as clay:
        provider.gateway = ClayGateway(clay)
        res = await run_discovery(provider, req, con, allow_live=True)
    out = ROOT / "docs/first_search_result.json"
    raw = con.execute("SELECT detail_json FROM runs WHERE agent='company_scout' ORDER BY id DESC LIMIT 1").fetchone()[0]
    out.write_text(json.dumps({"query": prepared.text, "result": res.__dict__, "run": json.loads(raw)},
                              indent=2, default=str), encoding="utf-8")
    print(json.dumps(res.__dict__, indent=2, default=str))
    print(f"Saved: {out}. Report before any further search.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--product", default="PRD-SUTRADHAR")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--confirm-query", default=None)
    p.add_argument("--scope", default=str(ROOT / "data/pilot_scope.json"))
    sys.exit(asyncio.run(main(p.parse_args())))
