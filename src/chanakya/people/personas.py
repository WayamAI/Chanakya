"""Agent 3 - buyer persona -> searchable role mapping, with human review and an append-only audit trail.

Rules:
  * Personas come ONLY from the catalogue (ICP.buyer_roles, with evidence) or from a named human reviewer who gives
    a reason and supporting evidence. Code and the LLM never invent or approve personas or job titles.
  * Every mapping starts `pending`. Only a human `approve` makes it `approved`; only `approved` is usable by
    People Finder. `rejected` is never usable.
  * Updating titles/seniorities/persona/evidence sends the mapping back to `pending` (re-approval required).
  * Every create / approve / reject / update writes a persona_mapping_history row holding a full snapshot of the
    mapping BEFORE the change, so no review history is ever lost.
  * Mappings are keyed by product (22 products have no ICP); an ICP id is attached when known.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from chanakya.catalog.models import ICP
from chanakya.storage.db import now

USABLE = {"approved"}
STATUSES = {"pending", "approved", "rejected"}
# Clay `experiences.seniority` enum (from docs/clay_capabilities.json, search-contacts field reference).
CLAY_SENIORITIES = frozenset({"Founder", "Owner", "Board Member", "Partner", "C-suite", "VP", "Director", "Head",
                              "Manager", "Senior", "Mid-level", "Entry", "Intern / In Training", "Unknown"})
NO_PERSONA_ISSUE = ("needs_human_review: the catalogue states no buyer persona for this product; "
                    "a reviewer must add one and approve it before any contact search")


@dataclass
class RoleMapping:
    id: int
    icp_id: int | None
    product_id: str | None
    persona: str
    persona_source: str  # catalogue | human
    titles: list[str]
    seniorities: list[str]
    status: str
    evidence: list[dict]
    reviewed_by: str | None = None
    reason: str | None = None


@dataclass
class PersonaPlan:
    icp_id: int | None
    mappings: list[RoleMapping] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def usable(self) -> list[RoleMapping]:
        return [m for m in self.mappings if m.status in USABLE]

    @property
    def ready(self) -> bool:
        return bool(self.usable)


def _row(r) -> RoleMapping:
    return RoleMapping(r["id"], r["icp_id"], r["product_id"], r["persona"], r["persona_source"],
                       json.loads(r["titles_json"]), json.loads(r["seniorities_json"]), r["status"],
                       json.loads(r["evidence_json"]), r["reviewed_by"], r["reason"])


def _clean_list(values, what: str) -> list[str]:
    out = []
    for v in values or []:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{what} must be non-empty strings")
        v = " ".join(v.split())
        if any(c in v for c in '"\\\n') or len(v) > 120:
            raise ValueError(f"invalid {what}: {v!r}")
        if v not in out:
            out.append(v)
    return out


def _clean_seniorities(values) -> list[str]:
    sen = _clean_list(values or [], "seniority")
    if bad := [s for s in sen if s not in CLAY_SENIORITIES]:
        raise ValueError(f"unknown Clay seniority values: {bad} (allowed: {sorted(CLAY_SENIORITIES)})")
    return sen


def _require(reviewer: str, reason: str) -> tuple[str, str]:
    if not (reviewer or "").strip() or not (reason or "").strip():
        raise ValueError("reviewer and reason are required")
    return reviewer.strip(), reason.strip()


def make_evidence(text: str, url: str | None = None, source: str = "human_review") -> list[dict]:
    """Supporting evidence supplied by the reviewer (a quote, document reference or note). Required."""
    if not (text or "").strip():
        raise ValueError("supporting evidence is required")
    if url is not None and not url.strip().lower().startswith(("http://", "https://")):
        raise ValueError(f"evidence url must be http(s): {url!r}")
    return [{"source_path": source, "quote": text.strip(), "field": "persona_role_mappings", "url": url,
             "verification": "human_provided"}]


def get_mapping(con, mapping_id: int) -> RoleMapping:
    r = con.execute("SELECT * FROM persona_role_mappings WHERE id=?", (mapping_id,)).fetchone()
    if r is None:
        raise ValueError(f"no persona mapping #{mapping_id}")
    return _row(r)


def _history(con, m: RoleMapping | None, mapping_id: int, action: str, to_status: str, reviewer: str, reason: str,
             evidence: list[dict]) -> None:
    con.execute("INSERT INTO persona_mapping_history(mapping_id,action,from_status,to_status,snapshot_json,reviewer,"
                "reason,evidence_json,at) VALUES(?,?,?,?,?,?,?,?,?)",
                (mapping_id, action, m.status if m else None, to_status, json.dumps(asdict(m) if m else None),
                 reviewer, reason, json.dumps(evidence), now()))


def history(con, mapping_id: int) -> list[dict]:
    return [{**dict(r), "snapshot": json.loads(r["snapshot_json"]), "evidence": json.loads(r["evidence_json"])}
            for r in con.execute("SELECT * FROM persona_mapping_history WHERE mapping_id=? ORDER BY id",
                                 (mapping_id,))]


def mappings_for(con, icp_id: int | None = None, product_id: str | None = None) -> list[RoleMapping]:
    if product_id is not None:
        q, a = "product_id=? OR (icp_id IS NOT NULL AND icp_id IS ?)", (product_id, icp_id)
    else:
        q, a = "icp_id IS ?", (icp_id,)
    return [_row(r) for r in con.execute(f"SELECT * FROM persona_role_mappings WHERE {q} ORDER BY id", a)]


def plan_personas(con, icp_id: int | None, icp: ICP, product_id: str | None = None) -> PersonaPlan:
    """Record catalogue-stated personas as PENDING mappings (once), and return every mapping for the product/ICP.
    Nothing here approves anything."""
    plan = PersonaPlan(icp_id)
    existing = {m.persona for m in mappings_for(con, icp_id, product_id) if m.persona_source == "catalogue"}
    if icp.buyer_roles is not None:
        ev = [e.model_dump() for e in icp.buyer_roles.evidence]
        for persona in _clean_list(icp.buyer_roles.values, "buyer persona"):
            if persona in existing:
                continue
            cur = con.execute("INSERT INTO persona_role_mappings(icp_id,product_id,persona,persona_source,titles_json,"
                              "seniorities_json,status,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                              (icp_id, product_id, persona, "catalogue", json.dumps([persona]), "[]", "pending",
                               json.dumps(ev), now()))
            _history(con, None, cur.lastrowid, "create", "pending", "system:catalogue",
                     "persona stated in catalogue buyer_roles", ev)
        con.commit()
    plan.mappings = mappings_for(con, icp_id, product_id)
    if not plan.mappings:
        plan.issues.append(NO_PERSONA_ISSUE)
    elif not plan.usable:
        plan.issues.append("needs_human_review: no approved persona mapping (pending or rejected only)")
    return plan


def add_human_mapping(con, icp_id: int | None, persona: str, *, titles: list[str],
                      seniorities: list[str] | None = None, reviewer: str, reason: str, evidence: list[dict],
                      product_id: str | None = None) -> int:
    """Record a reviewer-entered persona. It starts PENDING; approval is a separate, explicit step."""
    reviewer, reason = _require(reviewer, reason)
    if not evidence or not all((e.get("quote") or "").strip() for e in evidence):
        raise ValueError("supporting evidence is required")
    persona = _clean_list([persona], "persona")[0]
    titles = _clean_list(titles, "title")
    sen = _clean_seniorities(seniorities)
    if not titles and not sen:
        raise ValueError("at least one title or seniority is required")
    if product_id is not None and not con.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
        raise ValueError(f"unknown product {product_id!r}")
    cur = con.execute("INSERT INTO persona_role_mappings(icp_id,product_id,persona,persona_source,titles_json,"
                      "seniorities_json,status,evidence_json,created_at,reviewed_by,reason) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (icp_id, product_id, persona, "human", json.dumps(titles), json.dumps(sen), "pending",
                       json.dumps(evidence), now(), reviewer, reason))
    _history(con, None, cur.lastrowid, "create", "pending", reviewer, reason, evidence)
    con.commit()
    return cur.lastrowid


def review_mapping(con, mapping_id: int, status: str, *, reviewer: str, reason: str,
                   evidence: list[dict] | None = None) -> None:
    """Human approve/reject. Evidence is optional here (the mapping already carries its own)."""
    if status not in {"approved", "rejected"}:
        raise ValueError(f"invalid status {status!r}")
    reviewer, reason = _require(reviewer, reason)
    m = get_mapping(con, mapping_id)
    if m.status == status:
        raise ValueError(f"mapping #{mapping_id} is already {status}")
    _history(con, m, mapping_id, "approve" if status == "approved" else "reject", status, reviewer, reason,
             evidence or [])
    con.execute("UPDATE persona_role_mappings SET status=?, reviewed_by=?, reviewed_at=?, reason=? WHERE id=?",
                (status, reviewer, now(), reason, mapping_id))
    con.commit()


def update_mapping(con, mapping_id: int, *, reviewer: str, reason: str, evidence: list[dict],
                   persona: str | None = None, titles: list[str] | None = None,
                   seniorities: list[str] | None = None) -> RoleMapping:
    """Edit a mapping. The previous version is snapshotted in history; the mapping returns to PENDING."""
    reviewer, reason = _require(reviewer, reason)
    if not evidence or not all((e.get("quote") or "").strip() for e in evidence):
        raise ValueError("supporting evidence is required")
    m = get_mapping(con, mapping_id)
    new_persona = _clean_list([persona], "persona")[0] if persona is not None else m.persona
    new_titles = _clean_list(titles, "title") if titles is not None else m.titles
    new_sen = _clean_seniorities(seniorities) if seniorities is not None else m.seniorities
    if not new_titles and not new_sen:
        raise ValueError("at least one title or seniority is required")
    _history(con, m, mapping_id, "update", "pending", reviewer, reason, evidence)
    con.execute("UPDATE persona_role_mappings SET persona=?, titles_json=?, seniorities_json=?, evidence_json=?, "
                "status='pending', reviewed_by=?, reviewed_at=?, reason=? WHERE id=?",
                (new_persona, json.dumps(new_titles), json.dumps(new_sen), json.dumps(m.evidence + evidence),
                 reviewer, now(), reason, mapping_id))
    con.commit()
    return get_mapping(con, mapping_id)
