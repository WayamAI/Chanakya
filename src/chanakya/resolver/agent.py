"""Agent 4 - LinkedIn Resolver: People Finder people -> candidate LinkedIn profile URLs (via the external harness).

Rules enforced here:
  * Dry run by default: `run_resolver(..., allow_live=False)` only returns the planned harness commands.
  * Strictly sequential: one person at a time, plus a cross-process lock file so two runs can never overlap
    (they would share one LinkedIn account and its daily page budget).
  * A small per-run limit (default 3, hard cap 25).
  * Live runs start with a harness `status` check; login / page-limit / security-check errors stop the run.
  * Every attempt is stored in `linkedin_resolutions` with confidence, evidence (best candidate, alternatives,
    searches, the verbatim harness JSON and its saved path) and the harness version.
  * Nothing is trusted automatically: every row is `verification='unverified'`, `review_status='pending_review'`.
    `people.linkedin_url` is only written by a human approval (see resolver/review.py).
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from chanakya.people.finder import normalize_linkedin
from chanakya.resolver.harness_client import HarnessClient, HarnessError, HarnessResult
from chanakya.storage.db import finish_run, now, start_run

AGENT = "linkedin_resolver"
DEFAULT_LIMIT = 3
MAX_LIMIT = 25
STOP_KINDS = frozenset({"not_logged_in", "page_limit", "security_check", "not_installed", "not_configured",
                        "policy_disabled"})
KNOWN_CONFIDENCE = frozenset({"high", "medium", "low"})
# Only roles a human approved (people/role_review.py) are ever resolved. There is deliberately no override.
REQUIRED_ROLE_STATUS = "approved"


class ResolverBlocked(RuntimeError):
    pass


@dataclass
class Candidate:
    person_id: int
    full_name: str
    person_role_id: int
    title: str | None
    company_id: int
    company: str
    role_review_status: str

    def input(self) -> dict:
        return {"name": self.full_name, "company": self.company, "title": self.title or ""}


@dataclass
class ResolverReport:
    dry_run: bool
    planned: list[dict] = field(default_factory=list)
    resolution_ids: list[int] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    stopped: str | None = None
    run_id: int | None = None
    harness_version: str | None = None


def select_candidates(con, *, person_ids: list[int] | None = None) -> list[Candidate]:
    """People with no LinkedIn URL, a human-approved role, and no open/approved resolution.
    One row per person (current role preferred). People who already have a URL (e.g. from Clay) are skipped."""
    sql = (f"SELECT p.id AS person_id, p.full_name, r.id AS role_id, r.title, r.review_status, c.id AS company_id, "
           f"COALESCE(NULLIF(c.name,''), c.domain) AS company FROM people p "
           f"JOIN person_roles r ON r.person_id = p.id JOIN companies c ON c.id = r.company_id "
           f"WHERE (p.linkedin_url IS NULL OR p.linkedin_url = '') AND r.review_status = ? "
           f"AND NOT EXISTS (SELECT 1 FROM linkedin_resolutions lr WHERE lr.person_id = p.id "
           f"  AND lr.status != 'error' AND lr.review_status IN ('pending_review','approved')) ")
    params: list = [REQUIRED_ROLE_STATUS]
    if person_ids:
        sql += f"AND p.id IN ({','.join('?' * len(person_ids))}) "
        params += person_ids
    sql += "ORDER BY p.id, COALESCE(r.is_current, 0) DESC, r.id"
    out, seen = [], set()
    for r in con.execute(sql, params):
        if r["person_id"] in seen:
            continue
        seen.add(r["person_id"])
        out.append(Candidate(r["person_id"], r["full_name"], r["role_id"], r["title"], r["company_id"],
                             r["company"], r["review_status"]))
    return out


@contextmanager
def single_run_lock(lock_path: Path):
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ResolverBlocked(f"another LinkedIn resolver run holds {lock_path}. If no run is active, delete it.")
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "started": now()}).encode())
        os.close(fd)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def default_lock_path() -> Path:
    return Path(os.getenv("CHANAKYA_DB_PATH", "data/chanakya.db")).parent / ".linkedin_resolver.lock"


def interpret(res: HarnessResult) -> dict:
    """Harness result -> row fields. Defensive: anything unexpected becomes manual_queue, never 'found'."""
    if not res.ok:
        return {"status": "error", "error_kind": res.error_kind, "error": res.error}
    d = res.data or {}
    conf = d.get("confidence") if d.get("confidence") in KNOWN_CONFIDENCE else "low"
    best = d.get("best") or None
    url = normalize_linkedin(d.get("profile_url"))
    status = "found" if d.get("status") == "found" and url and conf in ("high", "medium") else "manual_queue"
    score = best.get("score") if isinstance(best, dict) and isinstance(best.get("score"), (int, float)) else None
    return {"status": status, "error_kind": None, "error": None, "confidence": conf, "score": score,
            "profile_url": url if status == "found" else None, "best": best,
            "alternatives": d.get("alternatives") or [], "searches": d.get("searches") or []}


def store_resolution(con, c: Candidate, res: HarnessResult | None, fields: dict, run_id: int | None,
                     *, argv: list[str], version: str, raw: dict | None = None) -> int:
    j = lambda v: json.dumps(v, ensure_ascii=False) if v is not None else None  # noqa: E731
    cur = con.execute(
        "INSERT INTO linkedin_resolutions(person_id,person_role_id,company_id,run_id,input_json,status,error_kind,"
        "error,confidence,score,profile_url,best_json,alternatives_json,searches_json,harness_json,"
        "harness_saved_path,harness_version,harness_argv_json,verification,review_status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unverified','pending_review',?)",
        (c.person_id, c.person_role_id, c.company_id, run_id, j(c.input()), fields["status"], fields.get("error_kind"),
         fields.get("error"), fields.get("confidence"), fields.get("score"), fields.get("profile_url"),
         j(fields.get("best")), j(fields.get("alternatives")), j(fields.get("searches")),
         j(res.raw if res else (raw or {})), res.saved if res else None, version, j(argv), now()))
    return cur.lastrowid


def run_resolver(client: HarnessClient, con, *, allow_live: bool = False, limit: int = DEFAULT_LIMIT,
                 person_ids: list[int] | None = None,
                 lock_path: Path | None = None) -> ResolverReport:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    cands = select_candidates(con, person_ids=person_ids)[:limit]
    rep = ResolverReport(dry_run=not allow_live)
    rep.planned = [{"person_id": c.person_id, "input": c.input(), "argv": client.resolve_argv(**c.input())}
                   for c in cands]
    if not allow_live or not cands:
        rep.counts = {"candidates": len(cands)}
        return rep

    with single_run_lock(lock_path or default_lock_path()):
        client.check_installed()
        st = client.status()
        rep.harness_version = st.harness_version
        if not (st.ok and (st.data or {}).get("logged_in")):
            raise ResolverBlocked(f"harness session is not logged in ({st.error or st.data}). "
                                  "Run the harness `login` command by hand first.")
        run_id = start_run(con, AGENT, {"limit": limit, "candidates": len(cands), "harness_version": st.harness_version})
        rep.run_id = run_id
        counts = {"found": 0, "manual_queue": 0, "error": 0}
        try:
            for c in cands:  # strictly sequential, never parallel
                argv = client.resolve_argv(**c.input())
                try:
                    res = client.resolve(**c.input())
                    fields = interpret(res)
                    rid = store_resolution(con, c, res, fields, run_id, argv=argv, version=res.harness_version)
                except HarnessError as e:
                    fields = {"status": "error", "error_kind": e.kind, "error": str(e)}
                    rid = store_resolution(con, c, None, fields, run_id, argv=argv, version=st.harness_version,
                                           raw=e.raw or {"error": str(e)})
                con.commit()
                rep.resolution_ids.append(rid)
                counts[fields["status"]] += 1
                if fields["status"] == "error" and fields.get("error_kind") in STOP_KINDS:
                    rep.stopped = fields["error_kind"]
                    break
        except BaseException as e:
            con.commit()
            finish_run(con, run_id, "failed", {"counts": counts, "error": repr(e)})
            raise
        rep.counts = counts
        finish_run(con, run_id, "stopped" if rep.stopped else "ok", {"counts": counts, "stopped": rep.stopped,
                                                                    "resolution_ids": rep.resolution_ids})
    return rep
