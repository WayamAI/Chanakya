"""Review gates for the UI: one list + decide + history endpoint set per gate, each showing its evidence.

Every decision calls the EXISTING review function of the owning agent with the signed-in user as reviewer and a
mandatory reason, so the agents' own rules apply unchanged (e.g. a role cannot be approved at an unqualified company,
a LinkedIn URL must be one of the stored candidates, blocked research cannot be approved, QA-blocked prospects
cannot be approved, drafts with [PERSONALISE] markers cannot be marked ready).
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response

from chanakya.api import auth
from chanakya.api import mapping as M
from chanakya.api.db import db
from chanakya.storage.db import now

router = APIRouter(prefix="/api/review")
j = M.j


def who(request: Request) -> str:
    return auth.current_user(request)["name"]


def need_reason(body: dict) -> str:
    r = (body.get("reason") or "").strip()
    if not r:
        raise HTTPException(400, "A reason is required for every review decision.")
    return r


def guard(fn):
    """Agent review errors (ValueError subclasses) become 400 with the agent's own message."""
    try:
        return fn()
    except HTTPException:
        raise
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


# ---------------- counts for the inbox ----------------
@router.get("/summary")
def summary():
    with db() as con:
        q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
        return {"gates": [
            {"key": "mappings", "label": "Industry mappings", "href": "/review/mappings",
             "count": q("SELECT COUNT(*) FROM industry_mappings WHERE status NOT IN ('approved','rejected','exact')")},
            {"key": "personas", "label": "Buyer personas", "href": "/review/personas",
             "count": q("SELECT COUNT(*) FROM persona_role_mappings WHERE status='pending'")},
            {"key": "companies", "label": "Company qualification", "href": "/review/companies",
             "count": q("SELECT COUNT(*) FROM companies WHERE qualification_source!='human'")},
            {"key": "leads", "label": "People found on LinkedIn", "href": "/review/leads",
             "count": q("SELECT COUNT(*) FROM bottomup_leads WHERE status IN ('unclear','company_unverified')")},
            {"key": "roles", "label": "People and roles", "href": "/review/roles",
             "count": q("SELECT COUNT(*) FROM person_roles WHERE review_status='needs_manual_review'")},
            {"key": "linkedin", "label": "LinkedIn matches", "href": "/review/linkedin",
             "count": q("SELECT COUNT(*) FROM linkedin_resolutions WHERE review_status='pending_review' AND "
                        "status!='error'")},
            {"key": "emails", "label": "Email findings", "href": "/review/emails",
             "count": q("SELECT COUNT(*) FROM email_records WHERE review_status='pending_review'")},
            {"key": "research", "label": "Profile research", "href": "/review/research",
             "count": q("SELECT COUNT(*) FROM profile_research WHERE review_status='pending_review' AND "
                        "status='researched'")},
            {"key": "prospects", "label": "Prospects (QA)", "href": "/review/prospects",
             "count": q("SELECT COUNT(*) FROM prospects WHERE review_status='pending_review' AND qa_status!='blocked'")},
            {"key": "clay", "label": "Clay confirmations", "href": "/review/clay",
             "count": q("SELECT COUNT(*) FROM workflow_stages s JOIN workflows w ON w.id=s.workflow_id WHERE "
                        "s.status='blocked' AND s.stage IN ('scout','people') AND w.status NOT IN "
                        "('cancelled','completed')")},
            {"key": "drafts", "label": "Outreach drafts", "href": "/drafts",
             "count": q("SELECT COUNT(*) FROM outreach_drafts WHERE status IN ('draft','edited')")}]}


# ---------------- industry mappings (Agent 2) ----------------
@router.get("/mappings")
def mappings():
    with db() as con:
        out = []
        for r in con.execute("SELECT m.*, i.product_id FROM industry_mappings m LEFT JOIN icps i ON i.id=m.icp_id "
                             "ORDER BY m.id DESC"):
            out.append({**{k: r[k] for k in ("id", "icp_id", "product_id", "field", "original_term", "status",
                                             "method", "confidence", "rationale", "reviewed_by", "reviewed_at")},
                        "canonical": j(r["canonical_json"], []), "alternatives": j(r["alternatives_json"], []),
                        "history": [dict(h) for h in con.execute(
                            "SELECT * FROM industry_mapping_history WHERE mapping_id=? ORDER BY id", (r["id"],))]})
        return {"rows": out}


@router.post("/mappings/{mid}")
def decide_mapping(mid: int, request: Request, body: dict = Body(...)):
    from chanakya.scout import industry_map as IM
    from chanakya.scout.dsl import ClaySchema
    reviewer, reason, decision = who(request), need_reason(body), body.get("decision")
    schema = ClaySchema.from_capabilities(M_ROOT() / "docs/clay_capabilities.json")
    with db() as con:
        r = con.execute("SELECT status FROM industry_mappings WHERE id=?", (mid,)).fetchone()
        if r is None:
            raise HTTPException(404, "unknown mapping")
        guard(lambda: IM.review(con, mid, decision, reviewer, body.get("canonical"), schema.enums["industry"]))
        con.execute("INSERT INTO industry_mapping_history(mapping_id,from_status,to_status,canonical_json,reviewer,"
                    "reason,at) VALUES(?,?,?,?,?,?,?)", (mid, r["status"], decision, json.dumps(body.get("canonical")),
                                                         reviewer, reason, now()))
        con.commit()
    return {"ok": True}


# ---------------- personas (Agent 3) ----------------
@router.get("/personas")
def personas():
    from chanakya.people import personas as P
    with db() as con:
        rows = con.execute("SELECT m.*, p.name product_name FROM persona_role_mappings m LEFT JOIN products p ON "
                           "p.id=m.product_id ORDER BY (m.status='pending') DESC, m.id").fetchall()
        return {"rows": [{**{k: r[k] for k in ("id", "product_id", "product_name", "persona", "persona_source",
                                               "status", "reviewed_by", "reviewed_at", "reason")},
                          "titles": j(r["titles_json"], []), "seniorities": j(r["seniorities_json"], []),
                          "evidence": j(r["evidence_json"], []), "history": P.history(con, r["id"])} for r in rows]}


@router.post("/personas/{mid}")
def decide_persona(mid: int, request: Request, body: dict = Body(...)):
    from chanakya.people import personas as P
    reviewer, reason = who(request), need_reason(body)
    with db() as con:
        guard(lambda: P.review_mapping(con, mid, body.get("decision"), reviewer=reviewer, reason=reason))
    return {"ok": True}


# ---------------- companies (Agent 2 -> 3) ----------------
@router.get("/companies")
def companies():
    from chanakya.scout.qualification import human_reviews
    with db() as con:
        out = []
        for r in con.execute("SELECT * FROM companies ORDER BY (qualification_source='human'), id"):
            a = j(r["assessment_json"], {})
            out.append({**M.company_obj(con, r), "assessment": {k: a.get(k) for k in (
                "search_filter_match", "data_quality", "product_relevance", "business_fit", "review_reasons",
                "executed_query", "provenance_note")},
                "sources": [dict(s) for s in con.execute(
                    "SELECT source, tool, source_url, query, fetched_at FROM company_sources WHERE company_id=? "
                    "ORDER BY id", (r["id"],))],
                "history": human_reviews(con, r["id"])})
        return {"rows": out}


@router.post("/companies/{cid}")
def decide_company(cid: int, request: Request, body: dict = Body(...)):
    from chanakya.scout.qualification import record_human_decision
    reviewer, reason, decision = who(request), need_reason(body), body.get("decision")
    if decision not in ("qualified", "disqualified", "needs_manual_review"):
        raise HTTPException(400, "decision must be qualified, disqualified or needs_manual_review")
    with db() as con:
        r = con.execute("SELECT domain FROM companies WHERE id=?", (cid,)).fetchone()
        if r is None:
            raise HTTPException(404, "unknown company")
        guard(lambda: record_human_decision(con, r["domain"], decision, reviewer, reason))
        con.commit()
    return {"ok": True}


# ---------------- people / roles (Agent 3) ----------------
@router.get("/roles")
def roles():
    from chanakya.people import role_review as RV
    with db() as con:
        out = []
        for r in con.execute("SELECT id FROM person_roles ORDER BY (review_status='needs_manual_review') DESC, id"):
            d = RV.get_role(con, r["id"])
            d["sources"] = [{**s, "raw": j(s.pop("raw_json"), {})} for s in d["sources"]]
            d["history"] = RV.history(con, r["id"])
            out.append(d)
        return {"rows": out}


@router.post("/roles/{rid}")
def decide_role(rid: int, request: Request, body: dict = Body(...)):
    from chanakya.people import role_review as RV
    reviewer, reason = who(request), need_reason(body)
    status = {"approve": RV.APPROVED, "reject": RV.REJECTED, "reopen": RV.PENDING}.get(body.get("decision"))
    if status is None:
        raise HTTPException(400, "decision must be approve, reject or reopen")
    with db() as con:
        guard(lambda: RV.review_role(con, rid, status, reviewer=reviewer, reason=reason,
                                     evidence=[{"text": body["evidence"]}] if body.get("evidence") else None))
    return {"ok": True}


# ---------------- LinkedIn resolutions (Agent 4) ----------------
@router.get("/linkedin")
def linkedin():
    from chanakya.resolver import review as RR
    with db() as con:
        out = []
        for r in RR.list_resolutions(con):
            d = {k: r[k] for k in r.keys() if k not in ("harness_json",)}
            for k in ("input_json", "best_json", "alternatives_json", "searches_json", "harness_argv_json"):
                d[k[:-5]] = j(d.pop(k), None)
            d["candidate_urls"] = RR.candidate_urls(r)
            d["history"] = [dict(h) for h in RR.history(con, r["id"])]
            out.append(d)
        return {"rows": out[::-1]}


@router.post("/linkedin/{rid}")
def decide_linkedin(rid: int, request: Request, body: dict = Body(...)):
    from chanakya.resolver import review as RR
    reviewer, reason = who(request), need_reason(body)
    with db() as con:
        if body.get("decision") == "approve":
            return {"approved_url": guard(lambda: RR.approve(con, rid, by=reviewer, reason=reason, url=body.get("url")))}
        if body.get("decision") == "reject":
            guard(lambda: RR.reject(con, rid, by=reviewer, reason=reason))
            return {"ok": True}
    raise HTTPException(400, "decision must be approve or reject")


# ---------------- emails (Agent 5) ----------------
@router.get("/emails")
def emails():
    from chanakya.emailhunter import agent as E
    with db() as con:
        out = []
        for r in con.execute("SELECT e.*, p.full_name, c.domain FROM email_records e JOIN people p ON p.id=e.person_id "
                             "LEFT JOIN companies c ON c.id=e.company_id ORDER BY (e.review_status='pending_review') "
                             "DESC, e.id DESC"):
            d = dict(r)
            d["evidence"], d["mx"] = j(d.pop("evidence_json"), []), j(d.pop("mx_json"), None)
            d["pages"] = [dict(p) for p in con.execute(
                "SELECT url, http_status, robots_allowed, emails_json, error, fetched_at FROM website_pages WHERE "
                "domain=? AND run_id IS ? ORDER BY id", (r["domain"], r["run_id"]))] if r["run_id"] else []
            d["history"] = E.history(con, r["id"])
            out.append(d)
        return {"rows": out}


@router.post("/emails/{rid}")
def decide_email(rid: int, request: Request, body: dict = Body(...)):
    from chanakya.emailhunter import agent as E
    reviewer, reason = who(request), need_reason(body)
    with db() as con:
        d = body.get("decision")
        if d == "approve":
            guard(lambda: E.approve(con, rid, by=reviewer, reason=reason))
        elif d == "reject":
            guard(lambda: E.reject(con, rid, by=reviewer, reason=reason))
        elif d == "override":
            r = guard(lambda: E.get_record(con, rid))
            return {"email_record_id": guard(lambda: E.override(con, r["person_id"], body.get("email") or "",
                                                                by=reviewer, reason=reason,
                                                                evidence_url=body.get("evidence_url") or None))}
        else:
            raise HTTPException(400, "decision must be approve, reject or override")
    return {"ok": True}


# ---------------- research (Agent 6) ----------------
@router.get("/research")
def research():
    from chanakya.research import agent as RA
    with db() as con:
        out = []
        for r in con.execute("SELECT x.*, p.full_name FROM profile_research x JOIN people p ON p.id=x.person_id ORDER "
                             "BY (x.review_status='pending_review' AND x.status='researched') DESC, x.id DESC"):
            d = {k: r[k] for k in r.keys() if k != "harness_json"}
            for k in ("facts_json", "analysis_json", "dropped_json", "matches_json", "harness_argv_json"):
                d[k[:-5]] = j(d.pop(k), None)
            d["history"] = RA.history(con, r["id"])
            out.append(d)
        return {"rows": out}


@router.post("/research/{rid}")
def decide_research(rid: int, request: Request, body: dict = Body(...)):
    from chanakya.research import agent as RA
    reviewer, reason = who(request), need_reason(body)
    status = {"approve": "approved", "reject": "rejected"}.get(body.get("decision"))
    if status is None:
        raise HTTPException(400, "decision must be approve or reject")
    with db() as con:
        guard(lambda: RA.review(con, rid, status, by=reviewer, reason=reason))
    return {"ok": True}


# ---------------- prospects (Agent 7) ----------------
@router.get("/prospects")
def prospects():
    with db() as con:
        out = []
        for r in con.execute("SELECT * FROM prospects ORDER BY (review_status='pending_review') DESC, score DESC"):
            d = dict(r)
            d["record"], d["issues"], d["conflicts"] = (j(d.pop("record_json"), {}), j(d.pop("issues_json"), []),
                                                        j(d.pop("conflicts_json"), []))
            d["checks"] = [dict(c, detail=j(c["detail_json"], {})) for c in con.execute(
                "SELECT check_name, status, detail_json FROM qa_results WHERE run_id=? AND entity_id=? ORDER BY id",
                (r["qa_run_id"], r["person_id"]))]
            for c in d["checks"]:
                c.pop("detail_json", None)
            d["history"] = [dict(h, snapshot=None) for h in con.execute(
                "SELECT id, action, from_status, to_status, reviewer, reason, at FROM prospect_history WHERE "
                "prospect_id=? ORDER BY id", (r["id"],))]
            out.append(d)
        return {"rows": out}


@router.post("/prospects/run-qa")
def run_qa():
    from chanakya.qa.agent import run_qa as rq
    with db() as con:
        return rq(con)


@router.post("/prospects/{xid}")
def decide_prospect(xid: int, request: Request, body: dict = Body(...)):
    from chanakya.qa import agent as Q
    reviewer, reason = who(request), need_reason(body)
    status = {"approve": "approved", "reject": "rejected", "reopen": "pending_review"}.get(body.get("decision"))
    if status is None:
        raise HTTPException(400, "decision must be approve, reject or reopen")
    with db() as con:
        guard(lambda: Q.review_prospect(con, xid, status, by=reviewer, reason=reason))
    return {"ok": True}


# ---------------- bottom-up leads ----------------
@router.get("/leads")
def leads():
    with db() as con:
        out = []
        for r in con.execute("SELECT * FROM bottomup_leads ORDER BY id DESC"):
            d = {k: r[k] for k in r.keys()}
            for k in ("search_json", "relevance_json", "company_json", "matches_json"):
                d[k[:-5]] = j(d.pop(k), None)
            prof = j(d.pop("profile_json"), None)
            d["profile"] = {"company_basis": prof.get("company_basis"), "top": (prof.get("data") or {}).get("top")} \
                if prof else None
            d["history"] = [dict(h) for h in con.execute(
                "SELECT * FROM bottomup_lead_history WHERE lead_id=? ORDER BY id", (r["id"],))]
            out.append(d)
        return {"rows": out}


@router.post("/leads/{lid}")
def decide_lead(lid: int, request: Request, body: dict = Body(...)):
    from chanakya.bottomup import agent as B
    reviewer, reason = who(request), need_reason(body)
    with db() as con:
        if body.get("decision") in ("relevant", "not_relevant"):
            guard(lambda: B.review_lead(con, lid, body["decision"], by=reviewer, reason=reason))
            return {"ok": True}
        if body.get("decision") == "link":
            cid, pid = guard(lambda: B.link_lead(con, lid, domain=body.get("domain"), by=reviewer))
            con.execute("INSERT INTO bottomup_lead_history(lead_id,from_status,to_status,actor,note,at) "
                        "VALUES(?,?,?,?,?,?)", (lid, "linked", "linked", reviewer, f"manual link: {reason}", now()))
            con.commit()
            return {"company_id": cid, "person_id": pid}
    raise HTTPException(400, "decision must be relevant, not_relevant or link")


# ---------------- Clay confirmations + paid-email spend approval ----------------
@router.get("/clay")
def clay():
    with db() as con:
        pend = []
        for s in con.execute("SELECT s.*, w.kind, w.params_json FROM workflow_stages s JOIN workflows w ON "
                             "w.id=s.workflow_id WHERE s.status='blocked' AND s.stage IN ('scout','people') AND "
                             "w.status NOT IN ('cancelled','completed') ORDER BY s.id DESC"):
            out = j(s["output_json"], {}) or {}
            params = j(s["params_json"], {})
            if s["stage"] == "scout" and out.get("query"):
                pend.append({"workflow_id": s["workflow_id"], "stage": "scout", "domain": None, "query": out["query"],
                             "confirmed": params.get("confirm_scout_query") == out["query"], "message": s["error"]})
            elif s["stage"] == "people":
                conf = params.get("confirm_people_query") or {}
                for dom, q in out.items():
                    if isinstance(q, str) and dom != "problems":
                        pend.append({"workflow_id": s["workflow_id"], "stage": "people", "domain": dom, "query": q,
                                     "confirmed": conf.get(dom) == q, "message": s["error"]})
        appr = [dict(r) for r in con.execute("SELECT * FROM spend_approvals ORDER BY id DESC")]
        for a in appr:
            a["used"] = con.execute("SELECT COUNT(*) FROM email_records WHERE method='clay' AND created_at>=?",
                                    (a["created_at"],)).fetchone()[0]
        return {"pending": pend, "spend_approvals": appr,
                "env_spend_approval": bool(os.getenv("CHANAKYA_PAID_EMAIL_APPROVAL")),
                "note": "Confirming a query lets the next run make ONE live Clay call for it. search-companies / "
                        "search-contacts cost is unconfirmed on this workspace (docs/clay_cost_review.md)."}


@router.post("/spend-approvals")
def add_spend(request: Request, body: dict = Body(...)):
    reviewer, reason = who(request), need_reason(body)
    n = int(body.get("max_records") or 0)
    if not 1 <= n <= 5:
        raise HTTPException(400, "max_records must be 1..5")
    with db() as con:
        con.execute("UPDATE spend_approvals SET status='revoked', revoked_by=?, revoked_at=? WHERE status='active'",
                    (reviewer, now()))
        sid = con.execute("INSERT INTO spend_approvals(kind,reviewer,reason,max_records,status,created_at) VALUES(?,?,?,?,"
                          "?,?)", ("paid_email", reviewer, reason, n, "active", now())).lastrowid
        con.commit()
    return {"id": sid}


@router.post("/spend-approvals/{sid}/revoke")
def revoke_spend(sid: int, request: Request):
    with db() as con:
        con.execute("UPDATE spend_approvals SET status='revoked', revoked_by=?, revoked_at=? WHERE id=? AND "
                    "status='active'", (who(request), now(), sid))
        con.commit()
    return {"ok": True}


# ---------------- outreach drafts ----------------
drafts = APIRouter(prefix="/api/drafts")


@drafts.get("")
def list_drafts():
    with db() as con:
        from chanakya.outreach.drafts import LABELS
        return {"rows": [{**dict(r), "status_label": LABELS.get(r["status"], r["status"])} for r in con.execute(
            "SELECT d.id, d.prospect_id, d.person_id, p.full_name, d.to_address, d.subject, d.status, d.generator, "
            "d.updated_at, d.outlook_message_id FROM outreach_drafts d JOIN people p ON p.id=d.person_id ORDER BY d.id DESC")]}


@drafts.get("/{did}")
def get_draft(did: int):
    from chanakya.outreach import drafts as D
    with db() as con:
        r = con.execute("SELECT d.*, p.full_name FROM outreach_drafts d JOIN people p ON p.id=d.person_id WHERE d.id=?",
                        (did,)).fetchone()
        if r is None:
            raise HTTPException(404, "unknown draft")
        d = dict(r)
        d["hooks"] = j(d.pop("hooks_json"), [])
        d["history"] = D.history(con, did)
        d["status_label"] = D.LABELS.get(d["status"], d["status"])
        return d


@drafts.post("/generate")
def generate(request: Request):
    from chanakya.outreach import drafts as D
    from chanakya.workflow.services import make_llm
    who(request)
    with db() as con:
        return {"draft_ids": D.generate(con, llm=make_llm(task="outreach_draft"), sender=os.getenv("CHANAKYA_SENDER", "[Your name]"))}


@drafts.post("/{did}")
def act_draft(did: int, request: Request, body: dict = Body(...)):
    from chanakya.outreach import drafts as D
    reviewer, action = who(request), body.get("action")
    reason = (body.get("reason") or "").strip() or {"edit": "edited in the UI", "ready": "ready for me to send",
                                                     "discard": "rejected in the UI", "reject": "rejected in the UI",
                                                     "approve": "approved in the UI",
                                                     "request_changes": "needs editing"}.get(action, "")
    with db() as con:
        if action == "edit":
            guard(lambda: D.edit(con, did, by=reviewer, reason=reason, subject=body.get("subject"),
                                 body=body.get("body")))
        elif action == "ready":
            guard(lambda: D.mark_ready(con, did, by=reviewer, reason=reason))
        elif action == "discard":
            guard(lambda: D.discard(con, did, by=reviewer, reason=reason))
        elif action == "reject":
            guard(lambda: D.reject(con, did, by=reviewer, reason=reason))
        elif action == "approve":
            guard(lambda: D.approve(con, did, by=reviewer, reason=reason))
        elif action == "request_changes":
            guard(lambda: D.request_changes(con, did, by=reviewer, reason=reason))
        else:
            raise HTTPException(400, "action must be edit, request_changes, approve, ready, reject or discard "
                                     "(sending is not possible)")
    return get_draft(did)


@drafts.get("/{did}/eml")
def draft_eml(did: int):
    from email.message import EmailMessage
    with db() as con:
        r = con.execute("SELECT * FROM outreach_drafts WHERE id=?", (did,)).fetchone()
    if r is None:
        raise HTTPException(404, "unknown draft")
    m = EmailMessage()
    m["To"] = r["to_address"] or ""
    m["Subject"] = r["subject"]
    m["X-Chanakya-Draft"] = f"{r['id']} ({r['status']}) - NOT SENT; review and send yourself"
    m.set_content(r["body"])
    return Response(bytes(m), media_type="message/rfc822",
                    headers={"Content-Disposition": f'attachment; filename="draft_{did:04d}.eml"'})


def M_ROOT():
    from chanakya.api.db import ROOT
    return ROOT
