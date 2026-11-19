"""Agent 5 - Email Hunter: approved prospects -> professional email findings with source, confidence, review.

Order (cheapest first, paid last and only with explicit approval):
  1. Company website: public pages (home, contact, about, team, leadership), robots.txt respected. An address
     published there that matches the person's name is the strongest free evidence.
  2. Domain pattern: infer the address format from named addresses on the site (other people we know at the company,
     or the person's own address) -> apply it to this person.
  3. Clay `add-contact-data-points` (Email) - PAID. Only with a SpendApproval, only for people that came from a
     Clay search-contacts task (its taskId + entityId are required by the real tool schema), capped per approval.

Classification (stored in email_records.status):
  valid      - published verbatim on the company's own website, matches the person's name, domain has MX.
               ("valid" = published by the organisation, NOT SMTP-verified.)
  risky      - pattern-inferred with named evidence from the same domain, and the domain has MX.
  unverified - pattern from local-part shape only or from no evidence (prior), MX unknown, or a Clay value that
               carries no verification status.
  not_found  - no usable domain/name, or the domain has no MX record.
An MX record is never treated as proof that a mailbox exists. Every row starts `pending_review`.

Inputs (gates): person role `approved` (people/role_review.py) at a company that is human-`qualified`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from chanakya.emailhunter.checks import (MXResult, Page, ROLE_LOCALS, apply_pattern, check_mx, contact_urls,
                                         extract_emails, name_parts, on_domain, patterns_matching, valid_syntax)
from chanakya.storage.db import finish_run, jdump, now, start_run

AGENT = "email_hunter"
DEFAULT_LIMIT, MAX_LIMIT = 10, 100
STATUSES = ("valid", "risky", "unverified", "not_found")
DEFAULT_PRIOR = "first.last"  # used only as a labelled prior -> always `unverified`


@dataclass
class EmailCandidate:
    person_id: int
    full_name: str
    company_id: int
    domain: str
    clay_task_id: str | None = None
    clay_entity_id: str | None = None


@dataclass
class Finding:
    email: str | None
    method: str  # website | pattern | clay | none
    status: str
    confidence: float
    pattern: str | None = None
    source_url: str | None = None
    evidence: list[dict] = field(default_factory=list)
    raw_response_id: int | None = None


@dataclass
class EmailReport:
    dry_run: bool
    planned: list[dict] = field(default_factory=list)
    record_ids: list[int] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    run_id: int | None = None
    paid_used: int = 0


def _entity_id(raw: dict) -> str | None:
    for k in ("entityId", "entity_id", "id", "contactId"):
        v = raw.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v)
    return None


def select_candidates(con, *, person_ids: list[int] | None = None) -> list[EmailCandidate]:
    sql = ("SELECT p.id pid, p.full_name, c.id cid, c.domain FROM people p JOIN person_roles r ON r.person_id=p.id "
           "JOIN companies c ON c.id=r.company_id WHERE r.review_status='approved' AND c.qualification_status="
           "'qualified' AND c.qualification_source='human' AND NOT EXISTS (SELECT 1 FROM email_records e WHERE "
           "e.person_id=p.id AND e.review_status IN ('pending_review','approved')) ")
    params: list = []
    if person_ids:
        sql += f"AND p.id IN ({','.join('?' * len(person_ids))}) "
        params += person_ids
    out, seen = [], set()
    for r in con.execute(sql + "ORDER BY p.id, COALESCE(r.is_current,0) DESC", params):
        if r["pid"] in seen:
            continue
        seen.add(r["pid"])
        src = con.execute("SELECT task_id, raw_json FROM person_sources WHERE person_id=? AND task_id IS NOT NULL "
                          "AND tool='search-contacts' ORDER BY id DESC LIMIT 1", (r["pid"],)).fetchone()
        tid = src["task_id"] if src else None
        ent = _entity_id(json.loads(src["raw_json"])) if src else None
        out.append(EmailCandidate(r["pid"], r["full_name"], r["cid"], r["domain"], tid, ent))
    return out


def known_names(con, company_id: int, exclude_person: int) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT p.full_name FROM people p JOIN person_roles r ON "
                                      "r.person_id=p.id WHERE r.company_id=? AND p.id!=?", (company_id, exclude_person))]


def infer_pattern(domain: str, site_emails: list[tuple[str, str]], names: list[str]) -> dict:
    """site_emails = [(email, source_url)]. Named evidence = an on-domain address equal to a pattern applied to a known
    person at the company. Shape evidence = 'a.b' style local parts (weak). Returns pattern, confidence, basis."""
    votes: dict[str, list[dict]] = {}
    for email, url in site_emails:
        if not on_domain(email, domain):
            continue
        local = email.split("@")[0]
        if local in ROLE_LOCALS:
            continue
        for n in names:
            for p in patterns_matching(local, n):
                votes.setdefault(p, []).append({"email": email, "name": n, "source_url": url, "kind": "named"})
    if votes:
        best = max(votes, key=lambda p: (len(votes[p]), p == DEFAULT_PRIOR))
        n = len(votes[best])
        return {"pattern": best, "basis": "named", "support": n, "confidence": round(min(0.5 + 0.1 * n, 0.75), 2),
                "evidence": votes[best], "competing": {p: len(v) for p, v in votes.items() if p != best}}
    shaped = [{"email": e, "source_url": u, "kind": "shape"} for e, u in site_emails if on_domain(e, domain)
              and e.split("@")[0] not in ROLE_LOCALS and "." in e.split("@")[0]
              and all(t.isalpha() and len(t) > 1 for t in e.split("@")[0].split("."))]
    if shaped:
        return {"pattern": "first.last", "basis": "shape", "support": len(shaped), "confidence": 0.3,
                "evidence": shaped, "competing": {}}
    return {"pattern": DEFAULT_PRIOR, "basis": "prior", "support": 0, "confidence": 0.15, "evidence": [],
            "competing": {}}


def classify(cand: EmailCandidate, site_emails: list[tuple[str, str]], inference: dict, mx: MXResult) -> Finding:
    f, l = name_parts(cand.full_name)
    if not cand.domain or not f:
        return Finding(None, "none", "not_found", 0.0, evidence=[{"reason": "no usable domain or name"}])
    if mx.status == "none":
        return Finding(None, "none", "not_found", 0.0, evidence=[{"reason": f"{cand.domain} has no MX record",
                                                                  "mx": mx.as_dict()}])
    # 1. the person's own address published on the company site
    for email, url in site_emails:
        local = email.split("@")[0]
        if on_domain(email, cand.domain) and local not in ROLE_LOCALS and (
                patterns_matching(local, cand.full_name) or (l and f in local and l in local)):
            status = "valid" if mx.status == "ok" else "unverified"
            conf = 0.9 if mx.status == "ok" else 0.6
            return Finding(email, "website", status, conf, (patterns_matching(local, cand.full_name) or [None])[0],
                           url, [{"kind": "published_on_company_site", "email": email, "source_url": url}])
    # 2. pattern
    email = apply_pattern(inference["pattern"], cand.full_name, cand.domain)
    if not email or not valid_syntax(email):
        return Finding(None, "none", "not_found", 0.0, evidence=[{"reason": "name does not fit an address pattern"}])
    conf = inference["confidence"] * (0.7 if mx.status != "ok" else 1.0)
    status = "risky" if inference["basis"] == "named" and mx.status == "ok" else "unverified"
    ev = [{"kind": f"pattern_{inference['basis']}", "pattern": inference["pattern"], "support": inference["support"],
           "examples": inference["evidence"][:5], "competing_patterns": inference["competing"]}]
    return Finding(email, "pattern", status, round(conf, 2), inference["pattern"], None, ev)


def _store_page(con, domain, p: Page, emails, run_id):
    con.execute("INSERT INTO website_pages(domain,url,http_status,robots_allowed,sha256,emails_json,error,run_id,"
                "fetched_at) VALUES(?,?,?,?,?,?,?,?,?)", (domain, p.url, p.status, int(p.robots_allowed), p.sha256,
                                                         json.dumps(emails), p.error, run_id, now()))


def scrape_domain(con, domain: str, fetcher: Callable[[str], Page], run_id: int | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for url in contact_urls(domain):
        p = fetcher(url)
        emails = extract_emails(p.text) if p.text and (p.status or 0) < 400 else []
        _store_page(con, domain, p, emails, run_id)
        con.commit()  # never hold SQLite's write lock across the next (slow) page fetch
        out += [(e, p.url) for e in emails if (e, p.url) not in out]
    return out


def store_finding(con, c: EmailCandidate, f: Finding, mx: MXResult, run_id) -> int:
    rid = con.execute("INSERT INTO email_records(person_id,company_id,run_id,email,method,status,confidence,pattern,"
                      "source_url,evidence_json,mx_json,raw_response_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (c.person_id, c.company_id, run_id, f.email, f.method, f.status, f.confidence, f.pattern,
                       f.source_url, jdump(f.evidence), jdump(mx.as_dict()), f.raw_response_id, now())).lastrowid
    con.execute("INSERT INTO email_record_history(email_record_id,action,from_status,to_status,email,reviewer,reason,"
                "at) VALUES(?,?,?,?,?,?,?,?)", (rid, "create", None, "pending_review", f.email, AGENT,
                                                 f"{f.method}:{f.status}", now()))
    return rid


def run_email_hunter(con, *, allow_live: bool = False, limit: int = DEFAULT_LIMIT,
                     person_ids: list[int] | None = None, fetcher: Callable[[str], Page] | None = None,
                     mx_lookup: Callable[[str], MXResult] = check_mx, paid=None) -> EmailReport:
    """`paid` = an emailhunter.clay.PaidEmailLookup (or None). It is consulted only for people the free methods left
    `unverified`/`not_found`, and it enforces its own SpendApproval cap."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    cands = select_candidates(con, person_ids=person_ids)[:limit]
    rep = EmailReport(dry_run=not allow_live)
    rep.planned = [{"person_id": c.person_id, "name": c.full_name, "domain": c.domain, "pages": contact_urls(c.domain),
                    "clay_paid_possible": bool(paid and c.clay_task_id and c.clay_entity_id)} for c in cands]
    if not allow_live or not cands:
        rep.counts = {"candidates": len(cands)}
        return rep
    if fetcher is None:
        from chanakya.emailhunter.checks import default_fetcher
        fetcher = default_fetcher()
    run_id = start_run(con, AGENT, {"limit": limit, "candidates": len(cands), "paid": bool(paid)})
    rep.run_id = run_id
    counts = {s: 0 for s in STATUSES}
    sites: dict[str, list] = {}
    mxs: dict[str, MXResult] = {}
    try:
        for c in cands:
            if c.domain not in sites:
                sites[c.domain] = scrape_domain(con, c.domain, fetcher, run_id)
                mxs[c.domain] = mx_lookup(c.domain)
                inf = infer_pattern(c.domain, sites[c.domain], known_names(con, c.company_id, -1))
                con.execute("INSERT INTO domain_email_patterns(domain,pattern,confidence,basis,evidence_json,mx_json,"
                            "run_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (c.domain, inf["pattern"], inf["confidence"], inf["basis"], jdump(inf),
                             jdump(mxs[c.domain].as_dict()), run_id, now()))
            inf = infer_pattern(c.domain, sites[c.domain], known_names(con, c.company_id, c.person_id))
            f = classify(c, sites[c.domain], inf, mxs[c.domain])
            if paid is not None and f.status in ("unverified", "not_found") and c.clay_task_id and c.clay_entity_id \
                    and mxs[c.domain].status != "none":
                pf = paid.lookup(con, c, run_id)
                if pf is not None:
                    rep.paid_used += 1
                    f = pf
            rep.record_ids.append(store_finding(con, c, f, mxs[c.domain], run_id))
            counts[f.status] += 1
            con.commit()
    except BaseException as e:
        con.commit()
        finish_run(con, run_id, "failed", {"counts": counts, "error": repr(e)})
        raise
    rep.counts = counts
    finish_run(con, run_id, "ok", {"counts": counts, "record_ids": rep.record_ids, "paid_used": rep.paid_used})
    return rep


# ---------------- human review ----------------
class EmailReviewError(ValueError):
    pass


def _hist(con, rid, action, frm, to, email, by, reason):
    con.execute("INSERT INTO email_record_history(email_record_id,action,from_status,to_status,email,reviewer,reason,"
                "at) VALUES(?,?,?,?,?,?,?,?)", (rid, action, frm, to, email, by, reason, now()))


def _need(by, reason):
    if not (by or "").strip() or not (reason or "").strip():
        raise EmailReviewError("reviewer name and reason are required")


def get_record(con, rid: int):
    r = con.execute("SELECT * FROM email_records WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise EmailReviewError(f"unknown email record {rid}")
    return r


def approve(con, rid: int, *, by: str, reason: str) -> dict:
    _need(by, reason)
    r = get_record(con, rid)
    if r["review_status"] != "pending_review":
        raise EmailReviewError(f"email record {rid} is already {r['review_status']}")
    if not r["email"]:
        raise EmailReviewError("nothing to approve (no address); use `override` with evidence or `reject`")
    other = con.execute("SELECT id, email FROM email_records WHERE person_id=? AND review_status='approved' AND id!=?",
                        (r["person_id"], rid)).fetchone()
    if other:
        raise EmailReviewError(f"person already has approved email #{other['id']} ({other['email']}); reject it first")
    con.execute("UPDATE email_records SET review_status='approved', reviewed_by=?, reviewed_at=?, review_reason=? "
                "WHERE id=?", (by, now(), reason, rid))
    _hist(con, rid, "approve", "pending_review", "approved", r["email"], by, reason)
    con.commit()
    return dict(get_record(con, rid))


def reject(con, rid: int, *, by: str, reason: str) -> None:
    _need(by, reason)
    r = get_record(con, rid)
    if r["review_status"] == "rejected":
        raise EmailReviewError(f"email record {rid} is already rejected")
    con.execute("UPDATE email_records SET review_status='rejected', reviewed_by=?, reviewed_at=?, review_reason=? "
                "WHERE id=?", (by, now(), reason, rid))
    _hist(con, rid, "reject", r["review_status"], "rejected", r["email"], by, reason)
    con.commit()


def override(con, person_id: int, email: str, *, by: str, reason: str, evidence_url: str | None = None) -> int:
    """A human supplies an address they verified themselves. Stored as method 'human', approved, with evidence."""
    _need(by, reason)
    email = (email or "").strip().lower()
    if not valid_syntax(email):
        raise EmailReviewError(f"invalid email syntax: {email!r}")
    row = con.execute("SELECT r.company_id FROM person_roles r WHERE r.person_id=? ORDER BY r.id LIMIT 1",
                      (person_id,)).fetchone()
    if row is None:
        raise EmailReviewError(f"unknown person {person_id}")
    for o in con.execute("SELECT id, review_status FROM email_records WHERE person_id=? AND review_status IN "
                         "('pending_review','approved')", (person_id,)):
        reject(con, o["id"], by=by, reason=f"superseded by human override: {reason}")
    rid = con.execute("INSERT INTO email_records(person_id,company_id,email,method,status,confidence,source_url,"
                      "evidence_json,review_status,reviewed_by,reviewed_at,review_reason,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (person_id, row["company_id"], email, "human", "valid" if evidence_url else "unverified",
                       0.95 if evidence_url else 0.5, evidence_url,
                       jdump([{"kind": "human_override", "reason": reason, "source_url": evidence_url}]),
                       "approved", by, now(), reason, now())).lastrowid
    _hist(con, rid, "override", None, "approved", email, by, reason)
    con.commit()
    return rid


def list_records(con, status: str | None = None) -> list[dict]:
    sql = "SELECT e.*, p.full_name FROM email_records e JOIN people p ON p.id=e.person_id"
    rows = con.execute(sql + (" WHERE e.review_status=?" if status else "") + " ORDER BY e.id",
                       (status,) if status else ()).fetchall()
    return [dict(r) for r in rows]


def history(con, rid: int) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM email_record_history WHERE email_record_id=? ORDER BY id",
                                         (rid,))]


def approved_email(con, person_id: int) -> dict | None:
    r = con.execute("SELECT * FROM email_records WHERE person_id=? AND review_status='approved' ORDER BY id DESC "
                    "LIMIT 1", (person_id,)).fetchone()
    return dict(r) if r else None

