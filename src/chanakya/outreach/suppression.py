"""Do-not-contact list. A person, an email address or a whole domain can be suppressed (e.g. an objection under
UK GDPR Art. 21 or an opt-out). Draft generation checks it first; suppressed people get no draft."""
from __future__ import annotations

from chanakya.storage.db import now


class Suppressed(RuntimeError):
    pass


def add(con, *, by: str, reason: str, person_id: int | None = None, email: str | None = None,
        domain: str | None = None) -> int:
    if not (person_id or email or domain):
        raise ValueError("give person_id, email or domain")
    if not (by or "").strip() or not (reason or "").strip():
        raise ValueError("by and reason are required")
    sid = con.execute("INSERT INTO suppression(person_id,email,domain,reason,added_by,added_at) VALUES(?,?,?,?,?,?)",
                      (person_id, (email or "").lower() or None, (domain or "").lower() or None, reason, by,
                       now())).lastrowid
    con.commit()
    return sid


def listing(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM suppression ORDER BY id DESC")]


def reason_for(con, person_id: int) -> str | None:
    """Why this person must not be contacted, or None."""
    r = con.execute("SELECT reason FROM suppression WHERE person_id=?", (person_id,)).fetchone()
    if r:
        return r[0]
    emails = [x[0].lower() for x in con.execute("SELECT email FROM email_records WHERE person_id=? AND email IS NOT "
                                                "NULL", (person_id,))]
    for e in emails:
        r = con.execute("SELECT reason FROM suppression WHERE email=? OR domain=?", (e, e.split("@")[-1])).fetchone()
        if r:
            return r[0]
    r = con.execute("SELECT s.reason FROM suppression s JOIN companies c ON c.domain=s.domain JOIN person_roles pr ON "
                    "pr.company_id=c.id WHERE pr.person_id=? LIMIT 1", (person_id,)).fetchone()
    return r[0] if r else None
