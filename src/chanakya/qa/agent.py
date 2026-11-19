"""Agent 7 - QA / Sync: one structured, checked prospect record per person, built from every agent's tables.

Checks (each one appended to qa_results on every run, never overwritten):
  company_qualified, role_approved, linkedin_present, linkedin_consistent, email_present, email_domain_match,
  email_candidates_conflict, research_approved, research_profile_match, provenance_person, provenance_company,
  duplicate_person, duplicate_company.
Score (deterministic, 0-100): company 20 + role 15 + LinkedIn 20 + email (valid 25 / risky 12 / unverified 5)
  + research (high 20 / medium 14 / low 6).
Tiers: High >= 80, Medium >= 55, Low otherwise.
qa_status: blocked (company not human-qualified or role not approved), needs_review (any conflict, or tier Low),
  passed (otherwise). Conflicts are recorded and surfaced; nothing is merged or dropped silently.
A prospect a human approved goes back to pending_review if a later QA run changes its record.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path

from chanakya.emailhunter.checks import on_domain
from chanakya.people.finder import normalize_name
from chanakya.storage.db import finish_run, jdump, now, start_run

AGENT = "qa_sync"
W_EMAIL = {"valid": 25, "risky": 12, "unverified": 5}
W_RESEARCH = {"high": 20, "medium": 14, "low": 6}
TIERS = (("High", 80), ("Medium", 55), ("Low", 0))
EXPORT_COLUMNS = ["prospect_id", "person_id", "name", "title", "company", "domain", "linkedin_url", "linkedin_source",
                  "email", "email_status", "email_method", "email_source_url", "research_confidence", "designation",
                  "accelerators", "origin", "score", "tier", "qa_status", "review_status", "issues", "conflicts"]


def tier_of(score: int) -> str:
    return next(t for t, lo in TIERS if score >= lo)


def _norm_company(s: str | None) -> str:
    stop = r"\b(pvt|private|ltd|limited|inc|llc|llp|co|corp|corporation|the)\b"
    return " ".join(re.sub(stop, " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).split())


def duplicates(con) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """(person_id -> conflicts, company_id -> conflicts). Same normalized name at the same company under different
    person ids, or one LinkedIn URL on two people; companies with the same normalized name on different domains."""
    pc: dict[int, list[dict]] = {}
    groups: dict[tuple, list[int]] = {}
    for r in con.execute("SELECT DISTINCT p.id, p.full_name, r.company_id FROM people p JOIN person_roles r ON "
                         "r.person_id=p.id"):
        groups.setdefault((normalize_name(r["full_name"]), r["company_id"]), []).append(r["id"])
    for (name, cid), ids in groups.items():
        ids = sorted(set(ids))
        for i in ids if len(ids) > 1 else []:
            pc.setdefault(i, []).append({"check": "duplicate_person", "normalized_name": name, "company_id": cid,
                                         "other_person_ids": [x for x in ids if x != i]})
    cc: dict[int, list[dict]] = {}
    cg: dict[str, list] = {}
    for r in con.execute("SELECT id, name, domain FROM companies"):
        if _norm_company(r["name"]):
            cg.setdefault(_norm_company(r["name"]), []).append((r["id"], r["domain"]))
    for name, rows in cg.items():
        if len({d for _, d in rows}) > 1:
            for cid, _ in rows:
                cc.setdefault(cid, []).append({"check": "duplicate_company", "normalized_name": name,
                                               "others": [x for x in rows if x[0] != cid]})
    return pc, cc


def build_record(con, person_id: int) -> dict:
    p = con.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    role = con.execute("SELECT r.*, c.domain, c.name cname, c.qualification_status, c.qualification_source FROM "
                       "person_roles r JOIN companies c ON c.id=r.company_id WHERE r.person_id=? ORDER BY "
                       "(r.review_status='approved') DESC, COALESCE(r.is_current,0) DESC, r.id LIMIT 1",
                       (person_id,)).fetchone()
    res = con.execute("SELECT id, approved_url FROM linkedin_resolutions WHERE person_id=? AND review_status='approved' "
                      "ORDER BY id DESC LIMIT 1", (person_id,)).fetchone()
    srcs = [r["source"] for r in con.execute("SELECT DISTINCT source FROM person_sources WHERE person_id=?",
                                             (person_id,))]
    lead = con.execute("SELECT id, matches_json FROM bottomup_leads WHERE person_id=? ORDER BY id LIMIT 1",
                       (person_id,)).fetchone()
    if res:
        li_source = f"resolver#{res['id']} (human-approved)"
    elif p["linkedin_url"] and lead:
        li_source = "linkedin people search (bottom-up)"
    elif p["linkedin_url"]:
        li_source = "provider record (" + ", ".join(srcs) + ")"
    else:
        li_source = None
    emails = [dict(r) for r in con.execute("SELECT id, email, status, method, confidence, source_url, review_status "
                                           "FROM email_records WHERE person_id=? ORDER BY id", (person_id,))]
    em = next((e for e in reversed(emails) if e["review_status"] == "approved"), None)
    rs = con.execute("SELECT * FROM profile_research WHERE person_id=? AND review_status='approved' ORDER BY id DESC "
                     "LIMIT 1", (person_id,)).fetchone()
    analysis = json.loads(rs["analysis_json"] or "null") if rs else None
    accel = [{"product_id": m["product_id"], "product_name": m.get("product_name"), "via": "profile_research"}
             for m in (analysis or {}).get("accelerator_matches", [])]
    if lead and lead["matches_json"]:
        accel += [{"product_id": m["product_id"], "product_name": m.get("product_name"), "via": "bottom_up_" +
                   m.get("method", "")} for m in json.loads(lead["matches_json"]).get("matches", [])]
    td = any(s != "linkedin-harness" for s in srcs)
    return {
        "person": {"id": p["id"], "name": p["full_name"], "linkedin_url": p["linkedin_url"],
                   "linkedin_source": li_source, "dedupe_basis": p["dedupe_basis"], "sources": srcs},
        "company": {"id": role["company_id"], "domain": role["domain"], "name": role["cname"],
                    "qualification_status": role["qualification_status"],
                    "qualification_source": role["qualification_source"]} if role else None,
        "role": {"id": role["id"], "title": role["title"], "review_status": role["review_status"]} if role else None,
        "email": em, "email_candidates": emails,
        "research": {"id": rs["id"], "confidence": rs["confidence"], "profile_url": rs["profile_url"],
                     "designation": (analysis or {}).get("current_designation"),
                     "priorities": (analysis or {}).get("priorities", []),
                     "pain_points": (analysis or {}).get("pain_points", []),
                     "summary": (analysis or {}).get("summary")} if rs else None,
        "accelerators": accel,
        "origin": "both" if (lead and td) else ("bottom_up" if lead else "top_down"),
        "provenance": {"person_sources": len(srcs),
                       "company_sources": con.execute("SELECT COUNT(*) FROM company_sources WHERE company_id=?",
                                                      (role["company_id"],)).fetchone()[0] if role else 0},
    }


def check(rec: dict, dup_people: list[dict], dup_company: list[dict]) -> tuple[list[dict], int]:
    res: list[dict] = []
    add = lambda name, status, **d: res.append({"check": name, "status": status, **d})  # noqa: E731
    c, role, p, em, rs = rec["company"], rec["role"], rec["person"], rec["email"], rec["research"]
    qualified = bool(c and c["qualification_status"] == "qualified" and c["qualification_source"] == "human")
    add("company_qualified", "pass" if qualified else "fail")
    add("role_approved", "pass" if role and role["review_status"] == "approved" else "fail")
    add("linkedin_present", "pass" if p["linkedin_url"] else "warn")
    if rs and p["linkedin_url"] and rs["profile_url"].rstrip("/").lower() != p["linkedin_url"].rstrip("/").lower():
        add("research_profile_match", "conflict", research_url=rs["profile_url"], person_url=p["linkedin_url"])
    add("email_present", "pass" if em else "warn")
    if em and c and not on_domain(em["email"], c["domain"]):
        add("email_domain_match", "conflict", email=em["email"], domain=c["domain"])
    open_emails = {e["email"] for e in rec["email_candidates"] if e["email"] and e["review_status"] != "rejected"}
    if len(open_emails) > 1:
        add("email_candidates_conflict", "conflict", emails=sorted(open_emails))
    add("research_approved", "pass" if rs else "warn")
    add("provenance_person", "pass" if rec["provenance"]["person_sources"] else "fail")
    add("provenance_company", "pass" if rec["provenance"]["company_sources"] else "fail")
    for d in dup_people + dup_company:
        add(d["check"], "conflict", **{k: v for k, v in d.items() if k != "check"})
    score = (20 if qualified else 0) + (15 if role and role["review_status"] == "approved" else 0) + \
        (20 if p["linkedin_url"] else 0) + (W_EMAIL.get(em["status"], 0) if em else 0) + \
        (W_RESEARCH.get(rs["confidence"] or "low", 0) if rs else 0)
    return res, score


def qa_status_of(results: list[dict], tier: str) -> str:
    st = {r["check"]: r["status"] for r in results}
    if st.get("company_qualified") == "fail" or st.get("role_approved") == "fail":
        return "blocked"
    if any(r["status"] == "conflict" for r in results) or tier == "Low" or \
            "fail" in (st.get("provenance_person"), st.get("provenance_company")):
        return "needs_review"
    return "passed"


def _hist(con, pid, action, frm, to, snap, by, reason):
    con.execute("INSERT INTO prospect_history(prospect_id,action,from_status,to_status,snapshot_json,reviewer,reason,"
                "at) VALUES(?,?,?,?,?,?,?,?)", (pid, action, frm, to, jdump(snap), by, reason, now()))


def run_qa(con, *, person_ids: list[int] | None = None) -> dict:
    run_id = start_run(con, AGENT, {"person_ids": person_ids})
    dp, dc = duplicates(con)
    ids = person_ids or [r[0] for r in con.execute("SELECT DISTINCT person_id FROM person_roles ORDER BY person_id")]
    counts = {"passed": 0, "needs_review": 0, "blocked": 0}
    tiers = {"High": 0, "Medium": 0, "Low": 0}
    try:
        for pid in ids:
            rec = build_record(con, pid)
            cid = rec["company"]["id"] if rec["company"] else None
            results, score = check(rec, dp.get(pid, []), dc.get(cid, []) if cid else [])
            tier = tier_of(score)
            qs = qa_status_of(results, tier)
            issues = [r for r in results if r["status"] in ("fail", "warn")]
            conflicts = [r for r in results if r["status"] == "conflict"]
            for r in results:
                con.execute("INSERT INTO qa_results(run_id,entity,entity_id,check_name,status,detail_json,at) "
                            "VALUES(?,?,?,?,?,?,?)", (run_id, "person", pid, r["check"], r["status"], jdump(r), now()))
            rj = jdump(rec)
            old = con.execute("SELECT * FROM prospects WHERE person_id=?", (pid,)).fetchone()
            if old is None:
                xid = con.execute("INSERT INTO prospects(person_id,company_id,record_json,score,tier,issues_json,"
                                  "conflicts_json,qa_status,qa_run_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                  (pid, cid, rj, score, tier, jdump(issues), jdump(conflicts), qs, run_id,
                                   now())).lastrowid
                _hist(con, xid, "create", None, "pending_review", rec, AGENT, f"qa {qs}, {tier} {score}")
            else:
                changed = hashlib.sha256(old["record_json"].encode()).digest() != hashlib.sha256(rj.encode()).digest()
                review = old["review_status"]
                if changed and review != "pending_review":
                    _hist(con, old["id"], "qa_changed", review, "pending_review", json.loads(old["record_json"]), AGENT,
                          "record changed after review; needs a fresh human review")
                    review = "pending_review"
                con.execute("UPDATE prospects SET company_id=?, record_json=?, score=?, tier=?, issues_json=?, "
                            "conflicts_json=?, qa_status=?, qa_run_id=?, review_status=?, updated_at=? WHERE id=?",
                            (cid, rj, score, tier, jdump(issues), jdump(conflicts), qs, run_id, review, now(),
                             old["id"]))
            counts[qs] += 1
            tiers[tier] += 1
        con.commit()
    except BaseException as e:
        con.rollback()
        finish_run(con, run_id, "failed", {"error": repr(e)})
        raise
    out = {"run_id": run_id, "prospects": len(ids), "qa_status": counts, "tiers": tiers}
    finish_run(con, run_id, "ok", out)
    return out


# ---------------- human review ----------------
class ProspectReviewError(ValueError):
    pass


def review_prospect(con, prospect_id: int, status: str, *, by: str, reason: str) -> None:
    if status not in ("approved", "rejected", "pending_review"):
        raise ProspectReviewError("status must be approved, rejected or pending_review")
    if not (by or "").strip() or not (reason or "").strip():
        raise ProspectReviewError("reviewer name and reason are required")
    r = con.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
    if r is None:
        raise ProspectReviewError(f"unknown prospect {prospect_id}")
    if r["review_status"] == status:
        raise ProspectReviewError(f"prospect {prospect_id} is already {status}")
    if status == "approved" and r["qa_status"] == "blocked":
        raise ProspectReviewError("prospect is QA-blocked (company not qualified or role not approved)")
    _hist(con, prospect_id, {"approved": "approve", "rejected": "reject"}.get(status, "reopen"), r["review_status"],
          status, json.loads(r["record_json"]), by, reason)
    con.execute("UPDATE prospects SET review_status=?, reviewed_by=?, reviewed_at=?, review_reason=? WHERE id=?",
                (status, by, now(), reason, prospect_id))
    con.commit()


# ---------------- export / sync ----------------
def rows(con, *, only_approved: bool = False, min_tier: str | None = None) -> list[dict]:
    order = {"High": 3, "Medium": 2, "Low": 1}
    out = []
    for r in con.execute("SELECT * FROM prospects ORDER BY score DESC, id"):
        if only_approved and r["review_status"] != "approved":
            continue
        if min_tier and order.get(r["tier"], 0) < order[min_tier]:
            continue
        rec = json.loads(r["record_json"])
        # Account-based prospects (outreach/account_drafts.py) carry no research / accelerators / origin keys.
        p = rec["person"]
        c, em, rs = rec.get("company") or {}, rec.get("email") or {}, rec.get("research") or {}
        out.append({"prospect_id": r["id"], "person_id": p["id"], "name": p["name"],
                    "title": (rec.get("role") or {}).get("title"), "company": c.get("name"), "domain": c.get("domain"),
                    "linkedin_url": p.get("linkedin_url"), "linkedin_source": p.get("linkedin_source"),
                    "email": em.get("email"), "email_status": em.get("status"), "email_method": em.get("method"),
                    "email_source_url": em.get("source_url"), "research_confidence": rs.get("confidence"),
                    "designation": rs.get("designation"),
                    "accelerators": "; ".join(sorted({a.get("product_name") or a["product_id"]
                                                      for a in rec.get("accelerators") or []})),
                    "origin": rec.get("origin") or c.get("origin"),
                    "score": r["score"], "tier": r["tier"], "qa_status": r["qa_status"],
                    "review_status": r["review_status"],
                    "issues": "; ".join(i["check"] for i in json.loads(r["issues_json"])),
                    "conflicts": "; ".join(i["check"] for i in json.loads(r["conflicts_json"]))})
    return out


def to_csv(data: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS)
    w.writeheader()
    w.writerows(data)
    return buf.getvalue()


def write_xlsx(data: list[dict], path: Path) -> None:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Prospects"
    ws.append(EXPORT_COLUMNS)
    for d in data:
        ws.append([d.get(k) for k in EXPORT_COLUMNS])
    ws.freeze_panes = "A2"
    wb.save(path)


def parse_targets(spec: str | None) -> list[tuple[str, str]]:
    """CHANAKYA_SYNC_TARGETS, e.g. "csv:exports/prospects.csv;xlsx:exports/prospects.xlsx;jsonl:x.jsonl;
    webhook:https://crm.example/hook". Unknown kinds are rejected."""
    out = []
    for part in [p.strip() for p in (spec or "").split(";") if p.strip()]:
        kind, _, target = part.partition(":")
        if kind not in ("csv", "xlsx", "jsonl", "webhook") or not target:
            raise ValueError(f"bad sync target {part!r} (kinds: csv, xlsx, jsonl, webhook)")
        out.append((kind, target))
    return out


def sync(con, targets: list[tuple[str, str]] | None = None, *, allow_live: bool = False, only_approved: bool = True,
         poster=None) -> list[dict]:
    """File targets are written always (they are local). Webhooks POST JSON only with allow_live=True and only
    human-approved prospects; otherwise they are recorded as dry runs."""
    targets = targets if targets is not None else parse_targets(os.getenv("CHANAKYA_SYNC_TARGETS"))
    data = rows(con, only_approved=only_approved)
    report = []
    for kind, target in targets:
        detail: dict = {"only_approved": only_approved}
        dry = False
        try:
            if kind in ("csv", "xlsx", "jsonl"):
                path = Path(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                if kind == "csv":
                    path.write_text(to_csv(data), encoding="utf-8", newline="")
                elif kind == "xlsx":
                    write_xlsx(data, path)
                else:
                    path.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in data), encoding="utf-8")
                status = "ok"
            else:
                approved = rows(con, only_approved=True)
                detail["rows_sent"] = len(approved)
                if not allow_live:
                    dry, status = True, "dry_run"
                else:
                    if poster is None:
                        import httpx
                        poster = lambda url, body: httpx.post(url, json=body, timeout=30).status_code  # noqa: E731
                    code = poster(target, {"prospects": approved, "source": "chanakya"})
                    detail["http_status"] = code
                    status = "ok" if 200 <= int(code) < 300 else "error"
        except Exception as e:
            status, detail["error"] = "error", f"{type(e).__name__}: {e}"
        con.execute("INSERT INTO sync_runs(target,kind,dry_run,rows,status,detail_json,at) VALUES(?,?,?,?,?,?,?)",
                    (target if kind != "webhook" else re.sub(r"\?.*$", "?…", target), kind, int(dry), len(data),
                     status, jdump(detail), now()))
        report.append({"kind": kind, "target": target, "rows": len(data), "status": status, **detail})
    con.commit()
    return report
