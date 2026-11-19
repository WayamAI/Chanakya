"""Map Chanakya's real tables onto the shapes the LinkedIn-harness UI expects (web/src/lib/types.ts).

Nothing here invents data: absent values are null/empty. Notable mappings:
  * UI "Run"  = a workflow (top-down or bottom-up). `month` = "<Top-down|Bottom-up> #id · date".
  * The UI's 7 agent cards = our stages grouped by agent (STAGE_AGENT). Gates count with their agent.
  * Company.fit_score stays null: our search-filter score is NOT a commercial fit (see scout/qualification.py);
    it is shown as `icp` (share of ICP search filters matched) instead.
  * Leader = person (+ best role, company, approved/latest email, latest research, prospect tier).
  * Email (draft) statuses: draft/edited -> draft, ready -> approved, discarded -> skipped.
"""
from __future__ import annotations

import json

AGENTS = ["Catalogue Agent", "Company Scout", "People Finder", "LinkedIn Resolver", "Email Hunter",
          "Profile Researcher", "QA and Sync"]
STAGE_AGENT = {"catalog": 1, "bu_match": 1, "intel": 1, "signal_sweep": 2, "company_signals": 2, "scout": 2, "company_gate": 2, "bu_company": 2, "people": 3,
               "role_gate": 3, "bu_search": 3, "bu_relevance": 3, "bu_relevance_gate": 3, "bu_profile": 3,
               "resolve": 4, "linkedin_gate": 4, "email": 5, "email_gate": 5, "research": 6, "research_gate": 6,
               "qa": 7, "qa_gate": 7, "outreach": 7}
KIND_LABEL = {"top_down": "Company search", "bottom_up": "People search", "combined": "Companies + people"}
DRAFT_STATUS = {"draft": "draft", "edited": "draft", "generated": "draft", "needs_editing": "draft", "failed": "draft",
                "approved": "approved", "ready": "approved", "saved_to_outlook": "exported", "discarded": "skipped",
                "rejected": "skipped"}


def j(v, default=None):
    """Decode a JSON column; JSON `null` and bad values give `default` (the UI expects lists, not null)."""
    try:
        r = json.loads(v) if v else None
    except (TypeError, ValueError):
        return default
    return default if r is None else r


def _one(con, sql, *a):
    r = con.execute(sql, a).fetchone()
    return r[0] if r else None


# ---------------- runs ----------------
def run_status(wf, stages: list, job) -> tuple[str, str | None]:
    if job is not None and job.workflow_id == wf["id"] and job.finished is None:
        return "running", None
    cur = next((s for s in stages if s["stage"] == wf["current_stage"]), None)
    msg = cur["error"] if cur else None
    st = wf["status"]
    if st == "completed":
        return "awaiting_emails", "Drafts are ready for your review. Chanakya never sends them."
    if st == "failed":
        return "failed", msg
    if st == "cancelled":
        return "cancelled", "Cancelled by you"
    if st == "waiting_human":
        if wf["current_stage"] == "company_gate":
            return "awaiting_companies", None  # the UI shows "Approve the companies"; not an error
        return "paused", f"Waiting for you: {msg}" if msg else "Waiting for a review"
    if st == "blocked":
        return "paused", f"Blocked: {msg}" if msg else "Blocked"
    if st == "paused":
        return "paused", "Paused by you"
    if st == "created":
        return "paused", "Not started yet: press Resume run"
    return "paused", msg  # 'running' persisted but no live job (e.g. API restarted mid-stage)


def run_obj(con, wf, job=None) -> dict:
    stages = [dict(r) for r in con.execute("SELECT * FROM workflow_stages WHERE workflow_id=? ORDER BY seq",
                                           (wf["id"],))]
    status, reason = run_status(wf, stages, job)
    finished = max((s["finished_at"] for s in stages if s["finished_at"]), default=None) \
        if wf["status"] in ("completed", "failed", "cancelled") else None
    llm = con.execute("SELECT COUNT(*), COALESCE(SUM(prompt_tokens+output_tokens),0) FROM llm_calls WHERE at>=?",
                      (wf["created_at"],)).fetchone()
    params = j(wf["params_json"], {})
    return {"id": wf["id"], "month": f"{KIND_LABEL.get(wf['kind'], wf['kind'])} #{wf['id']} · {wf['created_at'][:10]}",
            "status": status, "stage": wf["current_stage"], "started": wf["created_at"], "finished": finished,
            "stats": {"companies_found": _one(con, "SELECT COUNT(*) FROM companies"), "llm_calls": llm[0],
                      "llm_tokens": llm[1], "briefs": _one(con, "SELECT COUNT(*) FROM profile_research WHERE "
                                                                "status='researched'"),
                      "steps_done": [s["stage"] for s in stages if s["status"] == "done"]},
            "error": next((s["error"] for s in stages if s["status"] == "failed"), None), "reason": reason,
            "control": (job.control if job is not None and job.workflow_id == wf["id"] and job.finished is None
                        else None), "icp": None, "kind": wf["kind"], "params": params}


def agent_progress(con, wid: int, job=None) -> list[dict]:
    stages = [dict(r) for r in con.execute("SELECT * FROM workflow_stages WHERE workflow_id=? ORDER BY seq", (wid,))]
    running = job is not None and job.workflow_id == wid and job.finished is None
    out = []
    for n, name in enumerate(AGENTS, 1):
        mine = [s for s in stages if STAGE_AGENT.get(s["stage"]) == n]
        sts = [s["status"] for s in mine]
        if not mine:
            out.append({"n": n, "name": f"{name} (not used here)", "state": "done", "done": 0, "total": 0,
                        "started": None, "finished": None})
            continue
        if "failed" in sts:
            state = "failed"
        elif "running" in sts and running:
            state = "active"
        elif {"waiting_human", "blocked"} & set(sts):
            state = "paused"
        elif all(x == "done" for x in sts):
            state = "done"
        else:
            state = "todo"
        out.append({"n": n, "name": name, "state": state, "done": sts.count("done"), "total": len(mine),
                    "started": min((s["started_at"] for s in mine if s["started_at"]), default=None),
                    "finished": max((s["finished_at"] for s in mine if s["finished_at"]), default=None)
                    if state == "done" else None})
    return out


def log_lines(con, wid: int, limit: int = 50) -> list[dict]:
    rows = con.execute("SELECT * FROM workflow_events WHERE workflow_id=? ORDER BY id DESC LIMIT ?",
                       (wid, limit)).fetchall()[::-1]
    out = []
    for r in rows:
        n = STAGE_AGENT.get(r["stage"] or "")
        out.append({"ts": r["at"], "agent": AGENTS[n - 1] if n else "Orchestrator",
                    "msg": f"{r['stage']}: {r['message']}" if r["stage"] else r["message"]})
    return out


# ---------------- companies ----------------
def company_obj(con, r, run_id: int | None = None) -> dict:
    d = j(r["data_json"], {})
    a = j(r["assessment_json"], {})
    src = con.execute("SELECT source FROM company_sources WHERE company_id=? ORDER BY id LIMIT 1", (r["id"],)).fetchone()
    prod = con.execute("SELECT p.id, p.name FROM icps i JOIN products p ON p.id=i.product_id WHERE i.id=?",
                       (r["icp_id"],)).fetchone() if r["icp_id"] else None
    reasons = a.get("review_reasons") or []
    why = "; ".join(x if isinstance(x, str) else (x.get("reason") or x.get("detail") or json.dumps(x))
                    for x in reasons)[:600] or None
    qs, qsrc = r["qualification_status"], r["qualification_source"]
    return {"id": r["id"], "run_id": run_id, "name": r["name"] or r["domain"], "domain": r["domain"],
            "linkedin_url": d.get("linkedin_url") or (d.get("url") if "linkedin.com/company" in str(d.get("url"))
                                                      else None),
            "industry": d.get("industry"), "size": d.get("size"), "region": d.get("country") or d.get("locality"),
            "about": d.get("description") or d.get("about_excerpt"), "signals": [], "source": src[0] if src else None,
            "offering_id": None, "offering": prod["name"] if prod else None, "fit_score": None,
            # UI shows ICP on a 0-40 scale; ours is the 0-100 share of ICP search filters matched.
            "icp": None if r["search_filter_score"] is None else round(r["search_filter_score"] * 0.4),
            "icp_filter_match_pct": r["search_filter_score"], "intent": None, "timing": None, "why": why,
            "selected": int(qs == "qualified" and qsrc == "human"),
            "status": "removed" if qs == "disqualified" else qs, "removed_reason":
                "Disqualified by a reviewer" if qs == "disqualified" else None,
            "qualification_status": qs, "qualification_source": qsrc,
            "leaders": _one(con, "SELECT COUNT(DISTINCT person_id) FROM person_roles WHERE company_id=?", r["id"])}


# ---------------- leaders ----------------
def leader_obj(con, pid: int, run_id: int | None = None) -> dict | None:
    p = con.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if p is None:
        return None
    role = con.execute("SELECT r.*, c.name cname, c.domain FROM person_roles r JOIN companies c ON c.id=r.company_id "
                       "WHERE r.person_id=? ORDER BY (r.review_status='approved') DESC, COALESCE(r.is_current,0) DESC, "
                       "r.id LIMIT 1", (pid,)).fetchone()
    em = con.execute("SELECT * FROM email_records WHERE person_id=? AND review_status!='rejected' ORDER BY "
                     "(review_status='approved') DESC, id DESC LIMIT 1", (pid,)).fetchone()
    rs = con.execute("SELECT * FROM profile_research WHERE person_id=? AND status='researched' ORDER BY "
                     "(review_status='approved') DESC, id DESC LIMIT 1", (pid,)).fetchone()
    pr = con.execute("SELECT tier, qa_status, updated_at FROM prospects WHERE person_id=?", (pid,)).fetchone()
    res = con.execute("SELECT id, review_status FROM linkedin_resolutions WHERE person_id=? ORDER BY id DESC LIMIT 1",
                      (pid,)).fetchone()
    draft = con.execute("SELECT status, subject FROM outreach_drafts WHERE person_id=? AND status!='discarded' ORDER BY "
                        "id DESC LIMIT 1", (pid,)).fetchone()
    facts, analysis = (j(rs["facts_json"], {}), j(rs["analysis_json"], {}) or {}) if rs else ({}, {})
    hooks = []
    for kind in ("priorities", "pain_points", "responsibilities"):
        for it in analysis.get(kind, []):
            ev = (it.get("evidence") or [{}])[0]
            hooks.append({"text": it["text"], "source_label": kind.replace("_", " ").title(),
                          "source_url": ev.get("source_url"), "why": ev.get("quote", "")})
    li_source = None
    if p["linkedin_url"]:
        li_source = "Resolver (human-approved)" if res and res["review_status"] == "approved" else \
            ("LinkedIn people search" if _one(con, "SELECT 1 FROM bottomup_leads WHERE person_id=?", pid) else
             "Provider record")
    rm = j(role["role_match_json"], {}) if role else {}
    return {
        "id": p["id"], "run_id": run_id, "company_id": role["company_id"] if role else None,
        "company": (role["cname"] or role["domain"]) if role else None, "name": p["full_name"],
        "title": role["title"] if role else None, "role_reason": rm.get("status"),
        "linkedin_url": p["linkedin_url"], "linkedin_source": li_source,
        "email": em["email"] if em else None,
        "email_source": f"{em['method']} ({em['review_status']})" if em else None,
        "email_status": ("invalid" if em["status"] == "not_found" else em["status"]) if em else None,
        "research": {"experience": facts.get("experience", [])[:30],
                     "posts": [{"text": a["text"], "exact_date": a["date"] if a.get("date_kind") == "exact" else None,
                                "approx_date": a["date"] if a.get("date_kind") == "approx" else None, "url": a.get("url")}
                               for a in facts.get("activity", []) if a.get("type") != "comment"],
                     "comments": [{"text": a["text"], "url": a.get("url"), "approx_date": a.get("date")}
                                  for a in facts.get("activity", []) if a.get("type") == "comment"],
                     "themes": [i["text"] for i in analysis.get("priorities", [])],
                     "pain_points": [i["text"] for i in analysis.get("pain_points", [])],
                     "source": rs["profile_url"], "collected": facts.get("collected_at"),
                     "review_status": rs["review_status"], "confidence": rs["confidence"]} if rs else None,
        "hooks": hooks, "confidence": pr["tier"] if pr else None,
        "status": role["review_status"] if role else "unknown", "checked": pr["updated_at"] if pr else None,
        "industry": None, "offering": None,
        "email_draft": DRAFT_STATUS.get(draft["status"]) if draft else None,
        "subject": draft["subject"] if draft else None,
        "qa_status": pr["qa_status"] if pr else None,
    }


def draft_obj(r) -> dict:
    hooks = j(r["hooks_json"], [])
    if isinstance(hooks, dict):  # LinkedIn-run drafts store one context object: show its evidence as sources
        hooks = [{"kind": str(e), "evidence": []} for e in hooks.get("evidence") or []]
    hooks = [h for h in hooks if isinstance(h, dict)]
    return {"id": r["id"], "leader_id": r["person_id"], "subject": r["subject"], "body": r["body"], "tone": "warm",
            "sources": [{"label": h.get("kind", "research"), "url": (h.get("evidence") or [{}])[0].get("source_url")}
                        for h in hooks],
            "why": r["generator"], "status": DRAFT_STATUS.get(r["status"], r["status"]), "updated": r["updated_at"],
            "exported": None, "outlook": ({"message_id": r["outlook_message_id"], "web_link": r["outlook_web_link"],
                                          "account": r["outlook_account"], "created_at": r["outlook_created_at"]}
                                         if "outlook_message_id" in r.keys() and r["outlook_message_id"] else None)}
