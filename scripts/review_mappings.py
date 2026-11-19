"""Review ICP industry -> Clay taxonomy mappings.
  python scripts/review_mappings.py list
  python scripts/review_mappings.py approve <id> --by <name> [--values "Financial Services" "Banking"]
  python scripts/review_mappings.py reject  <id> --by <name>
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from chanakya.scout.dsl import ClaySchema  # noqa: E402
from chanakya.scout.industry_map import review  # noqa: E402
from chanakya.storage.db import connect  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("action", choices=["list", "approve", "reject"])
p.add_argument("id", nargs="?", type=int)
p.add_argument("--by")
p.add_argument("--values", nargs="+")
a = p.parse_args()
con = connect(os.getenv("CHANAKYA_DB_PATH", "data/chanakya.db"))
if a.action == "list":
    for r in con.execute("SELECT * FROM industry_mappings ORDER BY id"):
        print(f"#{r['id']} '{r['original_term']}' -> {json.loads(r['canonical_json'])} [{r['status']}, "
              f"conf {r['confidence']:.2f}] alt={json.loads(r['alternatives_json'] or '[]')}\n    {r['rationale']}")
else:
    if not (a.id and a.by):
        sys.exit("id and --by are required")
    review(con, a.id, "approved" if a.action == "approve" else "rejected", a.by, a.values,
           ClaySchema.from_capabilities().enums["industry"])
    print("recorded")
