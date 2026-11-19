"""Role Review CLI (Agent 3, People Finder). LOCAL ONLY: reads/writes SQLite, makes no network calls.

A human approves or rejects each person/role People Finder found. Only `approved` roles are processed by Agent 4
(LinkedIn Resolver). Every action is recorded in person_role_review_history with a snapshot of the role before it.

Usage:
  python scripts/review_roles.py list [--status needs_manual_review|approved|rejected] [--domain DOMAIN]
  python scripts/review_roles.py show ROLE_ID
  python scripts/review_roles.py approve|reject|reopen ROLE_ID --by NAME --reason R [--evidence TEXT]
  python scripts/review_roles.py history ROLE_ID
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
from chanakya.people import role_review as RR  # noqa: E402
from chanakya.storage.db import connect  # noqa: E402

TARGET = {"approve": RR.APPROVED, "reject": RR.REJECTED, "reopen": RR.PENDING}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "show", "approve", "reject", "reopen", "history"])
    ap.add_argument("role_id", nargs="?", type=int)
    ap.add_argument("--status", choices=sorted(RR.STATUSES))
    ap.add_argument("--domain")
    ap.add_argument("--by")
    ap.add_argument("--reason")
    ap.add_argument("--evidence")
    a = ap.parse_args(argv)
    con = connect(os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data" / "chanakya.db")))
    out = lambda o: print(json.dumps(o, indent=2, ensure_ascii=False, default=str))  # noqa: E731
    try:
        if a.cmd == "list":
            out(RR.list_roles(con, a.status, a.domain))
            return 0
        if a.role_id is None:
            ap.error(f"{a.cmd} needs ROLE_ID")
        if a.cmd == "show":
            out(RR.get_role(con, a.role_id))
        elif a.cmd == "history":
            out(RR.history(con, a.role_id))
        else:
            ev = [{"source": "human_review", "text": a.evidence}] if a.evidence else []
            r = RR.review_role(con, a.role_id, TARGET[a.cmd], reviewer=a.by or "", reason=a.reason or "", evidence=ev)
            out({"role_id": r["id"], "review_status": r["review_status"]})
    except RR.RoleReviewError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
