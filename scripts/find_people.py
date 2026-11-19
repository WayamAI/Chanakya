"""Agent 3 - People Finder: first controlled contact search (Clay search-contacts), ONE company, ONE person.

Default = DRY RUN: prints the company, approved persona mappings, exact Clay arguments and limits. The DB is opened
READ-ONLY and no Clay connection is made.
A live call requires ALL of:
  --live
  --confirm-query "<exact DSL printed by the dry run>"
  a human-qualified company (scripts/review_companies.py) and >=1 approved persona mapping (review_personas.py)
  get-current-workspace == CLAY_WORKSPACE_ID (default 1461095) and get-credits-available all true (re-checked live)
Only the People Finder allowlist (chanakya.clay.policy.PEOPLE_FINDER) is used. No enrichment, no LinkedIn.
The raw response is stored in raw_responses with the run id; people are stored unverified, roles pending review.

WARNING: the real search-contacts response shape has never been observed. The parser is NOT yet validated against
a real response: inspect the saved raw response before trusting the stored people.

Usage:
  python scripts/find_people.py --domain icai.org [--product PRD-SUTRADHAR] [--mapping 10]
  python scripts/find_people.py --domain icai.org --live --confirm-query "select from people where ..."
DB: $CHANAKYA_DB_PATH or data/chanakya.db
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from chanakya.clay import policy  # noqa: E402
from chanakya.people.finder import (ClayContactsProvider, PeopleGateway, PeopleRequest,  # noqa: E402
                                    build_contact_query, load_company_refs, run_people_finder)
from chanakya.people.personas import USABLE, mappings_for  # noqa: E402
from chanakya.storage.db import connect  # noqa: E402

LIVE_MAX_COMPANIES = 1  # hard cap for the first live run (not configurable)
LIVE_MAX_PEOPLE = 1
PARSER_WARNING = ("WARNING: the search-contacts response parser is NOT validated against a real Clay response. "
                  "Inspect the saved raw response before trusting stored people.")


class FindPeopleError(RuntimeError):
    pass


def db_path() -> str:
    return os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data" / "chanakya.db"))


def open_readonly(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        raise FindPeopleError(f"database not found: {path}")
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def prepare(con, domain: str, product: str | None, mapping_ids: list[int] | None) -> dict:
    """Pure, read-only preparation. Raises FindPeopleError on any gate failure."""
    refs, issues = load_company_refs(con, [domain], require_qualified=True)
    if issues:
        raise FindPeopleError("; ".join(issues))
    row = con.execute("SELECT c.icp_id, i.product_id FROM companies c LEFT JOIN icps i ON i.id=c.icp_id "
                      "WHERE c.domain=?", (domain,)).fetchone()
    product = product or row["product_id"]
    if not product:
        raise FindPeopleError(f"{domain}: no product linked via its ICP; pass --product")
    usable = [m for m in mappings_for(con, row["icp_id"], product) if m.status in USABLE and m.product_id == product]
    if mapping_ids:
        unknown = set(mapping_ids) - {m.id for m in usable}
        if unknown:
            raise FindPeopleError(f"mapping(s) {sorted(unknown)} are not approved mappings for {product}")
        usable = [m for m in usable if m.id in mapping_ids]
    if not usable:
        raise FindPeopleError(f"no approved persona mapping for {product}")
    req = PeopleRequest(refs[:LIVE_MAX_COMPANIES], usable, limit_per_company=LIVE_MAX_PEOPLE,
                        context={"product_id": product, "entry_point": "scripts/find_people.py"})
    q = build_contact_query(req)
    if not q.ready:
        raise FindPeopleError("; ".join(q.issues))
    return {"company": refs[0], "product": product, "mappings": usable, "request": req, "query": q}


def show(p: dict, workspace: str) -> None:
    c, q = p["company"], p["query"]
    print(f"Company : {c.domain} ({c.name}) - human-qualified, id {c.company_id}")
    print(f"Product : {p['product']}")
    for m in p["mappings"]:
        print(f"Mapping #{m.id} [{m.status}] {m.persona}: titles={m.titles} seniorities={m.seniorities}")
    print(f"Limits  : {LIVE_MAX_COMPANIES} company, {LIVE_MAX_PEOPLE} person (hard caps); tenure=current; "
          f"tools={sorted(policy.PEOPLE_FINDER)}; enrichment and LinkedIn: never")
    print(f"Workspace pin: {workspace}")
    print("Clay arguments:", json.dumps(q.arguments(), ensure_ascii=False))
    print("Query:", q.dsl)
    print(PARSER_WARNING)


def default_client_factory():
    from chanakya.clay.mcp_client import ClayMCPClient
    return ClayMCPClient(allowed_tools=set(policy.PEOPLE_FINDER))


async def main(argv=None, client_factory=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--product")
    ap.add_argument("--mapping", type=int, action="append", help="restrict to these approved mapping ids")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--confirm-query")
    a = ap.parse_args(argv)
    workspace = os.getenv("CLAY_WORKSPACE_ID", "1461095")
    try:
        ro = open_readonly(db_path())
        p = prepare(ro, a.domain.strip().lower(), a.product, a.mapping)
        ro.close()
    except FindPeopleError as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 2
    show(p, workspace)
    if not a.live:
        print("\nDRY RUN: no Clay call made, nothing written.")
        return 0
    if a.confirm_query != p["query"].dsl:
        print("REFUSED: --confirm-query does not exactly match the generated query.", file=sys.stderr)
        return 3

    con = connect(db_path())
    p = prepare(con, a.domain.strip().lower(), a.product, a.mapping)  # re-check gates on the writable connection
    if p["query"].dsl != a.confirm_query:
        print("REFUSED: query changed between dry run and execution.", file=sys.stderr)
        return 3
    factory = client_factory or default_client_factory
    try:
        async with factory() as clay:
            provider = ClayContactsProvider(PeopleGateway(clay), workspace)  # re-checks workspace + credits
            res = await run_people_finder(provider, p["request"], con, allow_live=True)
    except Exception as e:  # run row + any raw responses are already saved by run_people_finder
        print(f"LIVE RUN FAILED ({type(e).__name__}): {e}", file=sys.stderr)
        return 4
    raw = [r["id"] for r in con.execute("SELECT id FROM raw_responses WHERE run_id=? ORDER BY id", (res.run_id,))]
    print(json.dumps({"run_id": res.run_id, "inserted": res.inserted, "updated": res.updated,
                      "skipped": res.skipped, "people": res.people, "raw_response_ids": raw}, indent=2, default=str))
    print(PARSER_WARNING)
    print("People are unverified; roles need review: python scripts/review_roles.py list")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
