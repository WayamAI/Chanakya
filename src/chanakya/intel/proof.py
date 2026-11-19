"""Proof registry: the only proof points outreach may use, and claims it must never make.

Handover rule: "Use only proof Wayam has. Setu's published proof is a Tableau estate (1,200+ dashboards; 1,200 Tableau
dashboards retired in six weeks; $4.2M licence savings; 80 to 90% auto-migrated in 4 to 6 weeks). There is no
BusinessObjects, Cognos, OBIEE or MicroStrategy case study yet." Never invent customer references or results.
"""
from __future__ import annotations

import re

from chanakya.storage.db import now

HANDOVER = "Lakshya handover, 4 Nov 2026 (section 2)"
SEED = [
    ("PRD-SETU", "Tableau estate of 1,200+ dashboards migrated", 1),
    ("PRD-SETU", "1,200 Tableau dashboards retired in six weeks", 1),
    ("PRD-SETU", "$4.2M licence savings on a Tableau migration", 1),
    ("PRD-SETU", "80 to 90% of a Tableau estate auto-migrated in 4 to 6 weeks", 1),
    ("PRD-SETU", "BusinessObjects migration proof", 0),
    ("PRD-SETU", "Cognos migration proof", 0),
    ("PRD-SETU", "OBIEE migration proof", 0),
    ("PRD-SETU", "MicroStrategy migration proof", 0),
]
NO_PROOF_TOOLS = ["businessobjects", "business objects", "cognos", "obiee", "microstrategy"]
PROOF_WORDS = r"(migrated|migration[s]? (we|i) (did|delivered|ran)|case stud|retired|delivered|proof|we moved|we have done)"


def seed(con) -> int:
    n = 0
    for pid, claim, allowed in SEED:
        cur = con.execute("INSERT OR IGNORE INTO accelerator_proof(product_id,claim,allowed,source_url,note,created_at) "
                          "VALUES(?,?,?,?,?,?)", (pid, claim, allowed, None, HANDOVER, now()))
        n += cur.rowcount
    con.commit()
    return n


def allowed_proofs(con, product_id: str) -> list[str]:
    return [r[0] for r in con.execute("SELECT claim FROM accelerator_proof WHERE product_id=? AND allowed=1 ORDER BY id",
                                      (product_id,))]


def claim_violations(text: str, product_id: str) -> list[str]:
    """Flags proof claims the handover forbids (e.g. Setu BusinessObjects/Cognos/OBIEE/MicroStrategy proof)."""
    low = (text or "").lower()
    out = []
    if product_id == "PRD-SETU":
        for tool in NO_PROOF_TOOLS:
            for m in re.finditer(re.escape(tool), low):
                window = low[max(0, m.start() - 80): m.end() + 80]
                if re.search(PROOF_WORDS, window):
                    out.append(f"claims {tool} proof; Setu's only published proof is Tableau")
                    break
    return out
