"""Human review of LinkedIn resolutions (Agent 4). Local only, no network.

`approve` accepts the harness's profile URL, or one of its alternatives / a URL from the stored candidates, and
only then copies it to `people.linkedin_url`. The resolution keeps `verification='human_reviewed'`; the person keeps
`people.verification` unchanged (identity verification is a separate step). Every action goes to
linkedin_resolution_history.
"""
from __future__ import annotations

import json

from chanakya.people.finder import normalize_linkedin
from chanakya.storage.db import now

OPEN = "pending_review"


class ReviewError(ValueError):
    pass


def get(con, rid: int):
    r = con.execute("SELECT * FROM linkedin_resolutions WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise ReviewError(f"unknown resolution {rid}")
    return r


def candidate_urls(r) -> list[str]:
    urls = []
    for item in [json.loads(r["best_json"] or "null")] + json.loads(r["alternatives_json"] or "[]"):
        u = normalize_linkedin((item or {}).get("url")) if isinstance(item, dict) else None
        if u and u not in urls:
            urls.append(u)
    if r["profile_url"] and r["profile_url"] not in urls:
        urls.insert(0, r["profile_url"])
    return urls


def _need(by: str, reason: str) -> None:
    if not (by or "").strip() or not (reason or "").strip():
        raise ReviewError("reviewer name and reason are required")


def _history(con, rid, action, frm, to, url, by, reason):
    con.execute("INSERT INTO linkedin_resolution_history(resolution_id,action,from_status,to_status,url,reviewer,"
                "reason,at) VALUES(?,?,?,?,?,?,?,?)", (rid, action, frm, to, url, by, reason, now()))


def approve(con, rid: int, *, by: str, reason: str, url: str | None = None) -> str:
    """Approve the resolution. `url` defaults to the harness profile_url; otherwise it must be one of the stored
    candidates (best/alternatives), so a reviewer cannot attach an unrelated, unevidenced URL here."""
    _need(by, reason)
    r = get(con, rid)
    if r["review_status"] != OPEN:
        raise ReviewError(f"resolution {rid} is already {r['review_status']}")
    if r["status"] == "error":
        raise ReviewError(f"resolution {rid} is an error record; re-run the resolver instead")
    allowed = candidate_urls(r)
    chosen = normalize_linkedin(url) if url else r["profile_url"]
    if not chosen:
        raise ReviewError("no profile URL to approve; pass --url with one of: " + (", ".join(allowed) or "(none)"))
    if chosen not in allowed:
        raise ReviewError(f"{chosen} is not among this resolution's candidates: {', '.join(allowed) or '(none)'}")
    other = con.execute("SELECT id FROM people WHERE linkedin_url=? AND id!=?", (chosen, r["person_id"])).fetchone()
    if other:
        raise ReviewError(f"{chosen} already belongs to person {other['id']}")
    t = now()
    con.execute("UPDATE linkedin_resolutions SET review_status='approved', verification='human_reviewed', "
                "approved_url=?, reviewed_by=?, reviewed_at=?, review_reason=? WHERE id=?",
                (chosen, by, t, reason, rid))
    con.execute("UPDATE people SET linkedin_url=? WHERE id=?", (chosen, r["person_id"]))
    _history(con, rid, "approve", OPEN, "approved", chosen, by, reason)
    con.commit()
    return chosen


def reject(con, rid: int, *, by: str, reason: str) -> None:
    _need(by, reason)
    r = get(con, rid)
    if r["review_status"] != OPEN:
        raise ReviewError(f"resolution {rid} is already {r['review_status']}")
    con.execute("UPDATE linkedin_resolutions SET review_status='rejected', reviewed_by=?, reviewed_at=?, "
                "review_reason=? WHERE id=?", (by, now(), reason, rid))
    _history(con, rid, "reject", OPEN, "rejected", None, by, reason)
    con.commit()


def list_resolutions(con, status: str | None = None) -> list:
    sql = ("SELECT lr.*, p.full_name FROM linkedin_resolutions lr JOIN people p ON p.id=lr.person_id")
    if status:
        return con.execute(sql + " WHERE lr.review_status=? ORDER BY lr.id", (status,)).fetchall()
    return con.execute(sql + " ORDER BY lr.id").fetchall()


def history(con, rid: int) -> list:
    return con.execute("SELECT * FROM linkedin_resolution_history WHERE resolution_id=? ORDER BY id",
                       (rid,)).fetchall()
