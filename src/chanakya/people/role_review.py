"""Human review of People Finder roles (`person_roles.review_status`). Local only, no network.

People Finder stores every observed person/role as `needs_manual_review`. A human decides here whether the role is
a real target (`approved`) or not (`rejected`); `reopen` returns it to `needs_manual_review`. Downstream agents
(Agent 4, LinkedIn Resolver) only process `approved` roles.

Audit trail: every action appends a person_role_review_history row holding a snapshot of the role BEFORE the
change, plus reviewer, reason and evidence. History rows are never updated or deleted. Persona mappings and their
own history (persona_mapping_history) are not touched by this module.
"""
from __future__ import annotations

import json

from chanakya.storage.db import now

PENDING, APPROVED, REJECTED = "needs_manual_review", "approved", "rejected"
STATUSES = frozenset({PENDING, APPROVED, REJECTED})


class RoleReviewError(ValueError):
    pass


def _require(reviewer: str, reason: str) -> tuple[str, str]:
    r, why = (reviewer or "").strip(), (reason or "").strip()
    if not r or not why:
        raise RoleReviewError("reviewer name and reason are required")
    return r, why


def get_role(con, role_id: int) -> dict:
    r = con.execute(
        "SELECT r.*, p.full_name, p.linkedin_url, p.verification AS person_verification, c.domain, c.name AS company, "
        "c.qualification_status, c.qualification_source FROM person_roles r JOIN people p ON p.id=r.person_id "
        "JOIN companies c ON c.id=r.company_id WHERE r.id=?", (role_id,)).fetchone()
    if r is None:
        raise RoleReviewError(f"unknown person_role {role_id}")
    d = dict(r)
    d["role_match"] = json.loads(d.pop("role_match_json") or "{}")
    d["sources"] = [dict(s) for s in con.execute(
        "SELECT id, source, tool, query, fetched_at, raw_json FROM person_sources WHERE person_id=? AND company_id=? "
        "ORDER BY id", (r["person_id"], r["company_id"]))]
    return d


def list_roles(con, status: str | None = None, domain: str | None = None) -> list[dict]:
    if status and status not in STATUSES:
        raise RoleReviewError(f"status must be one of {sorted(STATUSES)}")
    sql = ("SELECT r.id, r.title, r.seniority, r.is_current, r.review_status, r.role_match_json, p.full_name, "
           "c.domain FROM person_roles r JOIN people p ON p.id=r.person_id JOIN companies c ON c.id=r.company_id "
           "WHERE 1=1")
    params: list = []
    if status:
        sql += " AND r.review_status=?"
        params.append(status)
    if domain:
        sql += " AND c.domain=?"
        params.append(domain)
    out = []
    for r in con.execute(sql + " ORDER BY r.id", params):
        d = dict(r)
        d["role_match_status"] = json.loads(d.pop("role_match_json") or "{}").get("status")
        out.append(d)
    return out


def _snapshot(con, role_id: int) -> dict:
    return dict(con.execute("SELECT * FROM person_roles WHERE id=?", (role_id,)).fetchone())


def _history(con, role_id, action, frm, to, snapshot, reviewer, reason, evidence):
    con.execute("INSERT INTO person_role_review_history(role_id,action,from_status,to_status,snapshot_json,reviewer,"
                "reason,evidence_json,at) VALUES(?,?,?,?,?,?,?,?,?)",
                (role_id, action, frm, to, json.dumps(snapshot, default=str), reviewer, reason,
                 json.dumps(evidence or []), now()))


def review_role(con, role_id: int, status: str, *, reviewer: str, reason: str,
                evidence: list[dict] | None = None) -> dict:
    """Set a role to approved / rejected / needs_manual_review (reopen). Approval requires the company to be
    human-qualified, matching the People Finder input rule."""
    if status not in STATUSES:
        raise RoleReviewError(f"status must be one of {sorted(STATUSES)}")
    reviewer, reason = _require(reviewer, reason)
    role = get_role(con, role_id)
    if role["review_status"] == status:
        raise RoleReviewError(f"role {role_id} is already {status}")
    if status == APPROVED and not (role["qualification_status"] == "qualified"
                                   and role["qualification_source"] == "human"):
        raise RoleReviewError(f"company {role['domain']} is not human-qualified; qualify it before approving roles")
    action = {APPROVED: "approve", REJECTED: "reject", PENDING: "reopen"}[status]
    _history(con, role_id, action, role["review_status"], status, _snapshot(con, role_id), reviewer, reason, evidence)
    con.execute("UPDATE person_roles SET review_status=? WHERE id=?", (status, role_id))
    con.commit()
    try:  # the access score depends on approved roles: rescore the account's opportunities now
        from chanakya.intel.scoring import rescore_company
        rescore_company(con, role["company_id"])
    except Exception:  # noqa: BLE001 - scoring tables may be absent in narrow tests; the review itself stands
        pass
    return get_role(con, role_id)


def history(con, role_id: int) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM person_role_review_history WHERE role_id=? ORDER BY id", (role_id,)):
        d = dict(r)
        d["snapshot"] = json.loads(d.pop("snapshot_json"))
        d["evidence"] = json.loads(d.pop("evidence_json"))
        out.append(d)
    return out
