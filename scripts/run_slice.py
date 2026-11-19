"""Vertical slice: Agent 1 (Catalog) -> Agent 2 (Company Scout) -> SQLite.
Default is a DRY RUN (builds and validates the Clay query; no search). Live search needs --live-search.
Usage: python scripts/run_slice.py [--offerings data/offerings] [--live-search] [--limit 5]
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chanakya.catalog.agent import NoOfferingSources, run_catalog  # noqa: E402
from chanakya.config import load_settings  # noqa: E402
from chanakya.llm.ollama_client import OllamaClient  # noqa: E402
from chanakya.scout.agent import APPROVED_TOOLS, ClayGateway, run_scout  # noqa: E402
from chanakya.scout.dsl import ClaySchema  # noqa: E402
from chanakya.scout.industry_map import map_industries  # noqa: E402
from chanakya.storage.db import connect  # noqa: E402


async def main(a) -> int:
    s = load_settings()
    con = connect(s.db_path)
    with OllamaClient(s) as llm:
        try:
            cat = run_catalog(llm, con, a.offerings)
        except NoOfferingSources as e:
            print(f"STOP (Catalog Agent): {e}")
            return 2
        print(f"Catalog: {len(cat.offerings)} offerings, ICP id={cat.icp_id}, missing={cat.icp.missing}, "
              f"dropped={cat.dropped}")
        schema = ClaySchema.from_capabilities()
        terms = cat.icp.industries.values if cat.icp.industries else []
        maps = map_industries(con, cat.icp_id, terms, schema.enums["industry"], llm)
    print("Industry mappings:")
    for m in maps:
        print(f"  #{m.id} '{m.original_term}' -> {m.canonical} [{m.status}, conf {m.confidence:.2f}] {m.rationale[:120]}")
    if not a.live_search:
        r = await run_scout(cat.icp, cat.icp_id, con, None, schema, limit=a.limit, industry_mappings=maps)
        print("DRY RUN - Clay query (not executed):\n ", r.query, "\nUnmapped:", r.unmapped,
              "\nPending review (blocks live search):", r.pending_review)
        return 0
    from chanakya.clay.mcp_client import ClayMCPClient
    async with ClayMCPClient(allowed_tools=set(APPROVED_TOOLS)) as clay:
        r = await run_scout(cat.icp, cat.icp_id, con, ClayGateway(clay), schema,
                            allow_live_search=True, limit=a.limit, industry_mappings=maps)
    print(json.dumps(r.__dict__, indent=2, default=str))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--offerings", default="data/offerings")
    p.add_argument("--live-search", action="store_true")
    p.add_argument("--limit", type=int, default=5)
    logging.basicConfig(level=logging.WARNING)
    sys.exit(asyncio.run(main(p.parse_args())))
