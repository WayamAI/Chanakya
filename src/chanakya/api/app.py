"""HTTP API for the LinkedIn-harness Next.js UI, backed by Chanakya's real agents, orchestrator and SQLite.

Run:  python -m chanakya.api            (http://127.0.0.1:8010/api, the port the UI's next.config.mjs expects)

The routes follow the harness UI contract (contracts/api.md) where the UI already calls them; the data comes from our
tables via api/mapping.py. Rules kept from the backend:
  * Every approval goes through the existing review functions with the SIGNED-IN USER as reviewer and a reason.
  * Workflows run in ONE background job (api/jobs.py) with live=True; each stage still enforces its own gates,
    Clay query confirmations, harness page budget, paid-email SpendApproval and LinkedIn read-only allowlists.
  * Nothing is ever sent: there is no send route; `/outreach/{id}/send` answers 410 and "sent" actions are refused.
Routes the UI calls that are not mapped yet answer 404, which the UI shows as "Not available yet".
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from chanakya.api import auth
from chanakya.api import mapping as M
from chanakya.api.db import ROOT, db
from chanakya.api.jobs import JobRunner
from chanakya.workflow import orchestrator as W

EXPORTS = Path(os.getenv("CHANAKYA_EXPORT_DIR", str(ROOT / "exports")))


def create_app(services_factory=None) -> FastAPI:
    from chanakya.api import simulated
    sim = simulated.enabled()
    real = services_factory is None and not sim  # the scheduler thread runs only with the real service set
    if services_factory is None:
        if sim:
            services_factory = simulated.services_factory
        else:
            from chanakya.workflow.services import make_services as services_factory
    if sim:
        with db() as con:
            simulated.seed(con)
    with db() as con:  # idempotent: give pre-existing records their discovery direction from stored provenance
        from chanakya.intel.entities import backfill_discovery
        backfill_discovery(con)
    app = FastAPI(title="Chanakya GTM API", version="0.2")
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.middleware("http")(auth.middleware)
    app.include_router(auth.router)
    from chanakya.api.reviews import drafts as drafts_router
    from chanakya.api.reviews import router as reviews_router
    app.include_router(reviews_router)
    app.include_router(drafts_router)
    from chanakya.api.intel_api import router as intel_router
    app.include_router(intel_router)
    from chanakya.api.workspace import router as workspace_router
    app.include_router(workspace_router)
    from chanakya.api.linkedin_live_api import router as li_live_router
    app.include_router(li_live_router)
    app.state.simulated = sim
    jobs = JobRunner(services_factory)
    app.state.jobs = jobs
    app.state.services_factory = services_factory
    from chanakya.workflow import scheduler as SCH
    with db() as con:
        SCH.ensure(con)
        app.state.recovered = W.recover_interrupted(con)  # runs cut off by a crash or restart become resumable
    app.state.scheduler = None
    if real and SCH.enabled_by_env():
        from chanakya.api.db import open_db as _open_db
        app.state.scheduler = SCH.SchedulerThread(jobs, _open_db,
                                                  float(os.getenv("CHANAKYA_SCHEDULER_TICK_S", "30"))).start()

    def job():
        return jobs.current

    def reviewer(request: Request) -> str:
        return auth.current_user(request)["name"]

    def latest_wf(con, run: str | None = None):
        if run:
            r = con.execute("SELECT * FROM workflows WHERE id=?", (int(run),)).fetchone()
        else:
            r = con.execute("SELECT * FROM workflows ORDER BY id DESC LIMIT 1").fetchone()
        return r

    def wf_or_404(con, wid: int):
        r = con.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
        if r is None:
            raise HTTPException(404, f"unknown run {wid}")
        return r

    # ---------------- summary / home ----------------
    def counts(con) -> dict:
        one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
        needs = (one("SELECT COUNT(*) FROM companies WHERE qualification_source!='human'") +
                 one("SELECT COUNT(*) FROM person_roles WHERE review_status='needs_manual_review'") +
                 one("SELECT COUNT(*) FROM linkedin_resolutions WHERE review_status='pending_review' AND status!='error'")
                 + one("SELECT COUNT(*) FROM email_records WHERE review_status='pending_review'") +
                 one("SELECT COUNT(*) FROM profile_research WHERE review_status='pending_review' AND status='researched'")
                 + one("SELECT COUNT(*) FROM prospects WHERE review_status='pending_review' AND qa_status!='blocked'"))
        return {"found": one("SELECT COUNT(*) FROM companies"),
                "companies": one("SELECT COUNT(*) FROM companies WHERE qualification_status='qualified' AND "
                                 "qualification_source='human'"),
                "leaders": one("SELECT COUNT(*) FROM people"),
                "with_li": one("SELECT COUNT(*) FROM people WHERE linkedin_url IS NOT NULL AND linkedin_url!=''"),
                "valid": one("SELECT COUNT(*) FROM email_records WHERE status='valid' AND review_status!='rejected'"),
                "risky": one("SELECT COUNT(*) FROM email_records WHERE status IN ('risky','unverified') AND "
                             "review_status!='rejected'"),
                "no_li": one("SELECT COUNT(DISTINCT p.id) FROM people p JOIN person_roles r ON r.person_id=p.id WHERE "
                             "r.review_status='approved' AND (p.linkedin_url IS NULL OR p.linkedin_url='')"),
                "review": one("SELECT COUNT(*) FROM prospects WHERE qa_status='needs_review'"),
                "drafts": one("SELECT COUNT(*) FROM outreach_drafts WHERE status IN ('draft','edited')"),
                "approved": one("SELECT COUNT(*) FROM outreach_drafts WHERE status='ready'"),
                "briefs": one("SELECT COUNT(*) FROM profile_research WHERE status='researched'"),
                "high": one("SELECT COUNT(*) FROM prospects WHERE tier='High'"),
                "medium": one("SELECT COUNT(*) FROM prospects WHERE tier='Medium'"),
                "low": one("SELECT COUNT(*) FROM prospects WHERE tier='Low'"),
                "offerings": one("SELECT COUNT(*) FROM products"), "needs": needs}

    def budget() -> dict:
        from chanakya.workflow.services import make_harness
        h = make_harness()
        return h.budget() if h else {"used": 0, "limit": int(os.getenv("DAILY_PAGE_LIMIT", "200"))}

    def summary(con, run=None) -> dict:
        wf = latest_wf(con, run)
        from chanakya.linkedin import live as LV  # the visible-Chrome agent's own daily page counter
        b = {"used": LV.pages_used(), "limit": LV.page_limit()}
        return {"run": M.run_obj(con, wf, job()) if wf else None, "counts": counts(con), "pages_today": b["used"],
                "page_limit": b["limit"], "demo": sim,  # SIMULATED external services -> "Demo data" badge
                # There is no mock mode: an unconfigured LLM is reported on the Integrations page instead.
                "mock": False, "llm_configured": bool(os.getenv("OLLAMA_API_KEY") and os.getenv("OLLAMA_MODEL")),
                "busy": jobs.busy}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/summary")
    def get_summary(run: str | None = None):
        with db() as con:
            return summary(con, run)

    @app.get("/api/home")
    def home(run: str | None = None):
        with db() as con:
            s = summary(con, run)
            rows = con.execute("SELECT * FROM companies ORDER BY (qualification_source='human' AND "
                               "qualification_status='qualified') DESC, COALESCE(search_filter_score,-1) DESC, id "
                               "LIMIT 5").fetchall()
            return {**s, "top": [M.company_obj(con, r, s["run"]["id"] if s["run"] else None) for r in rows]}

    # ---------------- runs = workflows ----------------
    @app.get("/api/runs")
    def runs(run: str | None = None):
        with db() as con:
            wfs = con.execute("SELECT * FROM workflows ORDER BY id DESC").fetchall()
            cur = next((w for w in wfs if run and str(w["id"]) == str(run)), wfs[0] if wfs else None)
            steps = [{"n": a["n"], "name": a["name"], "state": "active" if a["state"] == "active" else
                      ("done" if a["state"] == "done" else "todo")} for a in M.agent_progress(con, cur["id"], job())] \
                if cur else [{"n": i + 1, "name": n, "state": "todo"} for i, n in enumerate(M.AGENTS)]
            return {"runs": [M.run_obj(con, w, job()) for w in wfs], "current": M.run_obj(con, cur, job()) if cur
                    else None, "steps": steps}

    def default_params(con) -> dict:
        pid = os.getenv("CHANAKYA_DEFAULT_PRODUCT") or next(iter(con.execute(
            "SELECT i.product_id FROM companies c JOIN icps i ON i.id=c.icp_id WHERE i.product_id IS NOT NULL "
            "ORDER BY c.id LIMIT 1").fetchone() or []), None) or next(iter(con.execute(
                "SELECT product_id FROM persona_role_mappings WHERE status='approved' ORDER BY id LIMIT 1").fetchone()
                or []), None)
        from chanakya.api import workspace as WS
        p = {**WS.run_defaults(con), "product_id": pid, "out_dir": str(EXPORTS),
             "sender": os.getenv("CHANAKYA_SENDER", "[Your name]")}
        if sim:
            p["pilot_scope"] = str(EXPORTS / "no_pilot_scope.json")  # synthetic ICP: no real pilot scope applied
        return p

    @app.get("/api/workflow-options")
    def workflow_options():
        """What the start form can offer: products with an ICP and approved personas, and bottom-up queries
        planned from approved persona titles (bottom-up never invents roles)."""
        from chanakya.bottomup.agent import plan_queries
        with db() as con:
            prods = [dict(r) for r in con.execute(
                "SELECT p.id, p.name, (SELECT COUNT(*) FROM icps i WHERE i.product_id=p.id) icps, "
                "(SELECT COUNT(*) FROM persona_role_mappings m WHERE m.product_id=p.id AND m.status='approved') "
                "personas, (SELECT COUNT(*) FROM companies c JOIN icps i ON i.id=c.icp_id WHERE i.product_id=p.id) "
                "companies FROM products p ORDER BY p.name")]
            return {"products": prods, "default_product": default_params(con)["product_id"],
                    "bottom_up_queries": plan_queries(con, None, max_queries=10), "simulated": sim}

    @app.post("/api/runs", status_code=202)
    def start_run(body: dict | None = Body(None)):
        """Body optional: {kind: top_down|bottom_up, params: {...}}. Default: top-down for the pilot product."""
        body = body or {}
        kind = body.get("kind", "top_down")
        with db() as con:
            params = {**default_params(con), **(body.get("params") or {})}
            if kind == "top_down" and not params.get("product_id"):
                raise HTTPException(400, "no product to run: set CHANAKYA_DEFAULT_PRODUCT or pass params.product_id")
            try:
                wid = W.create_workflow(con, kind, params)
            except ValueError as e:
                raise HTTPException(400, str(e))
        try:
            jobs.start(wid)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"started": True, "run_id": wid}

    @app.post("/api/runs/{wid}/resume", status_code=202)
    def resume(wid: int):
        with db() as con:
            wf = wf_or_404(con, wid)
            if wf["status"] == "cancelled":
                raise HTTPException(409, "This run was cancelled; start a new one")
            if wf["status"] == "completed":
                raise HTTPException(409, "This run is complete")
        try:
            jobs.start(wid)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"started": True}

    @app.post("/api/runs/{wid}/retry", status_code=202)
    def retry(wid: int):
        """Re-run from the stage that failed or is blocked/waiting (earlier results are kept)."""
        with db() as con:
            wf = wf_or_404(con, wid)
            if wf["status"] in ("cancelled", "completed"):
                raise HTTPException(409, f"run is {wf['status']}")
        try:
            jobs.start(wid)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"started": True}

    @app.post("/api/runs/{wid}/reset/{stage}")
    def reset(wid: int, stage: str):
        with db() as con:
            wf = wf_or_404(con, wid)
            if stage not in W.KINDS[wf["kind"]]:
                raise HTTPException(400, f"{stage} is not a stage of this run")
            if jobs.busy:
                raise HTTPException(409, "A run step is working; wait for it to finish")
            W.reset_from(con, wid, stage)
        return {"ok": True}

    @app.post("/api/runs/{wid}/stage/{stage}", status_code=202)
    def run_stage(wid: int, stage: str):
        with db() as con:
            wf = wf_or_404(con, wid)
            if stage not in W.KINDS[wf["kind"]]:
                raise HTTPException(400, f"{stage} is not a stage of this run")
        try:
            jobs.start(wid, stage=stage)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"started": True}

    def control(wid: int, what: str):
        with db() as con:
            wf = wf_or_404(con, wid)
            if wf["status"] == "cancelled":
                raise HTTPException(409, "already cancelled")
            if jobs.request(wid, what):
                return {"status": "running"}
            new = "cancelled" if what == "cancel" else "paused"
            con.execute("UPDATE workflows SET status=?, updated_at=? WHERE id=?", (new, M_now(), wid))
            con.execute("INSERT INTO workflow_events(workflow_id,level,message,at) VALUES(?,?,?,?)",
                        (wid, "info", f"{new} by user", M_now()))
            con.commit()
            return {"status": new}

    @app.post("/api/runs/{wid}/pause")
    def pause(wid: int):
        return control(wid, "pause")

    @app.post("/api/runs/{wid}/cancel")
    def cancel(wid: int):
        return control(wid, "cancel")

    @app.get("/api/runs/{wid}/progress")
    def progress(wid: int):
        with db() as con:
            wf_or_404(con, wid)
            return {"agents": M.agent_progress(con, wid, job()), "log": M.log_lines(con, wid)}

    @app.get("/api/runs/{wid}/stages")
    def stages(wid: int):
        with db() as con:
            wf_or_404(con, wid)
            return W.status(con, wid)

    @app.post("/api/runs/{wid}/confirm")
    def confirm(wid: int, request: Request, body: dict = Body(...)):
        """Spend control for live Clay searches: confirm the EXACT query a blocked stage printed.
        {stage: scout, query} or {stage: people, domain, query}. Recorded in the run log with the reviewer."""
        who = reviewer(request)
        with db() as con:
            wf_or_404(con, wid)
            if body.get("stage") == "scout":
                W.update_params(con, wid, confirm_scout_query=body.get("query"))
            elif body.get("stage") == "people" and body.get("domain"):
                cur = M.j(con.execute("SELECT params_json FROM workflows WHERE id=?", (wid,)).fetchone()[0], {})
                W.update_params(con, wid, confirm_people_query={**(cur.get("confirm_people_query") or {}),
                                                                body["domain"]: body.get("query")})
            else:
                raise HTTPException(400, "stage must be scout, or people with a domain")
            con.execute("INSERT INTO workflow_events(workflow_id,stage,level,message,at) VALUES(?,?,?,?,?)",
                        (wid, body["stage"], "info", f"query confirmed by {who}", M_now()))
            con.commit()
        return {"ok": True}

    @app.get("/api/runs/{wid}/funnel")
    def funnel(wid: int):
        with db() as con:
            c = counts(con)
            steps = [("found", "Companies found", c["found"]), ("approved_companies", "Companies qualified",
                                                                c["companies"]),
                     ("leaders", "People", c["leaders"]), ("linkedin", "With LinkedIn", c["with_li"]),
                     ("valid_email", "Valid emails", c["valid"]), ("briefs", "Research briefs", c["briefs"]),
                     ("drafts", "Drafts", c["drafts"] + c["approved"]), ("approved_emails", "Drafts ready",
                                                                         c["approved"])]
            return {"run_id": wid, "steps": [{"key": k, "label": lbl, "count": n} for k, lbl, n in steps]}

    @app.get("/api/runs/{wid}/companies")
    def run_companies(wid: int):
        with db() as con:
            wf_or_404(con, wid)
            rows = con.execute("SELECT * FROM companies ORDER BY COALESCE(search_filter_score,-1) DESC, id").fetchall()
            objs = [M.company_obj(con, r, wid) for r in rows]
            return {"run": M.run_obj(con, wf_or_404(con, wid), job()),
                    "rows": [o for o in objs if o["status"] != "removed"],
                    "removed": [o for o in objs if o["status"] == "removed"],
                    "offerings": sorted({o["offering"] for o in objs if o["offering"]})}

    @app.post("/api/runs/{wid}/approve", status_code=202)
    def approve_companies(wid: int, request: Request, body: dict = Body(...)):
        """Human company qualification (the only path is scout.qualification.record_human_decision). Selected
        companies become `qualified` with the signed-in reviewer; deselected ones are left untouched (no decision is
        recorded for them). Then the workflow resumes in the background."""
        from chanakya.scout.qualification import record_human_decision
        who = reviewer(request)
        keep = set(body.get("keep_ids") or [])
        reason = (body.get("reason") or "").strip() or f"Selected in the run approval screen by {who}"
        with db() as con:
            wf_or_404(con, wid)
            done = []
            for r in con.execute("SELECT id, domain, qualification_status, qualification_source FROM companies"):
                if r["id"] in keep and not (r["qualification_status"] == "qualified" and
                                            r["qualification_source"] == "human"):
                    record_human_decision(con, r["domain"], "qualified", who, reason)
                    done.append(r["domain"])
            con.commit()
        try:
            jobs.start(wid)
        except RuntimeError:
            pass  # a job is already running; the next resume picks the approvals up
        return {"started": True, "qualified": done}

    # ---------------- companies / leaders ----------------
    @app.get("/api/companies")
    def companies(run: str | None = None):
        with db() as con:
            rows = con.execute("SELECT * FROM companies ORDER BY id").fetchall()
            return {"rows": [M.company_obj(con, r) for r in rows]}

    @app.get("/api/companies/{cid}")
    def company(cid: int):
        with db() as con:
            r = con.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
            if r is None:
                raise HTTPException(404, "unknown company")
            pids = [x[0] for x in con.execute("SELECT DISTINCT person_id FROM person_roles WHERE company_id=?", (cid,))]
            return {"company": {**M.company_obj(con, r), "odesc": None},
                    "leaders": [M.leader_obj(con, p) for p in pids]}

    @app.post("/api/companies/{cid}/rescore")
    def company_rescore(cid: int):
        """Company page 'Rescore': recompute this account's opportunities from its current evidence."""
        from chanakya.intel import scoring as SC
        with db() as con:
            r = con.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
            if r is None:
                raise HTTPException(404, "unknown company")
            SC.rescore_company(con, cid)
            con.commit()
            return {"company": M.company_obj(con, con.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone())}

    @app.post("/api/companies/{cid}/exclude")
    def company_exclude(cid: int, request: Request):
        """Company page 'Exclude' (confirmed in a dialog): a human disqualification, recorded with reviewer + reason."""
        from chanakya.scout.qualification import record_human_decision
        reviewer = auth.current_user(request)["name"]
        with db() as con:
            r = con.execute("SELECT domain FROM companies WHERE id=?", (cid,)).fetchone()
            if r is None:
                raise HTTPException(404, "unknown company")
            record_human_decision(con, r["domain"], "disqualified", reviewer, "Excluded from the company page")
            con.commit()
            return {"company": M.company_obj(con, con.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone())}

    # Controls inherited from the harness UI whose capability lives elsewhere in this backend: answer with where to go
    # instead of a bare 404, so the UI's error toast is actionable.
    ELSEWHERE = {
        ("PUT", "/api/leaders/{pid}/hooks"): "Hook selection is not stored separately; edit the draft text in the "
        "Draft editor, where every claim is checked against the proof registry.",
    }

    def _elsewhere(msg: str):
        def handler():
            raise HTTPException(501, msg)
        return handler

    for (method, path), msg in ELSEWHERE.items():
        app.add_api_route(path, _elsewhere(msg), methods=[method], include_in_schema=False)

    FILTERS = {"all": lambda lead: True, "no_li": lambda lead: not lead["linkedin_url"],
               "risky": lambda lead: lead["email_status"] in ("risky", "unverified"),
               "low": lambda lead: lead["confidence"] == "Low",
               "pattern": lambda lead: bool(lead["email_source"] and lead["email_source"].startswith("pattern"))}

    @app.get("/api/leaders")
    def leaders(filter: str = "all", run: str | None = None):
        with db() as con:
            rows = [M.leader_obj(con, r[0]) for r in con.execute("SELECT id FROM people ORDER BY id")]
            return {"rows": [x for x in rows if FILTERS.get(filter, FILTERS["all"])(x)],
                    "filter_counts": {k: sum(1 for x in rows if f(x)) for k, f in FILTERS.items()}}

    LEADER_XLSX = [("Name", "name"), ("Title", "title"), ("Company", "company"), ("Role review", "status"),
                   ("LinkedIn", "linkedin_url"), ("LinkedIn source", "linkedin_source"), ("Email", "email"),
                   ("Email source", "email_source"), ("Email status", "email_status"), ("Confidence", "confidence"),
                   ("QA", "qa_status"), ("Draft", "email_draft"), ("Run", "run_id")]

    @app.get("/api/leaders.xlsx")
    def leaders_xlsx(run: str | None = None):
        """'Export to Excel' on Home, Companies, Leaders and Metrics: the leaders table as the UI shows it.
        Emails appear only with their method and review status; nothing unreviewed is presented as verified."""
        from openpyxl import Workbook
        with db() as con:
            rows = [M.leader_obj(con, r[0]) for r in con.execute("SELECT id FROM people ORDER BY id")]
        rows = [x for x in rows if x and (not run or str(x.get("run_id")) == str(run))]
        wb = Workbook()
        ws = wb.active
        ws.title = "Leaders"
        ws.append([h for h, _ in LEADER_XLSX])
        for x in rows:
            ws.append([x.get(k) for _, k in LEADER_XLSX])
        ws.freeze_panes = "A2"
        EXPORTS.mkdir(parents=True, exist_ok=True)
        path = EXPORTS / f"leaders_{dt.datetime.now():%Y%m%d-%H%M%S}.xlsx"
        wb.save(path)
        return FileResponse(path, filename=path.name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/leaders/{pid}")
    def leader(pid: int):
        with db() as con:
            lead = M.leader_obj(con, pid)
            if lead is None:
                raise HTTPException(404, "unknown person")
            d = con.execute("SELECT * FROM outreach_drafts WHERE person_id=? AND status!='discarded' ORDER BY id DESC "
                            "LIMIT 1", (pid,)).fetchone()
            ids = [r[0] for r in con.execute("SELECT id FROM people ORDER BY id")]
            i = ids.index(pid)
            return {"leader": lead, "email": M.draft_obj(d) if d else None,
                    "prev_id": ids[i - 1] if i > 0 else None, "next_id": ids[i + 1] if i + 1 < len(ids) else None}

    @app.post("/api/leaders/{pid}/email")
    def leader_email(pid: int, request: Request, body: dict = Body(...)):
        """Draft actions -> outreach/drafts.py; on a person with no draft, `approved`/`skipped` review their pending
        email finding (emailhunter.agent). `sent` and outcome actions are refused: Chanakya never sends."""
        from chanakya.emailhunter import agent as E
        from chanakya.outreach import drafts as D
        who, action = reviewer(request), body.get("action")
        if action in ("sent", "replied", "meeting", "won", "no_reply", "exported"):
            raise HTTPException(400, "Chanakya never sends email and does not track send outcomes yet.")
        with db() as con:
            d = con.execute("SELECT * FROM outreach_drafts WHERE person_id=? AND status!='discarded' ORDER BY id DESC "
                            "LIMIT 1", (pid,)).fetchone()
            if action == "regenerate":
                # "Write email now" / "Regenerate": an account-based draft from the company's evidence and the
                # approved role (Sonnet when configured, labelled template otherwise). A draft that is still open is
                # discarded first, with the reason in its history; a draft already marked ready is left alone.
                from chanakya.outreach.account_drafts import generate_for_person
                from chanakya.outreach.suppression import Suppressed
                from chanakya.workflow.services import make_llm
                if d is not None and d["status"] == "ready":
                    raise HTTPException(409, f"Draft #{d['id']} is already marked ready. Discard it in the Draft "
                                             "editor before writing a new one.")
                try:
                    if d is not None:
                        D.discard(con, d["id"], by=who, reason="replaced by a regenerated draft")
                    r = generate_for_person(con, pid, by=who, llm=make_llm(task="outreach_draft"),
                                            sender=os.getenv("CHANAKYA_SENDER") or "Vineet Shukla")
                except (D.DraftError, Suppressed) as e:
                    raise HTTPException(409, str(e))
                from chanakya.api.intel_api import _ai_check
                _ai_check(con, r["draft_id"])
                return {"email": M.draft_obj(con.execute("SELECT * FROM outreach_drafts WHERE id=?",
                                                         (r["draft_id"],)).fetchone()),
                        "leader": M.leader_obj(con, pid), "generator": r["generator"]}
            try:
                if d is not None:
                    if action == "save":
                        D.edit(con, d["id"], by=who, reason="edited in the UI", subject=body.get("subject"),
                               body=body.get("body"))
                    elif action == "approved":
                        D.mark_ready(con, d["id"], by=who, reason="approved in the UI (to send yourself)")
                    elif action == "skipped":
                        D.discard(con, d["id"], by=who, reason="skipped in the UI")
                    else:
                        raise HTTPException(400, f"action {action!r} is not supported")
                    return {"email": M.draft_obj(con.execute("SELECT * FROM outreach_drafts WHERE id=?",
                                                             (d["id"],)).fetchone())}
                er = con.execute("SELECT id FROM email_records WHERE person_id=? AND review_status='pending_review' "
                                 "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
                if er is None:
                    raise HTTPException(404, "no draft or pending email finding for this person")
                if action == "approved":
                    E.approve(con, er["id"], by=who, reason=body.get("reason") or "approved in the UI")
                elif action == "skipped":
                    E.reject(con, er["id"], by=who, reason=body.get("reason") or "rejected in the UI")
                else:
                    raise HTTPException(400, f"action {action!r} is not supported for an email finding")
                return {"email": None, "leader": M.leader_obj(con, pid)}
            except (D.DraftError, E.EmailReviewError) as e:
                raise HTTPException(400, str(e))

    @app.post("/api/leaders/{pid}/fix")
    def leader_fix(pid: int, request: Request, body: dict = Body(...)):
        """Needs-page fixes, each through the owning agent's review path with the signed-in reviewer:
        linkedin_url -> a 'manual' linkedin_resolutions row approved by you (resolver/review.approve);
        email -> emailhunter override (approved, method 'human'); drop -> the person's role is rejected."""
        from chanakya.emailhunter import agent as E
        from chanakya.people import role_review as RV
        from chanakya.people.finder import normalize_linkedin
        from chanakya.resolver import review as RR
        from chanakya.storage.db import jdump
        who = reviewer(request)
        reason = (body.get("reason") or "").strip() or f"Fixed on the Needs page by {who}"
        with db() as con:
            p = con.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
            if p is None:
                raise HTTPException(404, "unknown person")
            role = con.execute("SELECT * FROM person_roles WHERE person_id=? ORDER BY (review_status='approved') DESC, "
                               "id LIMIT 1", (pid,)).fetchone()
            try:
                if body.get("drop"):
                    if role is None or role["review_status"] == RV.REJECTED:
                        raise HTTPException(409, "nothing to drop")
                    RV.review_role(con, role["id"], RV.REJECTED, reviewer=who, reason=reason)
                if body.get("linkedin_url"):
                    url = normalize_linkedin(body["linkedin_url"])
                    if not url:
                        raise HTTPException(400, "not a linkedin.com/in/ profile URL")
                    rid = con.execute(
                        "INSERT INTO linkedin_resolutions(person_id,person_role_id,company_id,input_json,status,confidence,"
                        "profile_url,best_json,alternatives_json,searches_json,harness_json,harness_version,"
                        "harness_argv_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, role["id"] if role else None, role["company_id"] if role else None,
                         jdump({"name": p["full_name"], "source": "pasted by reviewer"}), "found", None, url,
                         jdump({"url": url, "title": p["full_name"], "lines": ["pasted by reviewer"]}), "[]", "[]",
                         jdump({"manual": True, "by": who}), "manual", "[]", M_now())).lastrowid
                    con.commit()
                    RR.approve(con, rid, by=who, reason=reason, url=url)
                if body.get("email"):
                    E.override(con, pid, body["email"], by=who, reason=reason, evidence_url=body.get("evidence_url"))
            except (RV.RoleReviewError, RR.ReviewError, E.EmailReviewError) as e:
                raise HTTPException(400, str(e))
            return {"leader": M.leader_obj(con, pid)}

    # ---------------- needs (open review gates) ----------------
    @app.get("/api/needs")
    def needs(run: str | None = None):
        with db() as con:
            wf = latest_wf(con, run)
            no_li = [M.leader_obj(con, r[0]) for r in con.execute(
                "SELECT DISTINCT p.id FROM people p JOIN person_roles r ON r.person_id=p.id WHERE r.review_status="
                "'approved' AND (p.linkedin_url IS NULL OR p.linkedin_url='')")]
            review = [M.leader_obj(con, r[0]) for r in con.execute(
                "SELECT person_id FROM prospects WHERE qa_status='needs_review' ORDER BY score")]
            risky = [{**M.leader_obj(con, r[0]), "subject": None} for r in con.execute(
                "SELECT DISTINCT person_id FROM email_records WHERE review_status='pending_review' AND status IN "
                "('risky','unverified')")]
            gates = {"companies": con.execute("SELECT COUNT(*) FROM companies WHERE qualification_source!='human'")
                     .fetchone()[0],
                     "roles": con.execute("SELECT COUNT(*) FROM person_roles WHERE review_status='needs_manual_review'")
                     .fetchone()[0],
                     "linkedin": con.execute("SELECT COUNT(*) FROM linkedin_resolutions WHERE review_status="
                                             "'pending_review' AND status!='error'").fetchone()[0],
                     "research": con.execute("SELECT COUNT(*) FROM profile_research WHERE review_status="
                                             "'pending_review' AND status='researched'").fetchone()[0],
                     "prospects": con.execute("SELECT COUNT(*) FROM prospects WHERE review_status='pending_review' AND "
                                              "qa_status!='blocked'").fetchone()[0]}
            return {"awaiting_companies": bool(wf and wf["status"] == "waiting_human" and
                                               wf["current_stage"] == "company_gate"),
                    "run_id": wf["id"] if wf else None, "no_linkedin": no_li, "review": review, "risky": risky,
                    "follow_up": [], "gates": gates}

    # ---------------- outreach (drafts only) ----------------
    @app.get("/api/outreach")
    def outreach(status: str = "draft", run: str | None = None):
        with db() as con:
            rows = con.execute("SELECT * FROM outreach_drafts ORDER BY id").fetchall()
            out = []
            for r in rows:
                d = M.draft_obj(r)
                if d["status"] != status:
                    continue
                lead = M.leader_obj(con, r["person_id"]) or {}
                out.append({**d, "name": lead.get("name"), "title": lead.get("title"), "addr": r["to_address"],
                            "email_status": lead.get("email_status"), "confidence": lead.get("confidence"),
                            "company": lead.get("company")})
            sc = {k: 0 for k in ("draft", "approved", "exported", "skipped", "replied", "meeting", "won")}
            sc.setdefault("draft", 0)
            for r in rows:
                sc[M.DRAFT_STATUS.get(r["status"], "draft")] += 1
            return {"rows": out, "status_counts": sc}

    @app.post("/api/outreach/bulk")
    def outreach_bulk(request: Request, body: dict = Body(...)):
        from chanakya.outreach import drafts as D
        who, action, n = reviewer(request), body.get("action"), 0
        if action not in ("approved", "skipped"):
            raise HTTPException(400, "action must be approved or skipped")
        with db() as con:
            for pid in body.get("ids") or []:
                d = con.execute("SELECT id FROM outreach_drafts WHERE person_id=? AND status IN ('draft','edited') "
                                "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
                if d is None:
                    continue
                try:
                    (D.mark_ready if action == "approved" else D.discard)(con, d["id"], by=who,
                                                                          reason=f"bulk {action} in the UI")
                    n += 1
                except D.DraftError:
                    pass  # e.g. [PERSONALISE] markers still present: left as draft
        return {"updated": n}

    @app.post("/api/outreach/export")
    def outreach_export():
        from chanakya.outreach import drafts as D
        from chanakya.qa import agent as Q
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = EXPORTS / f"drafts_{stamp}"
        with db() as con:
            files = D.export_eml(con, folder, status="ready")
            xlsx = EXPORTS / f"prospects_{stamp}.xlsx"
            Q.sync(con, [("xlsx", str(xlsx))], only_approved=True)
        return {"count": len(files), "folder": str(folder), "excel": str(xlsx), "download_url": f"/api/files/{xlsx.name}",
                "note": "Drafts (.eml) only. Nothing was sent."}

    @app.post("/api/outreach/{pid}/send")
    def no_send(pid: int):
        raise HTTPException(410, "Sending is disabled in Chanakya. Export the draft and send it yourself.")

    @app.get("/api/files/{name}")
    def files(name: str):
        p = (EXPORTS / name).resolve()
        if EXPORTS.resolve() not in p.parents or not p.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(p)

    # ---------------- notifications / integrations / settings / metrics / audit / search ----------------
    @app.get("/api/notifications")
    def notifications(request: Request):
        from chanakya.api.workspace import read_marker
        with db() as con:
            seen = read_marker(con, auth.current_user(request)["id"])
            out = []
            for w in con.execute("SELECT * FROM workflows WHERE status IN ('waiting_human','blocked','failed') ORDER "
                                 "BY id DESC LIMIT 20"):
                st = con.execute("SELECT error FROM workflow_stages WHERE workflow_id=? AND stage=?",
                                 (w["id"], w["current_stage"])).fetchone()
                out.append({"id": w["id"], "kind": f"run_{w['status']}", "title": f"Run #{w['id']} "
                            f"{w['current_stage']}: {(st[0] if st else '') or w['status']}"[:160],
                            "href": f"/runs/{w['id']}/approve" if w["current_stage"] == "company_gate" else "/runs",
                            "ts": w["updated_at"], "read": bool(seen) and w["updated_at"] <= seen})
            return out

    @app.get("/api/integrations")
    def integrations():
        from chanakya.workflow.services import make_harness
        h = make_harness()
        from chanakya.llm import routing
        with db() as con:
            def last(name):
                r = con.execute("SELECT mode, ok, at FROM integration_checks WHERE name=? ORDER BY id DESC LIMIT 1",
                                (name,)).fetchone()
                return (r["mode"], bool(r["ok"]), r["at"][:16]) if r else (None, False, None)
            li_on = con.execute("SELECT value_json FROM settings WHERE key='linkedin_automation'").fetchone()
            clay_mode, clay_ok, clay_at = last("clay")
            ol_mode, ol_ok, ol_at = last("outlook")
        li_on = bool(li_on) and json.loads(li_on[0]) == "enabled"
        an, qw, orch = routing.route("outreach_draft"), routing.route("chanakya_agent"), routing.orchestrator()
        items = [
            {"name": "Claude (Anthropic)", "ok": an.configured,
             "status": "Key set: Opus, Sonnet, Haiku routed per task" if an.configured else an.reason,
             "used_for": "Briefs (Opus), research, matching and drafts (Sonnet), classification and checks (Haiku)"},
            {"name": "Qwen Max orchestrator model", "ok": qw.configured,
             "status": f"{qw.model} via Alibaba Cloud Model Studio" if qw.configured else
             f"Not configured ({qw.reason}). Runs are coordinated by {orch['model'] or 'no model'} until it is",
             "used_for": "Run coordination (which accounts and next steps)"},
            {"name": "Clay MCP", "ok": clay_ok,
             "status": (f"Last check {clay_at}: {clay_mode.replace('_', ' ')}" if clay_mode else "Never tested") +
             f"; workspace {os.getenv('CLAY_WORKSPACE_ID', '1461095')}", "used_for": "Company and contact search"},
            {"name": "LinkedIn", "ok": True,
             "status": ("Automation enabled by a recorded decision" if li_on else
                        "Human-assisted: search links and import. Browser automation is off by policy"),
             "used_for": "People discovery (see the LinkedIn capabilities table below)"},
            {"name": "Company websites + DNS", "ok": True, "status": "robots.txt respected; MX lookup only",
             "used_for": "Evidence, leadership pages, email findings"},
            {"name": "Outlook mail drafts", "ok": ol_ok,
             "status": (f"Last check {ol_at}: {ol_mode.replace('_', ' ')}" if ol_mode else
                        "Never tested (Settings > Outlook)"), "used_for": "Saving reviewed drafts to your Outlook Drafts"},
            {"name": "Email sending", "ok": True, "status": "Disabled: Chanakya never sends. Drafts go to Outlook "
             "Drafts or export as .eml for you to send", "used_for": "Never"}]
        return {"items": items, "harness_installed": bool(h and (h.harness_dir / "linkedin_harness.py").exists())}

    @app.get("/api/integrations/mcp")
    def mcp_servers():
        """Configuration only: listing Clay's tools would open an OAuth session, so nothing connects here."""
        from chanakya.clay import policy
        from chanakya.outreach import outlook as OL
        return {"servers": [{"name": "clay", "configured": True, "connected": False,
                             "tools": sorted(policy.WORKSPACE_APPROVED), "error": None,
                             "setup": "Chanakya's own Clay OAuth client. Connect it under Operations > Clay > "
                                      "Connect. Tools shown are the approved allowlist; enrichment tools are held."},
                            {"name": "outlook", "configured": OL.server_js().exists(), "connected": False,
                             "tools": None, "error": None,
                             "setup": "ms-365-mcp-server (drafts only). Sign in under Settings > Outlook."}]}

    @app.post("/api/integrations/mcp/{name}/test")
    def mcp_test(name: str, request: Request):
        """A real connection test for an MCP server (clay or outlook): same check as Operations > Test now."""
        from chanakya.api.intel_api import test_integration
        if name not in ("clay", "outlook"):
            raise HTTPException(404, "unknown MCP server")
        r = test_integration(name, request)
        return {"ok": r.get("ok"), "tools": r.get("tools") or r.get("tool_count"),
                "error": None if r.get("ok") else (r.get("error") or r.get("mode"))}

    @app.get("/api/integrations/linkedin")
    def linkedin():
        from chanakya.workflow.services import make_harness
        h = make_harness()
        state = bool(h and (h.harness_dir / ".linkedin-state.json").exists())
        return {"logged_in": False, "state_file_exists": state, "last_checked": None, "login_running": False,
                "error": None if h else "LINKEDIN_HARNESS_DIR is not set",
                "note": "Session saved" if state else "Run `uv run linkedin_harness.py login` in the harness folder"}

    @app.get("/api/integrations/harness")
    def harness_status():
        from chanakya.resolver.harness_client import harness_version
        from chanakya.workflow.services import make_harness
        h = simulated.harness() if sim else make_harness()
        if h is None:
            return {"configured": False, "setup": [
                r"git clone https://github.com/mrigankad/Linkedinharness-.git C:\tools\linkedin-harness",
                "winget install astral-sh.uv   (or: pip install uv)", r"cd C:\tools\linkedin-harness; uv sync",
                "uv run linkedin_harness.py login   (log in by hand once)",
                "optional: uv run linkedin_harness.py serve   (one persistent browser, Chanakya attaches to it)",
                r"set LINKEDIN_HARNESS_DIR=C:\tools\linkedin-harness in chanakya-catalog-agent\.env, restart the API"]}
        return {"configured": True, "simulated": sim, "dir": str(h.harness_dir), "command": h.command_prefix,
                "installed": (h.harness_dir / "linkedin_harness.py").exists(),
                "version": harness_version(h.harness_dir), "browser_mode": h.browser_mode(), "budget": h.budget(),
                "session_file": (h.harness_dir / ".linkedin-state.json").exists(),
                "allowed": {"resolver": ["resolve", "status"], "researcher": ["research", "status"],
                            "bottom_up": ["people", "companies", "profile", "company", "status"]}}

    @app.post("/api/integrations/harness/check")
    def harness_check():
        """Explicit button: runs the harness `status` command (1 LinkedIn page view, read-only)."""
        from chanakya.resolver.harness_client import HarnessError
        from chanakya.workflow.services import make_harness
        h = simulated.harness() if sim else make_harness()
        if h is None:
            raise HTTPException(409, "LINKEDIN_HARNESS_DIR is not set")
        if jobs.busy:
            raise HTTPException(409, "A run step is working; try again when it finishes")
        try:
            h.check_installed()
            h.ensure_budget("status", 1)
            st = h.status()
            return {"ok": st.ok, "logged_in": bool((st.data or {}).get("logged_in")), "error": st.error,
                    "version": st.harness_version, "simulated": sim}
        except HarnessError as e:
            return {"ok": False, "logged_in": False, "error": f"{e.kind}: {e}", "simulated": sim}

    @app.get("/api/integrations/ollama")
    def ollama_status():
        with db() as con:
            calls = [dict(r) for r in con.execute("SELECT agent, purpose, model, ok, attempts, error, at FROM "
                                                  "llm_calls ORDER BY id DESC LIMIT 20")]
        return {"configured": bool(os.getenv("OLLAMA_API_KEY")), "model": os.getenv("OLLAMA_MODEL") or None,
                "host": os.getenv("OLLAMA_HOST", "https://ollama.com"),
                "fallback": os.getenv("OLLAMA_FALLBACK_MODEL") or None,
                "fallback_allowed": os.getenv("OLLAMA_ALLOW_FALLBACK", "false").lower() == "true", "calls": calls,
                "setup": "Optional. Ollama Cloud serves open models only and lists no Qwen Max, so it is not the "
                         "orchestrator route: Qwen Max needs DASHSCOPE_API_KEY (Alibaba Cloud Model Studio). Ollama is "
                         "used only if a task is explicitly routed to it with CHANAKYA_ROUTE_<TASK>=ollama."}

    @app.get("/api/exports/prospects.{fmt}")
    def export_prospects(fmt: str, approved_only: bool = False):
        from chanakya.qa import agent as Q
        if fmt not in ("csv", "xlsx"):
            raise HTTPException(404, "csv or xlsx")
        EXPORTS.mkdir(parents=True, exist_ok=True)
        path = EXPORTS / f"prospects_{dt.datetime.now():%Y%m%d-%H%M%S}.{fmt}"
        with db() as con:
            rep_ = Q.sync(con, [(fmt, str(path))], only_approved=approved_only)
        if rep_[0]["status"] != "ok":
            raise HTTPException(500, rep_[0].get("error", "export failed"))
        return FileResponse(path, filename=path.name)

    @app.get("/api/exports/database.xlsx")
    def export_database(company_ids: str | None = None, product_id: str | None = None, origin: str | None = None,
                        qualification_status: str | None = None):
        """All (or filtered) client records as one workbook: Companies, Contacts, Opportunities, Signals, Outreach,
        Research, Sources, plus a Summary of filters and row counts."""
        from chanakya.outreach import workbook as WB
        f = {k: v for k, v in {"product_id": product_id, "origin": origin,
                               "qualification_status": qualification_status}.items() if v}
        if company_ids:
            f["company_ids"] = [int(x) for x in company_ids.split(",") if x.strip().isdigit()]
        path = EXPORTS / f"chanakya_database_{dt.datetime.now():%Y%m%d-%H%M%S}.xlsx"
        with db() as con:
            WB.export(con, path, f, os.getenv("CHANAKYA_DB_PATH", "data/chanakya.db"))
        return FileResponse(path, filename=path.name)

    @app.get("/api/exports/view/{view}.{fmt}")
    def export_view(view: str, fmt: str, product_id: str | None = None, origin: str | None = None):
        """One named view (companies, contacts, leaders, opportunities, signals, research, email_enrichment, outreach,
        sources, audit, pending_approvals, qualified, top_down, bottom_up, combined) as xlsx or csv."""
        from chanakya.outreach import workbook as WB
        if view not in WB.VIEWS or fmt not in ("csv", "xlsx"):
            raise HTTPException(404, f"view one of {sorted(WB.VIEWS)}; fmt csv or xlsx")
        f = {k: v for k, v in {"product_id": product_id, "origin": origin}.items() if v}
        path = EXPORTS / f"chanakya_{view}_{dt.datetime.now():%Y%m%d-%H%M%S}.{fmt}"
        with db() as con:
            WB.export_one(con, view, path, fmt, f)
        return FileResponse(path, filename=path.name)

    @app.post("/api/integrations/linkedin/login")
    def linkedin_login():
        raise HTTPException(501, "Log in by hand in the harness folder: uv run linkedin_harness.py login")

    @app.post("/api/integrations/llm/test")
    def llm_test():
        """Real inference on Chanakya's main agent route (Qwen Max via Model Studio by default). No other model is
        tried in its place: if the route is not configured, the answer says so."""
        import time
        from chanakya.llm import routing
        from chanakya.workflow.services import make_llm
        r = routing.route("chanakya_agent")
        llm = make_llm(task="chanakya_agent") if r.configured else None
        if llm is None:
            return {"ok": False, "provider": r.provider, "model": r.model, "ms": 0, "error": r.reason}
        t = time.monotonic()
        try:
            out = llm.chat([{"role": "user", "content": "Reply with OK."}], max_tokens=5) if r.provider != "ollama"                 else llm.chat([{"role": "user", "content": "Reply with OK."}])
            return {"ok": True, "provider": r.provider, "model": r.model, "answered_by": getattr(out, "model", None),
                    "ms": int((time.monotonic() - t) * 1000)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": r.provider, "model": r.model, "ms": 0, "error": str(e)[:200]}

    # ---------------- schedules (recurring, unattended workflows) ----------------
    @app.get("/api/schedules")
    def schedules():
        th = app.state.scheduler
        with db() as con:
            return {"schedules": SCH.listing(con), "next_run": SCH.next_run(con),
                    "thread": {"running": bool(th and th.thread.is_alive()), "last_tick": th and th.last_tick,
                               "last_error": th and th.last_error,
                               "note": None if th else "scheduler thread off (CHANAKYA_SCHEDULER=0, simulated or "
                                                       "test services)"}}

    @app.post("/api/schedules", status_code=201)
    def schedule_create(request: Request, body: dict = Body(...)):
        with db() as con:
            params = {**default_params(con), **(body.get("params") or {})}
            try:
                sid = SCH.create(con, name=body.get("name") or f"{body.get('kind', 'top_down')} every "
                                 f"{body.get('every_minutes')} min", kind=body.get("kind", "top_down"), params=params,
                                 every_minutes=int(body.get("every_minutes") or 1440), by=reviewer(request),
                                 start_now=bool(body.get("start_now", True)),
                                 auto_resume=bool(body.get("auto_resume", True)))
            except (ValueError, TypeError) as e:
                raise HTTPException(400, str(e))
        return {"id": sid}

    @app.post("/api/schedules/{sid}/{what}")
    def schedule_toggle(sid: int, what: str, request: Request):
        if what not in ("enable", "disable"):
            raise HTTPException(404, "use enable or disable")
        with db() as con:
            SCH.set_enabled(con, sid, what == "enable", reviewer(request))
        return {"id": sid, "enabled": what == "enable"}

    def _next_run():
        with db() as con:
            return SCH.next_run(con)

    def _run_defaults() -> dict:
        from chanakya.api import workspace as WS
        from chanakya.intel import settings as ST
        with db() as con:
            r = con.execute("SELECT updated_by, updated_at FROM settings WHERE key='run_defaults'").fetchone()
            return {"run_defaults": WS.run_defaults(con), "run_default_ranges": WS.RUN_RANGES,
                    "run_defaults_factory": ST.DEFAULTS["run_defaults"],
                    "run_defaults_updated": {"by": r[0], "at": r[1]} if r else None}

    @app.get("/api/settings")
    def settings():
        return {"clients": [], "sender": {"name": os.getenv("CHANAKYA_SENDER", ""), "company": "Wayam", "email": ""},
                "learned": {}, "setup_complete": True,
                "run": {"target": 0, "pool": 0, "leaders_per_company": 1,
                        "daily_page_limit": int(os.getenv("DAILY_PAGE_LIMIT", "200")), "calm_speed": 1.0,
                        "research_days": 90, "headless": False}, "next_run": _next_run(),
                **_run_defaults()}

    @app.get("/api/metrics/pages")
    def pages():
        from chanakya.workflow.services import make_harness
        h = make_harness()
        try:
            u = json.loads((h.harness_dir / "outputs" / "usage.json").read_text()) if h else {}
        except (OSError, ValueError):
            u = {}
        today = dt.date.today()
        return [{"date": (d := (today - dt.timedelta(days=i)).isoformat()), "pages": int(u.get(d, 0))}
                for i in range(29, -1, -1)]

    @app.get("/api/metrics")
    def metrics(run: str | None = None):
        with db() as con:
            calls = con.execute("SELECT COUNT(*), COALESCE(SUM(prompt_tokens+output_tokens),0) FROM llm_calls")\
                .fetchone()
            return {"outcomes": {"exported": 0, "replied": 0, "meeting": 0, "won": 0, "no_reply": 0},
                    "by_offering": [], "runs": [M.run_obj(con, w, job()) for w in
                                                con.execute("SELECT * FROM workflows ORDER BY id DESC")],
                    "cost": {"llm_calls": calls[0], "llm_tokens": calls[1], "price_per_mtok": 0, "usd": 0}}

    AUDIT = [("human_reviews", "reviewed_at", "reviewer", "'company '||status", "company_id||': '||reason"),
             ("person_role_review_history", "at", "reviewer", "'role '||action", "role_id||': '||reason"),
             ("persona_mapping_history", "at", "reviewer", "'persona '||action", "mapping_id||': '||reason"),
             ("linkedin_resolution_history", "at", "reviewer", "'linkedin '||action",
              "resolution_id||': '||COALESCE(url,'')||' '||reason"),
             ("email_record_history", "at", "reviewer", "'email '||action",
              "email_record_id||': '||COALESCE(email,'')||' '||reason"),
             ("profile_research_history", "at", "reviewer", "'research '||action", "research_id||': '||reason"),
             ("prospect_history", "at", "reviewer", "'prospect '||action", "prospect_id||': '||reason"),
             ("outreach_draft_history", "at", "reviewer", "'draft '||action", "draft_id||': '||reason")]

    @app.get("/api/audit")
    def audit():
        with db() as con:
            rows = []
            for t, ts, actor, action, detail in AUDIT:
                rows += [dict(r) for r in con.execute(f"SELECT {ts} ts, {actor} actor, {action} action, {detail} detail "
                                                      f"FROM {t} ORDER BY id DESC LIMIT 200")]
            rows.sort(key=lambda r: r["ts"] or "", reverse=True)
            return {"rows": [{"id": i + 1, **r} for i, r in enumerate(rows[:300])]}

    @app.get("/api/search")
    def search(q: str = ""):
        like = f"%{q.strip()}%"
        with db() as con:
            cs = [{"id": r["id"], "name": r["name"] or r["domain"], "industry": M.j(r["data_json"], {}).get("industry")}
                  for r in con.execute("SELECT * FROM companies WHERE name LIKE ? OR domain LIKE ? LIMIT 10",
                                       (like, like))]
            ls = [{"id": r["id"], "name": r["full_name"], "title": r["title"], "company": r["cname"]}
                  for r in con.execute("SELECT p.id, p.full_name, r.title, c.name cname FROM people p LEFT JOIN "
                                       "person_roles r ON r.person_id=p.id LEFT JOIN companies c ON c.id=r.company_id "
                                       "WHERE p.full_name LIKE ? GROUP BY p.id LIMIT 10", (like,))]
            return {"companies": cs, "leaders": ls}

    @app.get("/api/offerings")
    def offerings():
        with db() as con:
            return {"rows": [{"id": i + 1, "slug": r["id"], "name": r["name"], "kind": r["status"],
                              "description": r["summary"], "industries": M.j(r["target_industries_json"], []),
                              "buyer_roles": M.j(r["buyer_roles_json"], []), "keywords": [], "proof": None,
                              "active": 1, "evidence": None, "source": r["source_path"]}
                             for i, r in enumerate(con.execute("SELECT * FROM products ORDER BY id"))]}

    return app


def M_now() -> str:
    from chanakya.storage.db import now
    return now()
