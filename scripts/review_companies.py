"""Company qualification review CLI (Agent 2 output -> Agent 3 input). LOCAL ONLY: reads/writes SQLite, makes no
network calls.

A human qualifies or disqualifies a company found by Company Scout. Only human-`qualified` companies can be used by
People Finder. Decisions go through chanakya.scout.qualification.record_human_decision (the only path), which
appends to human_reviews (never edited) and re-derives the company's status from that history.
Nothing is ever qualified automatically.

Usage:
  python scripts/review_companies.py list
  python scripts/review_companies.py show DOMAIN
  python scripts/review_companies.py qualify|disqualify DOMAIN --by NAME --reason R
DB: $CHANAKYA_DB_PATH or data/chanakya.db
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from chanakya.scout.qualification import human_reviews, record_human_decision  # noqa: E402
from chanakya.storage.db import connect  # noqa: E402

TARGET = {"qualify": "qualified", "disqualify": "disqualified"}
EVIDENCE_KEYS = ("search_filter_match", "data_quality", "product_relevance", "business_fit", "review_reasons",
                 "executed_query", "provider_source", "provenance_note")


class ReviewError(ValueError):
    pass


def company_view(con, domain: str) -> dict:
    r = con.execute("SELECT c.id, c.domain, c.name, c.icp_id, i.product_id, c.qualification_status, "
                    "c.qualification_source, c.assessment_json FROM companies c LEFT JOIN icps i ON i.id=c.icp_id "
                    "WHERE c.domain=?", (domain,)).fetchone()
    if r is None:
        raise ReviewError(f"unknown company {domain!r} (only companies stored by Company Scout can be reviewed)")
    a = json.loads(r["assessment_json"] or "{}")
    src = [dict(s) for s in con.execute("SELECT source, tool, clay_task_id, source_url, query, fetched_at "
                                        "FROM company_sources WHERE company_id=? ORDER BY id", (r["id"],))]
    return {"id": r["id"], "domain": r["domain"], "name": r["name"], "icp_id": r["icp_id"],
            "product_id": r["product_id"], "qualification_status": r["qualification_status"],
            "qualification_source": r["qualification_source"],
            "evidence": {k: a[k] for k in EVIDENCE_KEYS if k in a}, "sources": src,
            "human_reviews": human_reviews(con, r["id"])}


def decide(con, domain: str, cmd: str, reviewer: str, reason: str) -> dict:
    if not (reviewer or "").strip() or not (reason or "").strip():
        raise ReviewError("--by and --reason are required and must not be blank")
    company_view(con, domain)  # raises on unknown domain before anything is written
    record_human_decision(con, domain, TARGET[cmd], reviewer, reason)
    return company_view(con, domain)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "show", "qualify", "disqualify"])
    ap.add_argument("domain", nargs="?")
    ap.add_argument("--by")
    ap.add_argument("--reason")
    a = ap.parse_args(argv)
    con = connect(os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data" / "chanakya.db")))
    out = lambda o: print(json.dumps(o, indent=2, ensure_ascii=False, default=str))  # noqa: E731
    try:
        if a.cmd == "list":
            out([dict(r) for r in con.execute("SELECT domain, name, qualification_status, qualification_source "
                                               "FROM companies ORDER BY id")])
            return 0
        if not a.domain:
            raise ReviewError(f"{a.cmd} needs DOMAIN")
        before = company_view(con, a.domain)
        print(f"== {before['domain']} ({before['name']}) - current: {before['qualification_status']} "
              f"[{before['qualification_source']}], product: {before['product_id']}")
        out(before)
        if a.cmd == "show":
            return 0
        after = decide(con, a.domain, a.cmd, a.by or "", a.reason or "")
        print(f"recorded: {before['qualification_status']} -> {after['qualification_status']} "
              f"[{after['qualification_source']}] by {a.by.strip()} (human_reviews entries: "
              f"{len(after['human_reviews'])})")
    except (ReviewError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
