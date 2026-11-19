"""LinkedIn Resolver CLI (Agent 4). Uses Mriganka's LinkedIn harness through a subprocess; DRY RUN by default.

Usage:
  python scripts/resolve_linkedin.py candidates
  python scripts/resolve_linkedin.py run [--limit 3] [--person ID ...]   (dry run)
  python scripts/resolve_linkedin.py run --live [--limit 3] ...    (real LinkedIn page views, one person at a time)
  python scripts/resolve_linkedin.py list [--status pending_review|approved|rejected]
  python scripts/resolve_linkedin.py show RESOLUTION_ID
  python scripts/resolve_linkedin.py approve RESOLUTION_ID --by NAME --reason R [--url ALTERNATIVE_URL]
  python scripts/resolve_linkedin.py reject RESOLUTION_ID --by NAME --reason R
  python scripts/resolve_linkedin.py history RESOLUTION_ID
Env: CHANAKYA_DB_PATH (default data/chanakya.db), LINKEDIN_HARNESS_DIR (harness checkout; needed for `run`),
     LINKEDIN_HARNESS_TIMEOUT_S (default 300).
Only people whose role was approved with scripts/review_roles.py are eligible (no override).
Before --live: in the harness folder run `uv sync` and `uv run linkedin_harness.py login` by hand.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from chanakya.resolver import review as R  # noqa: E402
from chanakya.resolver.agent import (DEFAULT_LIMIT, ResolverBlocked, run_resolver,  # noqa: E402
                                     select_candidates)
from chanakya.resolver.harness_client import HarnessClient, HarnessError  # noqa: E402
from chanakya.storage.db import connect  # noqa: E402


def _client() -> HarnessClient:
    try:
        return HarnessClient.from_env()
    except HarnessError:
        # Dry runs only need argv; a placeholder dir keeps them working before the harness is set up.
        return HarnessClient(Path(os.getenv("LINKEDIN_HARNESS_DIR") or "<LINKEDIN_HARNESS_DIR>"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["candidates", "run", "list", "show", "approve", "reject", "history"])
    ap.add_argument("id", nargs="?", type=int)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--person", type=int, action="append")
    ap.add_argument("--status")
    ap.add_argument("--by")
    ap.add_argument("--reason")
    ap.add_argument("--url")
    a = ap.parse_args(argv)
    con = connect(os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data" / "chanakya.db")))
    out = lambda o: print(json.dumps(o, indent=2, ensure_ascii=False, default=str))  # noqa: E731
    try:
        if a.cmd == "candidates":
            out([c.__dict__ for c in select_candidates(con)])
        elif a.cmd == "run":
            client = HarnessClient.from_env() if a.live else _client()
            rep = run_resolver(client, con, allow_live=a.live, limit=a.limit, person_ids=a.person)
            out(rep.__dict__)
            if rep.dry_run:
                print("\nDRY RUN: no harness process was started. Add --live to run these commands.", file=sys.stderr)
        elif a.cmd == "list":
            out([{k: r[k] for k in ("id", "full_name", "status", "confidence", "score", "profile_url",
                                    "review_status", "harness_version")} for r in R.list_resolutions(con, a.status)])
        elif a.cmd in ("show", "approve", "reject", "history"):
            if a.id is None:
                ap.error(f"{a.cmd} needs RESOLUTION_ID")
            if a.cmd == "show":
                r = R.get(con, a.id)
                d = dict(r)
                d["candidate_urls"] = R.candidate_urls(r)
                out(d)
            elif a.cmd == "approve":
                out({"approved_url": R.approve(con, a.id, by=a.by or "", reason=a.reason or "", url=a.url)})
            elif a.cmd == "reject":
                R.reject(con, a.id, by=a.by or "", reason=a.reason or "")
                out({"rejected": a.id})
            else:
                out([dict(h) for h in R.history(con, a.id)])
    except (R.ReviewError, ResolverBlocked, HarnessError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
