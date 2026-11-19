"""Map ICP industry terms onto Clay's discovered `industry` taxonomy, with an audit trail.

Policy (never silently substitute):
  exact        case-insensitive exact taxonomy value            -> used in queries
  proposed     LLM mapping, confidence >= AUTO_CONF, unambiguous -> NOT used until approved
  needs_review LLM mapping that is ambiguous or lower confidence -> NOT used until approved
  unmapped     no defensible taxonomy value                      -> never used
  approved / rejected   human decision recorded in SQLite
The LLM may only choose values present in the taxonomy; anything else is discarded and reported.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from chanakya.storage.db import now

AUTO_CONF = 0.85
USABLE = {"exact", "approved"}
FIELD = "industry"


@dataclass
class IndustryMapping:
    original_term: str
    canonical: list[str]
    status: str
    method: str
    confidence: float
    rationale: str
    alternatives: list[str] = field(default_factory=list)
    rejected_llm_values: list[str] = field(default_factory=list)
    id: int | None = None


SYSTEM = (
    "You map a company-industry term onto a FIXED taxonomy. Choose ONLY values copied exactly from the taxonomy. "
    "Pick the value(s) a sales researcher would filter on to find companies described by the term. "
    "If the term spans several taxonomy values, list them in canonical and set ambiguous=true if a human should "
    "choose. If no value is a defensible match, return canonical=[]. Never pick a merely adjacent industry. "
    'Return JSON: {"canonical": [str], "alternatives": [str], "confidence": 0..1, "ambiguous": bool, '
    '"rationale": str}'
)


def map_term(term: str, taxonomy: list[str], llm) -> IndustryMapping:
    exact = next((v for v in taxonomy if v.lower() == term.strip().lower()), None)
    if exact:
        return IndustryMapping(term, [exact], "exact", "exact", 1.0, "Term is an exact Clay taxonomy value.")
    if llm is None:
        return IndustryMapping(term, [], "unmapped", "none", 0.0, "No exact match and no LLM available.")
    d = None
    for _ in range(2):  # one retry on malformed output
        r = llm.chat([{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": f"Term: {term}\nTaxonomy ({FIELD}):\n" + json.dumps(taxonomy)}],
                     json_format=True, temperature=0.0)
        try:
            d = json.loads(r.content)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(d, dict):
        return IndustryMapping(term, [], "needs_review", "llm", 0.0, "Model returned invalid JSON twice.")
    allowed = set(taxonomy)
    raw_c = [str(v) for v in d.get("canonical") or []]
    raw_a = [str(v) for v in d.get("alternatives") or []]
    canonical = [v for v in raw_c if v in allowed]
    alts = [v for v in raw_a if v in allowed and v not in canonical]
    rejected = [v for v in raw_c + raw_a if v not in allowed]
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    rationale = str(d.get("rationale", ""))[:500]
    if not canonical:
        status = "unmapped"
    elif conf >= AUTO_CONF and not d.get("ambiguous") and not rejected:
        status = "proposed"
    else:
        status = "needs_review"
    return IndustryMapping(term, canonical, status, "llm", conf, rationale, alts, rejected)


# ---------------- persistence + review ----------------
def prior_decision(con, term: str) -> IndustryMapping | None:
    """Latest human decision for the term; else the latest pending mapping, so one term = one review item."""
    # pending rows are reused only if an LLM (or exact match) produced them; a no-LLM 'unmapped' is retried later
    for q in ("status IN ('approved','rejected')", "status NOT IN ('approved','rejected') AND method != 'none'"):
        r = con.execute(f"SELECT * FROM industry_mappings WHERE lower(original_term)=lower(?) AND {q} "
                        "ORDER BY id DESC LIMIT 1", (term.strip(),)).fetchone()
        if r:
            return _row(r)
    return None


def save(con, icp_id: int | None, m: IndustryMapping) -> IndustryMapping:
    cur = con.execute(
        "INSERT INTO industry_mappings(icp_id,field,original_term,canonical_json,status,method,confidence,rationale,"
        "alternatives_json,rejected_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (icp_id, FIELD, m.original_term, json.dumps(m.canonical), m.status, m.method, m.confidence, m.rationale,
         json.dumps(m.alternatives), json.dumps(m.rejected_llm_values), now()))
    con.commit()
    m.id = cur.lastrowid
    return m


def review(con, mapping_id: int, decision: str, reviewer: str, canonical: list[str] | None = None,
           taxonomy: list[str] | None = None) -> None:
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved|rejected")
    if canonical is not None:
        bad = [v for v in canonical if taxonomy is not None and v not in taxonomy]
        if bad:
            raise ValueError(f"not Clay taxonomy values: {bad}")
        con.execute("UPDATE industry_mappings SET canonical_json=? WHERE id=?", (json.dumps(canonical), mapping_id))
    con.execute("UPDATE industry_mappings SET status=?, reviewed_by=?, reviewed_at=? WHERE id=?",
                (decision, reviewer, now(), mapping_id))
    con.commit()


def _row(r) -> IndustryMapping:
    return IndustryMapping(r["original_term"], json.loads(r["canonical_json"]), r["status"], r["method"],
                           r["confidence"], r["rationale"], json.loads(r["alternatives_json"] or "[]"),
                           json.loads(r["rejected_json"] or "[]"), r["id"])


def map_industries(con, icp_id, terms: list[str], taxonomy: list[str], llm) -> list[IndustryMapping]:
    """Human decisions win and are reused without an LLM call; new terms are mapped and stored."""
    return [prior_decision(con, t) or save(con, icp_id, map_term(t, taxonomy, llm)) for t in terms]


def as_report(ms: list[IndustryMapping]) -> list[dict]:
    return [asdict(m) for m in ms]
