"""Unified GTM workspace API: dashboard, accounts, people, signal radar, accelerators, gaps, discovery import,
methodology settings and integration checks. Everything reads the shared tables; nothing here invents data."""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Body, HTTPException, Request

from chanakya.api import auth
from chanakya.api import mapping as M
from chanakya.api.db import db
from chanakya.intel import compliance as C
from chanakya.intel import entities as E
from chanakya.intel import gaps as G
from chanakya.intel import scoring as SC
from chanakya.intel import settings as ST
from chanakya.intel import signals as SG
from chanakya.intel.voice import accelerator_url
from chanakya.storage.db import jdump, now

router = APIRouter(prefix="/api/intel")
j = M.j


def who(request: Request) -> str:
    return auth.current_user(request)["name"]


def _reason(body: dict) -> str:
    r = (body.get("reason") or "").strip()
    if not r:
        raise HTTPException(400, "A reason is required.")
    return r


def _opp_row(con, r) -> dict:
    d = dict(r)
    d["explanation"] = j(d.pop("explanation_json"), {})
    d["product_name"] = (con.execute("SELECT name FROM products WHERE id=?", (r["product_id"],)).fetchone() or [None])[0]
    d["company"] = (con.execute("SELECT name FROM companies WHERE id=?", (r["company_id"],)).fetchone() or [None])[0]
    d["accelerator_url"] = accelerator_url(r["product_id"])
    return d


def _signal_row(con, s) -> dict:
    d = dict(s)
    from chanakya.intel.ai_agents import evidence_class
    d["evidence_class"] = evidence_class(d)
    d["roles"] = j(d.pop("roles_json"), [])
    d["verification"] = j(d.pop("verification_json"), {})
    d.update(SG.signal_strength(con, s))
    d["fits"] = [dict(f) for f in con.execute("SELECT f.product_id, p.name product_name, f.score, f.why, f.method FROM "
                                              "signal_fits f LEFT JOIN products p ON p.id=f.product_id WHERE "
                                              "f.signal_id=? ORDER BY f.score DESC", (s["id"],))]
    d["type_label"] = ST.SIGNAL_TYPES.get(s["type"], {}).get("label", s["type"])
    return d


# ---------------- dashboard ----------------
@router.get("/dashboard")
def dashboard(origin: str = "all"):
    with db() as con:
        cos = [r[0] for r in con.execute("SELECT id FROM companies WHERE identity_status IS NOT 'merged'")]
        org = {cid: E.origin(con, "company", cid) for cid in cos}
        keep = {cid for cid, o in org.items() if origin in ("all", "", None) or o == origin}
        q = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
        qualified = sum(1 for cid in keep if q("SELECT COUNT(*) FROM companies WHERE id=? AND qualification_status="
                                               "'qualified' AND qualification_source='human'", cid))
        people = sum(q("SELECT COUNT(DISTINCT person_id) FROM person_roles WHERE company_id=? AND review_status="
                       "'approved'", cid) for cid in keep)
        opps = [_opp_row(con, r) for r in con.execute("SELECT * FROM opportunities ORDER BY score DESC")
                if r["company_id"] in keep]
        sig_rows = [s for s in con.execute("SELECT * FROM signals") if s["company_id"] in keep or
                    (origin in ("all", "", None) and s["company_id"] is None)]
        by_ver: dict[str, int] = {}
        for s in sig_rows:
            by_ver[s["verification_status"]] = by_ver.get(s["verification_status"], 0) + 1
        matches = sum(1 for s in sig_rows if (q("SELECT MAX(score) FROM signal_fits WHERE signal_id=?", s["id"]) or 0)
                      >= ST.get(con, "gap_fit_threshold"))
        gaps = G.themes(con)
        review = q("SELECT COUNT(*) FROM prospects WHERE review_status='pending_review' AND qa_status!='blocked'")
        recent = [dict(r) for r in con.execute(
            "SELECT d.entity, d.entity_id, d.direction, d.source, d.workflow_id, d.at, CASE d.entity WHEN 'company' THEN "
            "(SELECT name FROM companies WHERE id=d.entity_id) WHEN 'person' THEN (SELECT full_name FROM people WHERE "
            "id=d.entity_id) ELSE (SELECT substr(summary,1,90) FROM signals WHERE id=d.entity_id) END label "
            "FROM discovery_events d ORDER BY d.id DESC LIMIT 15")]
        activity = [dict(r) for r in con.execute("SELECT workflow_id, stage, level, message, at FROM workflow_events "
                                                 "ORDER BY id DESC LIMIT 10")]
        return {"origin": origin, "counts": {
            "qualified_accounts": qualified, "accounts": len(keep), "relevant_people": people,
            "active_opportunities": sum(1 for o in opps if o["status"] == "open"),
            "signals": len(sig_rows), "verified_signals": by_ver.get("verified", 0),
            "signals_by_verification": by_ver, "accelerator_matches": matches,
            "emerging_gaps": len(gaps), "ready_to_presell": sum(1 for g in gaps if g["verdict"] == "ready_to_presell"),
            "prospects_to_review": review,
            "origins": {o: sum(1 for x in org.values() if x == o) for o in ("top_down", "bottom_up", "both")}},
            "top_opportunities": opps[:10], "recent_discoveries": recent, "activity": activity,
            "note": "Signals marked 'verified' were opened by a person; automatic checks only show reachability."}


# ---------------- accounts ----------------
@router.get("/accounts")
def accounts(origin: str = "all", q: str = ""):
    with db() as con:
        out = []
        for c in con.execute("SELECT * FROM companies WHERE identity_status IS NOT 'merged' ORDER BY id"):
            o = E.origin(con, "company", c["id"])
            srcs = E.sources(con, "company", c["id"])
            if origin == "lakshya":
                if "lakshya" not in srcs:
                    continue
            elif origin not in ("all", "") and o != origin:
                continue
            if q and q.lower() not in f"{c['name']} {c['domain']}".lower():
                continue
            best = con.execute("SELECT score, product_id FROM opportunities WHERE company_id=? ORDER BY score DESC "
                               "LIMIT 1", (c["id"],)).fetchone()
            out.append({"id": c["id"], "name": c["name"] or c["domain"], "domain": c["domain"],
                        "identity_status": c["identity_status"], "origin": o, "sources": srcs,
                        "qualification_status": c["qualification_status"],
                        "qualification_source": c["qualification_source"], **SC.account_heat(con, c["id"]),
                        "people": con.execute("SELECT COUNT(DISTINCT person_id) FROM person_roles WHERE company_id=?",
                                              (c["id"],)).fetchone()[0],
                        "best_score": best["score"] if best else None, "best_product": best["product_id"] if best else None})
        return {"rows": sorted(out, key=lambda r: (-(r["best_score"] or -1), -r["heat"]))}


@router.get("/accounts/{cid}")
def account(cid: int):
    from chanakya.scout.qualification import human_reviews
    with db() as con:
        c = con.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        if c is None:
            raise HTTPException(404, "unknown account")
        pids = [r[0] for r in con.execute("SELECT DISTINCT person_id FROM person_roles WHERE company_id=?", (cid,))]
        sigs = [_signal_row(con, s) for s in con.execute("SELECT * FROM signals WHERE company_id=? ORDER BY id DESC",
                                                         (cid,))]
        opps = [_opp_row(con, r) for r in con.execute("SELECT * FROM opportunities WHERE company_id=? ORDER BY score "
                                                      "DESC", (cid,))]
        roles_named = sorted({r for s in sigs for r in s["roles"]})
        committee = [{"person_id": r["pid"], "name": r["full_name"], "title": r["title"],
                      "review_status": r["review_status"]} for r in con.execute(
            "SELECT p.id pid, p.full_name, r.title, r.review_status FROM person_roles r JOIN people p ON "
            "p.id=r.person_id WHERE r.company_id=?", (cid,))]
        known = " ".join((x["title"] or "") for x in committee).lower()
        open_roles = [r for r in roles_named if r.lower() not in known]
        unknowns = sorted({u for o in opps for u in o["explanation"].get("unknowns", [])})
        from chanakya.intel import lakshya as LK
        from chanakya.intel.ai_agents import latest_analysis
        return {"analysis": latest_analysis(con, cid), "lakshya": LK.for_company(con, cid),
                "sources": E.sources(con, "company", cid),
                "company": {**M.company_obj(con, c), "identity_status": c["identity_status"]},
                "identity": E.company_identity(con, cid), "heat": SC.account_heat(con, cid),
                "people": [M.leader_obj(con, p) | {"origin": E.origin(con, "person", p)} for p in pids],
                "signals": sigs, "opportunities": opps,
                "buying_committee": {"known": committee, "roles_named_in_signals_without_a_person": [
                    {"role": r, "linkedin_search": C.linkedin_people_search_link(r, c["name"] or "")}
                    for r in open_roles]},
                "risks_unknowns": unknowns,
                "research": [dict(r) for r in con.execute(
                    "SELECT x.id, p.full_name, x.status, x.confidence, x.review_status, x.created_at FROM "
                    "profile_research x JOIN person_roles r ON r.person_id=x.person_id JOIN people p ON p.id=x.person_id "
                    "WHERE r.company_id=? GROUP BY x.id", (cid,))],
                "drafts": [dict(r) for r in con.execute(
                    "SELECT d.id, p.full_name, d.subject, d.status, d.updated_at FROM outreach_drafts d JOIN people p ON "
                    "p.id=d.person_id JOIN person_roles r ON r.person_id=d.person_id WHERE r.company_id=? GROUP BY d.id",
                    (cid,))],
                "reviews": human_reviews(con, cid),
                "merges": [dict(r) for r in con.execute("SELECT kept_id, merged_id, reason, by, at FROM company_merges "
                                                        "WHERE kept_id=? OR merged_id=?", (cid, cid))]}


@router.get("/accounts/{cid}/domain-suggestions")
def domain_suggestions(cid: int):
    """Official-website candidates from Wikidata for an account; suggestions only, a human confirms via /resolve."""
    from chanakya.intel.domain_suggest import SuggestError, suggest_domains
    with db() as con:
        c = con.execute("SELECT name, domain FROM companies WHERE id=?", (cid,)).fetchone()
    if c is None:
        raise HTTPException(404, "unknown account")
    try:
        rows = suggest_domains(c["name"])
    except SuggestError as e:
        raise HTTPException(502, str(e))
    return {"company_id": cid, "name": c["name"], "current_domain": c["domain"], "source": "wikidata",
            "note": "Suggestions only. Check the Wikidata item and the website before confirming.", "rows": rows}


@router.post("/accounts/{cid}/resolve")
def resolve_account(cid: int, request: Request, body: dict = Body(...)):
    """A human supplies the real domain for an unresolved account (merges if another record owns it)."""
    by, reason = who(request), _reason(body)
    with db() as con:
        c = con.execute("SELECT name FROM companies WHERE id=?", (cid,)).fetchone()
        if c is None:
            raise HTTPException(404, "unknown account")
        d = E.norm_domain(body.get("domain"))
        if not d:
            raise HTTPException(400, "a valid domain is required")
        owner = con.execute("SELECT id FROM companies WHERE domain=? AND id!=?", (d, cid)).fetchone()
        if owner:
            E.merge_companies(con, keep=owner[0], drop=cid, by=by, reason=f"human: {reason}")
            kept = owner[0]
        else:
            con.execute("UPDATE companies SET domain=?, identity_status='resolved' WHERE id=?", (d, cid))
            E.add_alias(con, cid, "domain", d, f"human:{by}")
            kept = cid
        E.record_discovery(con, "company", kept, E.TOP, f"human-resolution:{by}", detail={"reason": reason, "domain": d})
        SC.rescore_company(con, kept)
        con.commit()
        return {"company_id": kept}


@router.post("/accounts/{cid}/correct-domain")
def correct_account_domain(cid: int, request: Request, body: dict = Body(...)):
    """A human fixes a wrong domain on a resolved account; evidence from the old site is rejected (kept for audit)."""
    by, reason = who(request), _reason(body)
    with db() as con:
        try:
            r = E.correct_domain(con, cid, body.get("domain") or "", by=by, reason=reason)
        except ValueError as e:
            raise HTTPException(404 if "unknown account" in str(e) else 400, str(e))
        SC.rescore_company(con, r["company_id"])
        G.recompute(con)
        con.commit()
        return r


@router.get("/accounts/duplicates")
def account_duplicates():
    with db() as con:
        return {"rows": E.duplicate_candidates(con)}


@router.post("/accounts/duplicates/auto-merge")
def account_auto_merge(request: Request):
    """Merge only the pairs marked safe (one side unresolved; acronyms confirmed by the website domain)."""
    by = who(request)
    with db() as con:
        done = E.auto_merge(con, by=f"entity-resolver (run by {by})")
        for c in done:
            SC.rescore_company(con, c["keep"])
        con.commit()
        return {"merged": done, "remaining": [c for c in E.duplicate_candidates(con) if not c["safe"]]}


@router.post("/accounts/merge")
def merge_accounts(request: Request, body: dict = Body(...)):
    by, reason = who(request), _reason(body)
    with db() as con:
        try:
            E.merge_companies(con, keep=int(body["keep"]), drop=int(body["drop"]), by=by, reason=f"human: {reason}")
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))
        SC.rescore_company(con, int(body["keep"]))
        con.commit()
    return {"ok": True}


# ---------------- people ----------------
@router.get("/people/{pid}")
def person(pid: int):
    with db() as con:
        lead = M.leader_obj(con, pid)
        if lead is None:
            raise HTTPException(404, "unknown person")
        cid = lead["company_id"]
        return {"person": lead, "origin": E.origin(con, "person", pid),
                "discoveries": [dict(r) for r in con.execute("SELECT direction, source, run_id, detail_json, at FROM "
                                                             "discovery_events WHERE entity='person' AND entity_id=? "
                                                             "ORDER BY id", (pid,))],
                "sources": [dict(r) for r in con.execute("SELECT source, tool, query, fetched_at FROM person_sources "
                                                         "WHERE person_id=? ORDER BY id", (pid,))],
                "emails": [dict(r) for r in con.execute("SELECT id, email, status, method, confidence, source_url, "
                                                        "review_status FROM email_records WHERE person_id=?", (pid,))],
                "company_signals": [_signal_row(con, s) for s in con.execute("SELECT * FROM signals WHERE company_id=? "
                                                                             "ORDER BY id DESC", (cid,))] if cid else [],
                "opportunities": [_opp_row(con, r) for r in con.execute("SELECT * FROM opportunities WHERE "
                                                                        "company_id=? ORDER BY score DESC", (cid,))]
                if cid else [],
                "roles": [dict(r) for r in con.execute(
                    "SELECT r.id, r.title, r.review_status, c.name company FROM person_roles r JOIN companies c ON "
                    "c.id=r.company_id WHERE r.person_id=? ORDER BY r.id DESC", (pid,))],
                "drafts": [dict(r) for r in con.execute(
                    "SELECT id, subject, status, generator, outlook_message_id FROM outreach_drafts WHERE person_id=? "
                    "AND status!='discarded' ORDER BY id DESC", (pid,))],
                "role_history": [dict(r) for r in con.execute(
                    "SELECT h.action, h.from_status, h.to_status, h.reviewer, h.reason, h.at FROM "
                    "person_role_review_history h JOIN person_roles r ON r.id=h.role_id WHERE r.person_id=? ORDER BY h.id",
                    (pid,))]}


# ---------------- signal radar ----------------
@router.get("/signals")
def signals(type: str = "", industry: str = "", region: str = "", max_days: int = 0, verification: str = "",
            q: str = ""):
    with db() as con:
        rows = []
        for s in con.execute("SELECT * FROM signals ORDER BY id DESC"):
            d = _signal_row(con, s)
            if type and s["type"] != type or verification and s["verification_status"] != verification:
                continue
            if industry and industry.lower() not in (s["industry"] or "").lower():
                continue
            if region and region.lower() not in (s["region"] or "").lower():
                continue
            if max_days and d["days"] > max_days:
                continue
            if q and q.lower() not in f"{s['summary']} {s['company_name']}".lower():
                continue
            rows.append(d)
        facets = {k: sorted({r[0] for r in con.execute(f"SELECT DISTINCT {k} FROM signals WHERE {k} IS NOT NULL")})
                  for k in ("type", "industry", "region", "verification_status")}
        return {"rows": rows, "facets": facets, "types": ST.SIGNAL_TYPES,
                "method": "strength = weight(type) x exp(-days/60); days from the source date (capture date only when "
                          "no source date is known, shown as the basis)"}


@router.post("/signals/{sid}/review")
def review_signal(sid: int, request: Request, body: dict = Body(...)):
    by, reason = who(request), _reason(body)
    with db() as con:
        try:
            SG.review_signal(con, sid, body.get("status") or "", by=by, reason=reason)
        except ValueError as e:
            raise HTTPException(400, str(e))
        r = con.execute("SELECT company_id FROM signals WHERE id=?", (sid,)).fetchone()
        if r and r[0]:
            SC.rescore_company(con, r[0])
    return {"ok": True}


@router.post("/signals/{sid}/verify")
def verify_signal(sid: int, request: Request):
    from chanakya.emailhunter.checks import default_fetcher
    who(request)
    with db() as con:
        if not con.execute("SELECT 1 FROM signals WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "unknown signal")
        return SG.verify_signal(con, sid, default_fetcher())


@router.post("/signals/import-seed")
def import_seed(request: Request, body: dict = Body(...)):
    from chanakya.intel import proof
    who(request)
    path = body.get("path") or os.getenv("LAKSHYA_SEED")
    if not path or not os.path.exists(path):
        raise HTTPException(400, "seed file not found (set path or LAKSHYA_SEED)")
    with db() as con:
        proof.seed(con)
        r = SG.import_lakshya_seed(con, path)
        G.recompute(con)
        SC.rescore_all(con)
        return r


# ---------------- Lakshya handover intelligence ----------------
@router.get("/lakshya")
def lakshya(q: str = "", kind: str = ""):
    """Summary of the ingested handover plus a text search over every record and Lakshya signal."""
    from chanakya.intel import lakshya as LK
    with db() as con:
        return {"summary": LK.summary(con), "rows": LK.search(con, q, kind=kind or None) if (q or kind) else [],
                "kinds": [*LK.KINDS, "signal"]}


@router.post("/lakshya/import")
def lakshya_import(request: Request, body: dict = Body(default={})):
    """(Re)ingest the whole handover folder. Idempotent: unchanged records stay untouched, counts are validated
    against the source files, and every run is recorded in lakshya_imports."""
    from chanakya.intel import lakshya as LK
    from chanakya.intel import proof
    by = who(request)
    with db() as con:
        try:
            proof.seed(con)
            r = LK.import_all(con, body.get("path") or None, by=by)
        except FileNotFoundError as e:
            raise HTTPException(400, str(e))
        G.recompute(con)
        SC.rescore_all(con)
        return r


# ---------------- accelerators, gaps, refresh ----------------
@router.get("/accelerators")
def accelerators():
    with db() as con:
        ladder = {a["product_id"]: a for a in SC.accelerator_ladder(con)}
        out = []
        for r in con.execute("SELECT * FROM products ORDER BY name"):
            needs = [n for n in j(r["needs_json"], []) if isinstance(n, dict)]
            out.append({"id": r["id"], "name": r["name"], "status": r["status"], "summary": r["summary"],
                        "url": accelerator_url(r["id"]), "capabilities": [n.get("quote") for n in needs][:12],
                        "target_industries": j(r["target_industries_json"], []), "buyer_roles": j(r["buyer_roles_json"], []),
                        "limitations": j(r["caveats_json"], []), "evidence": j(r["evidence_json"], []),
                        "verification": r["verification"],
                        "proof": [dict(p) for p in con.execute("SELECT claim, allowed, note FROM accelerator_proof WHERE "
                                                               "product_id=? ORDER BY allowed DESC, id", (r["id"],))],
                        "personas": [dict(p) for p in con.execute(
                            "SELECT persona, status, titles_json FROM persona_role_mappings WHERE product_id=?", (r["id"],))],
                        "demand": ladder.get(r["id"], {"score": 0, "signals": 0, "companies": 0}),
                        "opportunities": con.execute("SELECT COUNT(*) FROM opportunities WHERE product_id=?",
                                                     (r["id"],)).fetchone()[0]})
        return {"rows": sorted(out, key=lambda a: -a["demand"]["score"])}


@router.get("/knowledge")
def knowledge(q: str = ""):
    """The Wayam knowledge layer: every accelerator with its readiness (ICP, personas, proof, approved wording), or
    with `q` the accelerators whose recorded text mentions it."""
    from chanakya.intel import knowledge as KN
    with db() as con:
        return {"rows": KN.search(con, q) if q else KN.catalogue(con)}


@router.get("/knowledge/{pid}")
def knowledge_accelerator(pid: str):
    """Everything recorded about one accelerator, with what outreach may and may not claim."""
    from chanakya.intel import knowledge as KN
    with db() as con:
        a = KN.accelerator(con, pid)
        if a is None:
            raise HTTPException(404, "unknown accelerator")
        return a


@router.get("/gaps")
def gaps():
    with db() as con:
        G.recompute(con)
        return {"rows": G.themes(con), "rule": "gap = need with no accelerator at 60+; Ready to pre-sell = 3+ accounts "
                                               "and 2+ signal types; Building evidence = 2+ accounts"}


@router.post("/gaps/{tid}/decide")
def decide_gap(tid: int, request: Request, body: dict = Body(...)):
    by, reason = who(request), _reason(body)
    with db() as con:
        try:
            G.decide(con, tid, body.get("status") or "", by=by, reason=reason)
        except ValueError as e:
            raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/refresh")
def refresh(request: Request, body: dict | None = Body(None)):
    from chanakya.emailhunter.checks import default_fetcher
    from chanakya.intel.research_web import refresh as rf
    who(request)
    body = body or {}
    with db() as con:
        r = rf(con, None, default_fetcher() if body.get("verify") else None, verify_limit=int(body.get("limit", 20)))
        return {"verified": r["verified"], "gap_themes": len(r["gap_themes"]), "opportunities": r["opportunities"]}


@router.post("/accounts/{cid}/research-web")
def research_web(cid: int, request: Request):
    from chanakya.emailhunter.checks import default_fetcher
    from chanakya.intel.research_web import company_web_signals
    who(request)
    with db() as con:
        r = company_web_signals(con, cid, default_fetcher())
        SC.rescore_company(con, cid)
        return r


@router.post("/accounts/{cid}/classify-signals")
def classify_signals(cid: int, request: Request):
    """Haiku labels this account's evidence: general news / potential signal / strong evidence (never 'verified')."""
    from chanakya.intel import ai_agents as AI
    who(request)
    with db() as con:
        try:
            return AI.classify_signals(con, cid)
        except AI.AgentUnavailable as e:
            raise HTTPException(503, str(e))
        except Exception as e:  # noqa: BLE001 - model/output failure: reported, nothing stored
            raise HTTPException(502, f"classification failed: {str(e)[:300]}")


@router.post("/accounts/{cid}/analysis")
def account_analysis(cid: int, request: Request):
    """Opus qualification brief citing only this account's usable evidence."""
    from chanakya.intel import ai_agents as AI
    by = who(request)
    with db() as con:
        try:
            return AI.analyse_account(con, cid, by=by)
        except AI.AgentUnavailable as e:
            raise HTTPException(503, str(e))
        except AI.AgentOutputError as e:
            raise HTTPException(409, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"analysis failed: {str(e)[:300]}")


@router.get("/suppression")
def suppression_list():
    from chanakya.outreach import suppression as SUP
    with db() as con:
        return {"rows": SUP.listing(con)}


@router.post("/suppression", status_code=201)
def suppression_add(request: Request, body: dict = Body(...)):
    """Add a person, email or domain to the do-not-contact list (reason required)."""
    from chanakya.outreach import suppression as SUP
    with db() as con:
        try:
            sid = SUP.add(con, by=who(request), reason=_reason(body), person_id=body.get("person_id"),
                          email=body.get("email"), domain=body.get("domain"))
        except ValueError as e:
            raise HTTPException(400, str(e))
    return {"id": sid}


@router.post("/people/{person_id}/draft")
def person_draft(person_id: int, request: Request):
    """Account-based outreach draft for a person whose role a human approved. Written by the routed 'outreach_draft'
    model when configured (labelled), otherwise the rule-based template (labelled). Never sent."""
    from chanakya.outreach.account_drafts import generate_for_person
    from chanakya.outreach.drafts import DraftError
    from chanakya.outreach.suppression import Suppressed
    from chanakya.workflow.services import make_llm
    by = who(request)
    with db() as con:
        try:
            r = generate_for_person(con, person_id, by=by, llm=make_llm(task="outreach_draft"),
                                    sender=os.getenv("CHANAKYA_SENDER") or "Vineet Shukla")
        except DraftError as e:
            raise HTTPException(409, str(e))
        except Suppressed as e:
            raise HTTPException(409, str(e))
        r["ai_check"] = _ai_check(con, r["draft_id"])
        return r


def _ai_check(con, did: int) -> dict:
    """Haiku's advisory check of a new draft (stored in draft history). Failure is reported, never hidden."""
    from chanakya.intel import ai_agents as AI
    try:
        return AI.check_draft(con, did)
    except AI.AgentUnavailable as e:
        return {"ran": False, "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ran": False, "reason": f"check failed: {e}"[:300]}


# ---------------- discovery: manual people import (compliant LinkedIn route) ----------------
@router.post("/import-people")
def import_people(request: Request, body: dict = Body(...)):
    from chanakya.bottomup.agent import import_people as imp
    from chanakya.emailhunter.checks import default_fetcher
    by = who(request)
    items = body.get("items") or []
    if not items:
        raise HTTPException(400, "items are required")
    with db() as con:
        try:
            r = imp(con, items, query=body.get("query") or "manual import", by=by,
                    workflow_id=body.get("workflow_id"), fetcher=default_fetcher())
        except ValueError as e:
            raise HTTPException(400, str(e))
        for lid in r["linked"]:
            cid = con.execute("SELECT company_id FROM bottomup_leads WHERE id=?", (lid,)).fetchone()[0]
            SC.rescore_company(con, cid)
        return r


@router.get("/linkedin/capabilities")
def linkedin_capabilities():
    """Every LinkedIn-related capability with how the data is obtained and whether it is available right now."""
    from chanakya.workflow.services import make_harness
    with db() as con:
        last = con.execute("SELECT mode FROM integration_checks WHERE name='clay' ORDER BY id DESC LIMIT 1").fetchone()
        h = make_harness()
        return C.linkedin_capabilities(con, harness_configured=bool(h and (h.harness_dir / "linkedin_harness.py")
                                                                    .exists()), clay_mode=last[0] if last else None)


@router.get("/linkedin-links")
def linkedin_links(q: str, company: str = ""):
    with db() as con:
        return C.manual_route(con, [q], company)


# ---------------- methodology settings ----------------
@router.get("/settings")
def get_settings():
    with db() as con:
        return {"settings": ST.all_settings(con), "signal_types": ST.SIGNAL_TYPES,
                "public_naming_allowed_today": C.public_naming_allowed(con),
                "linkedin_automation_enabled": C.linkedin_automation_enabled(con)}


@router.put("/settings/{key}")
def put_setting(key: str, request: Request, body: dict = Body(...)):
    by, reason = who(request), _reason(body)
    with db() as con:
        try:
            ST.put(con, key, body.get("value"), by=f"{by} ({reason})")
        except ValueError as e:
            raise HTTPException(400, str(e))
        if key in ("signal_weights", "recency_basis", "recency_half_scale_days"):
            SC.rescore_all(con)
        if key.startswith(("gap_", "presell_", "building_")):
            G.recompute(con)
    return {"ok": True}


# ---------------- integration checks (live, recorded) ----------------
def _record(con, name, mode, ok, detail, by):
    con.execute("INSERT INTO integration_checks(name,mode,ok,detail_json,by,at) VALUES(?,?,?,?,?,?)",
                (name, mode, int(ok), jdump(detail), by, now()))
    con.commit()


@router.post("/integrations/{name}/test")
def test_integration(name: str, request: Request):
    by = who(request)
    t0 = time.monotonic()
    with db() as con:
        try:
            if name in ("ollama", "qwen", "chanakya_agent"):
                from chanakya.llm import routing
                from chanakya.workflow.services import make_llm
                rt = routing.route("chanakya_agent")
                llm = make_llm(task="chanakya_agent") if rt.configured else None
                if llm is None:
                    _record(con, name, "unconfigured", False, {"provider": rt.provider, "model": rt.model,
                                                               "error": rt.reason}, by)
                    return {"ok": False, "mode": "unconfigured", "provider": rt.provider, "model": rt.model,
                            "error": rt.reason}
                out = llm.chat([{"role": "user", "content": "Reply with OK."}])
                ok = True
                d = {"provider": rt.provider, "model": rt.model, "answered_by": getattr(out, "model", None)}
            elif name == "clay":
                import asyncio
                from chanakya.clay.mcp_client import ClayMCPClient
                from chanakya.scout.agent import result_payload

                class NoBrowser:
                    redirect_uri = "http://127.0.0.1:33418/callback"

                    async def open_browser(self, url):
                        raise RuntimeError("Clay needs an interactive re-login (scripts/clay_discover.py)")

                    async def wait_for_code(self):
                        raise RuntimeError("no interactive login from the API")

                async def go():
                    async with ClayMCPClient(callback=NoBrowser(), allowed_tools={"get-current-workspace",
                                                                                 "get-credits-available"}) as c:
                        tools = await c.list_tools()
                        ws = result_payload(await c.call_tool("get-current-workspace", {}))
                        cr = result_payload(await c.call_tool("get-credits-available", {}))
                        return {"tools": len(tools), "workspace": ws, "credits": cr}
                d = asyncio.run(go())
                ok = str(d["workspace"].get("workspaceId")) == os.getenv("CLAY_WORKSPACE_ID", "1461095")
            elif name == "linkedin":
                if not C.linkedin_automation_enabled(con):
                    d = {"policy": "disabled", "note": "LinkedIn automation disabled by policy; manual links in use"}
                    _record(con, name, "disabled_by_policy", True, d, by)
                    return {"ok": True, "mode": "disabled_by_policy", **d}
                from chanakya.workflow.services import make_harness
                h = make_harness()
                st = h.status() if h else None
                ok, d = bool(st and st.ok and (st.data or {}).get("logged_in")), {"status": st.data if st else None}
            elif name == "anthropic":
                from chanakya.llm import routing
                rt = routing.route("simple_checks")
                if not rt.configured:
                    _record(con, name, "unconfigured", False, {"error": rt.reason}, by)
                    return {"ok": False, "mode": "unconfigured", "error": rt.reason}
                out = routing.AnthropicClient(rt.model).chat([{"role": "user", "content": "Reply with OK."}],
                                                             max_tokens=5)
                ok, d = True, {"model": rt.model, "answered_by": out.model}
            elif name == "tenders":
                import datetime as _dt
                from chanakya.intel.sources import tenders_uk
                items = tenders_uk.search("Power BI", since=_dt.date.today() - _dt.timedelta(days=30))
                ok, d = True, {"source": tenders_uk.API, "matching_notices_last_30_days": len(items)}
            elif name == "jobs":
                from chanakya.intel.sources import jobs_public
                d = {"feeds": {}}
                for feed in ("arbeitnow", "remotive"):
                    try:
                        d["feeds"][feed] = {"postings": len(jobs_public.fetch(feed, pages=1))}
                    except Exception as fe:  # noqa: BLE001 - per-feed result
                        d["feeds"][feed] = {"error": f"{type(fe).__name__}: {str(fe)[:120]}"}
                ok = any("postings" in v for v in d["feeds"].values())
            elif name == "outlook":
                from chanakya.outreach import outlook as OL
                st = OL.status()
                ok = bool(st.get("authenticated"))
                d = {k: st.get(k) for k in ("installed", "mcp_connected", "authenticated", "account",
                                            "send_tools_exposed", "error") if k in st}
                d["tool_count"] = len(st.get("tools") or [])
                if not ok:
                    mode = "not_signed_in" if st.get("mcp_connected") else "failed"
                    _record(con, name, mode, False, d, by)
                    return {"ok": False, "mode": mode, **d,
                            "error": d.get("error") or "no mailbox signed in (Settings > Outlook > Connect)"}
            elif name == "web":
                from chanakya.emailhunter.checks import default_fetcher
                p = default_fetcher()("https://wayam.ai/")
                ok, d = bool(p.status and p.status < 400), {"url": p.url, "http_status": p.status, "error": p.error}
            elif name == "dns":
                from chanakya.emailhunter.checks import check_mx
                m = check_mx("wayam.ai")
                ok, d = m.status in ("ok", "none"), m.as_dict()
            else:
                raise HTTPException(404, "unknown integration")
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            from chanakya.clay.mcp_client import ClayReauthRequired, unwrap
            leaf = unwrap(e)
            if isinstance(leaf, ClayReauthRequired):
                d = {"error": str(leaf), "action": "python scripts/clay_discover.py",
                     "ms": int((time.monotonic() - t0) * 1000)}
                _record(con, name, "reauth_required", False, d, by)
                return {"ok": False, "mode": "reauth_required", **d}
            ok, d = False, {"error": f"{type(leaf).__name__}: {str(leaf)[:300]}"}
        d["ms"] = int((time.monotonic() - t0) * 1000)
        mode = "live" if ok else "failed"
        _record(con, name, mode, ok, d, by)
        return {"ok": ok, "mode": mode, **d}


@router.get("/integrations")
def integrations_status():
    with db() as con:
        out = {}
        for name in ("ollama", "anthropic", "clay", "linkedin", "outlook", "tenders", "jobs", "web", "dns"):
            last = con.execute("SELECT * FROM integration_checks WHERE name=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
            good = con.execute("SELECT at FROM integration_checks WHERE name=? AND ok=1 AND mode IN ('live',"
                               "'disabled_by_policy') ORDER BY id DESC LIMIT 1", (name,)).fetchone()
            out[name] = {"mode": last["mode"] if last else "unverified", "ok": bool(last["ok"]) if last else None,
                         "last_checked": last["at"] if last else None, "last_success": good[0] if good else None,
                         "detail": j(last["detail_json"], {}) if last else None}
        return {"integrations": out, "simulated": os.getenv("CHANAKYA_SIMULATED_EXTERNALS") == "1"}


# ---------------- Clay: connect / reconnect from the UI ----------------
def _clay_done(result: dict) -> None:
    with db() as con:
        ok = result.get("state") == "connected"
        _record(con, "clay", "live" if ok else "failed", ok,
                {k: result.get(k) for k in ("tool_count", "workspace", "credits", "error")}, "clay connect")


@router.post("/clay/connect")
def clay_connect(request: Request):
    """Start the Clay OAuth flow. Returns `auth_url` when a browser authorisation is needed (open it, approve, and the
    loopback listener finishes the login), or `connected` when the stored token still works."""
    who(request)
    if os.getenv("CHANAKYA_SIMULATED_EXTERNALS") == "1":
        raise HTTPException(409, "Simulated mode: Clay is replaced by a fake; nothing to connect.")
    from chanakya.clay import connect as CC
    return CC.start(on_done=_clay_done)


@router.get("/clay/connect")
def clay_connect_status():
    from chanakya.clay import connect as CC
    return CC.status()


@router.post("/clay/connect/cancel")
def clay_connect_cancel(request: Request):
    who(request)
    from chanakya.clay import connect as CC
    return CC.cancel()


@router.post("/clay/disconnect")
def clay_disconnect(request: Request):
    who(request)
    from chanakya.clay import connect as CC
    return CC.disconnect()



# ---------------- model routing ----------------
@router.get("/models")
def models(live: bool = True, test: bool = False):
    """Configured vs available vs tested, per provider and per routed task. `test=true` makes one tiny real call."""
    from chanakya.llm import routing
    h = routing.health(live=live, test_call=test)
    with db() as con:
        h["usage"] = routing.usage(con)
        if test:  # keep the result of every real test call, so "last tested" survives a page reload
            a = h["providers"]["anthropic"]
            for tier, m in (a.get("models") or {}).items():
                if m.get("available") is not None:
                    _record(con, f"model:{m['model']}", "live" if m.get("tested") else "failed", bool(m.get("tested")),
                            {"tier": tier, "available": m.get("available"), "error": m.get("error")}, "model test")
            qw = h["providers"]["qwen"]
            _record(con, f"model:{qw['model']}", "live" if qw.get("tested") else
                    ("unconfigured" if not qw["configured"] else "failed"), bool(qw.get("tested")),
                    {"error": qw.get("error"), "answered_by": qw.get("answered_by")}, "model test")
        h["tests"] = {r["name"][6:]: {"mode": r["mode"], "ok": bool(r["ok"]), "at": r["at"],
                                       "detail": j(r["detail_json"], {})}
                      for r in con.execute("SELECT * FROM integration_checks WHERE name LIKE 'model:%' AND id IN "
                                           "(SELECT MAX(id) FROM integration_checks GROUP BY name)")}
    return h


# ---------------- Wayam content catalogue ----------------
@router.get("/content")
def content_get():
    from chanakya.intel import content as CT
    try:
        return {"path": str(CT.path()), "content": CT.load(), "issues": []}
    except CT.ContentError as e:
        raise HTTPException(500, str(e))


@router.put("/content")
def content_put(request: Request, body: dict = Body(...)):
    from chanakya.intel import content as CT
    data = body.get("content")
    if not isinstance(data, dict):
        raise HTTPException(400, "body must be {\"content\": {...}}")
    try:
        CT.save(data)
    except CT.ContentError as e:
        raise HTTPException(400, f"not saved: {e}")
    return {"saved": True, "by": who(request), "content": CT.load()}


@router.get("/content/accelerators/{pid}")
def content_accelerator(pid: str):
    from chanakya.intel import content as CT
    with db() as con:
        try:
            return CT.for_accelerator(con, pid)
        except CT.ContentError as e:
            raise HTTPException(404, str(e))


# ---------------- Outlook (drafts only, through MCP) ----------------
@router.get("/outlook/status")
def outlook_status():
    from chanakya.outreach import outlook as O
    return O.status()


# ---- Chanakya's own Entra app (delegated Graph). Runs alongside the MCP route above; nothing there changes.
def _outlook_provider() -> str:
    """Chanakya's own Entra app when it is configured (Outlook-only consent shown as "Chanakya"), else the MCP route.
    OUTLOOK_PROVIDER=mcp|graph overrides."""
    from chanakya.outreach import graph_oauth as G
    return (os.getenv("OUTLOOK_PROVIDER") or ("graph" if G.configured() else "mcp")).lower()


@router.get("/outlook/graph/status")
def graph_status(request: Request):
    from chanakya.outreach import graph_oauth as G
    with db() as con:
        return {**G.status(con, auth.current_user(request)["id"]),
                "default_provider": _outlook_provider()}


@router.post("/outlook/graph/connect")
def graph_connect(request: Request):
    from chanakya.outreach import graph_oauth as G
    try:
        return G.start(auth.current_user(request)["id"])
    except G.GraphAuthError as e:
        raise HTTPException(400, str(e))


@router.get("/outlook/graph/callback")
def graph_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
                   error_description: str | None = None):
    from urllib.parse import quote

    from fastapi.responses import RedirectResponse

    from chanakya.outreach import graph_oauth as G
    with db() as con:
        try:
            G.callback(con, auth.current_user(request)["id"], code=code, state=state, error=error,
                       error_description=error_description)
            return RedirectResponse("/settings/outlook?graph=connected", 303)
        except G.GraphAuthError as e:
            return RedirectResponse(f"/settings/outlook?graph_error={quote(str(e)[:600])}", 303)


@router.post("/outlook/graph/disconnect")
def graph_disconnect(request: Request):
    from chanakya.outreach import graph_oauth as G
    with db() as con:
        return G.disconnect(con, auth.current_user(request)["id"])


@router.post("/outlook/connect")
def outlook_connect():
    """Start device-code sign-in through the MCP server; returns the code for microsoft.com/devicelogin."""
    from chanakya.outreach import outlook as O
    try:
        return O.start_login()
    except O.OutlookError as e:
        raise HTTPException(400, str(e))


@router.get("/outlook/connect")
def outlook_connect_state():
    from chanakya.outreach import outlook as O
    return O.login_state()


@router.post("/outlook/connect/cancel")
def outlook_connect_cancel():
    from chanakya.outreach import outlook as O
    return O.cancel_login()


@router.post("/outlook/disconnect")
def outlook_disconnect():
    """Sign out: clears the MCP server's token cache. Chanakya never held the token."""
    from chanakya.outreach import outlook as O
    try:
        return O.logout()
    except O.OutlookError as e:
        raise HTTPException(400, str(e))


@router.post("/drafts/{did}/outlook")
def outlook_draft(did: int, request: Request, provider: str | None = None):
    """Put a human-reviewed draft into the signed-in user's Outlook Drafts folder. Nothing is sent.
    provider: 'mcp' (Softeria MCP server, default) or 'graph' (Chanakya's own Entra app); OUTLOOK_PROVIDER sets the
    default."""
    from chanakya.outreach import outlook as O
    prov = (provider or _outlook_provider()).lower()
    with db() as con:
        try:
            if prov == "graph":
                from chanakya.outreach.graph_oauth import GraphSession
                uid = auth.current_user(request)["id"]
                return {**O.create_outlook_draft(con, did, by=who(request), allow_unreviewed=True,
                                                 session_factory=lambda: GraphSession(con, uid)), "provider": "graph"}
            return O.create_outlook_draft(con, did, by=who(request), allow_unreviewed=True)
        except O.OutlookError as e:
            msg = str(e)
            if "No accounts found" in msg or "No valid token" in msg or "login first" in msg.lower():
                raise HTTPException(409, "Outlook is not signed in. Connect the mailbox under Settings > Outlook, then "
                                         "press Save to Outlook Drafts again. The draft is unchanged and nothing was "
                                         "sent.")
            raise HTTPException(404 if msg.startswith("unknown draft") else 409 if "already in Outlook" in msg
                                else 400, msg)
