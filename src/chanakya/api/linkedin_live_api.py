"""HTTP control of the visible LinkedIn Chrome session (chanakya.linkedin.live). Starting it is the signed-in user's
recorded decision to use their own LinkedIn account in a visible browser; it never bypasses a login or CAPTCHA."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response

from chanakya.api import auth
from chanakya.api.db import db
from chanakya.linkedin import live as L

router = APIRouter(prefix="/api/linkedin/live")


def _s():
    return L.session(db)


@router.get("")
def status():
    return _s().status()


@router.post("/start")
def start(request: Request):
    who = auth.current_user(request)["name"]
    with db() as con:
        from chanakya.intel import settings as S
        S.put(con, "linkedin_automation", "enabled", by=who)  # recorded decision, shown in Integrations
        con.commit()
    return _s().start(who)


@router.post("/search")
def search(request: Request, body: dict = Body(...)):
    who = auth.current_user(request)["name"]
    parts = [body.get("role"), body.get("company"), body.get("industry"), body.get("query")]
    q = " ".join(p.strip() for p in parts if p and p.strip())
    if not q:
        raise HTTPException(400, "give a query, role, company or industry")
    s = _s()
    if not (s.thread and s.thread.is_alive()):
        raise HTTPException(409, "start the browser first")
    return s.enqueue(q, pages=int(body.get("pages") or 2), target=body.get("company"),
                     workflow_id=body.get("workflow_id"), by=who)


@router.post("/cancel")
def cancel():
    _s().cancel_search()
    return _s().status()


@router.post("/stop")
def stop():
    _s().stop()
    return _s().status()


@router.get("/screenshot.jpg")
def screenshot():
    shot = _s().shot
    if not shot:
        raise HTTPException(404, "no browser view yet")
    return Response(shot, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/accelerators")
def accelerators():
    with db() as con:
        return {"accelerators": [dict(r) for r in con.execute("SELECT id, name FROM products ORDER BY name")]}


def _plan(product_id: str):
    from chanakya.linkedin import planner as P
    from chanakya.workflow.services import make_llm
    with db() as con:
        try:
            llm = make_llm(task="accelerator_matching")
        except Exception:  # noqa: BLE001 - a broken model config falls back to the rule plan, which says so
            llm = None
        try:
            return P.plan(con, product_id, llm)
        except KeyError:
            raise HTTPException(404, "unknown accelerator")
        except Exception as e:  # noqa: BLE001 - model failed: rule plan, reported
            p = P.plan(con, product_id, None)
            p["source"] += f" (model failed: {type(e).__name__})"
            return p


@router.post("/plan")
def plan(body: dict = Body(...)):
    return _plan(body.get("product_id") or "")


@router.post("/accelerator-search")
def accelerator_search(request: Request, body: dict = Body(...)):
    who = auth.current_user(request)["name"]
    s = _s()
    if not (s.thread and s.thread.is_alive()):
        raise HTTPException(409, "open LinkedIn in Chrome first")
    p = body.get("plan") or _plan(body.get("product_id") or "")
    return {"plan": p, "status": s.enqueue_accelerator(p, company_pages=body.get("company_pages") or 1,
                                                        max_companies=body.get("max_companies") or 5,
                                                        titles_per_company=body.get("titles_per_company") or 2, by=who,
                                                        user_id=auth.current_user(request)["id"])}


@router.post("/save-drafts")
def save_drafts(request: Request):
    """Save every LinkedIn-sourced draft not yet in Outlook (e.g. after connecting Outlook). Nothing is sent."""
    from chanakya.linkedin import pipeline as PL
    u = auth.current_user(request)
    out = []
    with db() as con:
        for r in con.execute("SELECT id FROM outreach_drafts WHERE outlook_message_id IS NULL AND status IN "
                             "('generated','failed','approved','ready','edited') AND hooks_json LIKE "
                             "'%linkedin_live%'").fetchall():
            out.append({"draft_id": r[0], **PL.save_to_outlook(con, r[0], u["name"], user_id=u["id"])})
    return {"results": out, "saved": sum(1 for x in out if x.get("saved"))}


def _content_label(settings: dict) -> str:
    """Run summary line for authority content: a run-specific list, or master-sheet use cases per accelerator."""
    c = settings.get("authority_content") or []
    if isinstance(c, dict):
        text = " | ".join(f"{acc}: " + "; ".join(t) for acc, t in c.items())
    else:
        text = "; ".join(c)
    src = settings.get("authority_content_source")
    return (f"{text} ({src})" if src else text) if text else "none"


@router.post("/multi-search")
def multi_search(request: Request, body: dict = Body(...)):
    """Several (or all) accelerators in one run: plans in parallel, merged company searches, every company scored
    against all selected accelerators, drafts pitch each company's best match."""
    from concurrent.futures import ThreadPoolExecutor
    who = auth.current_user(request)
    ids = body.get("product_ids") or []
    if body.get("all"):
        with db() as con:
            ids = [r[0] for r in con.execute("SELECT id FROM products WHERE COALESCE(status,'active')='active' ORDER BY name")]
    if not ids:
        raise HTTPException(400, "choose at least one accelerator")
    s = _s()
    if not (s.thread and s.thread.is_alive()):
        raise HTTPException(409, "open LinkedIn in Chrome first")
    with ThreadPoolExecutor(max_workers=6) as ex:
        plans = list(ex.map(_plan, ids))
    extras = {k: body[k] for k in ("roles", "geography", "revenue", "company_queries", "company_sizes",
                                   "authority_content") if body.get(k)}  # optional run settings
    if len(plans) == 1:
        one = {**plans[0], **{k: v for k, v in extras.items() if k != "company_queries"}}  # plan keeps its own query dicts
        return {"plans": plans, "status": s.enqueue_accelerator(one, max_companies=body.get("max_companies") or 5,
                                                                titles_per_company=1, by=who["name"], user_id=who["id"],
                                                                extras=extras)}
    return {"plans": plans, "status": s.enqueue_multi(plans, max_companies=body.get("max_companies") or 8,
                                                      by=who["name"], user_id=who["id"], extras=extras)}


@router.get("/companies")
def company_candidates(product_id: str | None = None):
    from chanakya.linkedin.live import COMPANY_TABLE
    with db() as con:
        con.execute(COMPANY_TABLE)
        q = "SELECT * FROM linkedin_company_candidates" + (" WHERE product_id=?" if product_id else "") + " ORDER BY id DESC"
        return {"rows": [dict(r) for r in con.execute(q, (product_id,) if product_id else ())]}


@router.post("/companies/{cid}")
def review_company(cid: int, request: Request, body: dict = Body(...)):
    """approve: the company enters the shared companies table (origin bottom_up, source linkedin_live) for research;
    reject: kept with the reason so it is not proposed again."""
    from chanakya.storage.db import now
    who = auth.current_user(request)["name"]
    action, reason = body.get("action"), (body.get("reason") or "").strip()
    if action not in ("approve", "reject") or not reason:
        raise HTTPException(400, "action approve|reject and a reason are required")
    with db() as con:
        r = con.execute("SELECT * FROM linkedin_company_candidates WHERE id=?", (cid,)).fetchone()
        if r is None:
            raise HTTPException(404, "unknown candidate")
        company_id = None
        if action == "approve":
            from chanakya.intel import entities as E
            company_id = E.resolve_company(con, name=r["name"], domain=body.get("domain"), linkedin_url=r["linkedin_url"], direction=E.BOTTOM,
                                           source="linkedin_live")["company_id"]
        con.execute("UPDATE linkedin_company_candidates SET status=?, reviewed_by=?, review_reason=?, updated_at=? "
                    "WHERE id=?", ("approved" if action == "approve" else "rejected", who, reason, now(), cid))
        con.commit()
    return {"id": cid, "status": action + "d", "company_id": company_id}


@router.post("/followup")
def followup(request: Request, body: dict = Body(...)):
    """Finish the follow-up for LinkedIn-found people: email + personalised CEO/CFO draft for anyone without one, then
    save to Outlook. Runs in the background; progress appears in the activity log."""
    import threading
    u = auth.current_user(request)
    pid_ = body.get("product_id")
    if not pid_:
        raise HTTPException(400, "choose the accelerator the drafts are for")
    with db() as con:
        ids = [r[0] for r in con.execute(
            "SELECT DISTINCT ps.person_id FROM person_sources ps WHERE ps.tool='visible-chrome' AND NOT EXISTS (SELECT 1 "
            "FROM outreach_drafts d WHERE d.person_id=ps.person_id AND (d.outlook_message_id IS NOT NULL OR d.status IN "
            "('needs_editing','rejected','discarded'))) ORDER BY ps.person_id")]
    s = _s()
    threading.Thread(target=s._followup, args=(ids, {"product_id": pid_}, u["name"], u["id"]), daemon=True).start()
    return {"people": len(ids)}


def _run(con, run_id):
    from chanakya.linkedin.live import ensure_run_table
    ensure_run_table(con)
    q = "SELECT * FROM linkedin_runs " + ("WHERE id=?" if run_id != "latest" else "ORDER BY id DESC LIMIT 1")
    r = con.execute(q, (int(run_id),) if run_id != "latest" else ()).fetchone()
    if r is None:
        raise HTTPException(404, "no LinkedIn run yet")
    return r


@router.get("/runs")
def runs():
    from chanakya.linkedin.live import ensure_run_table
    with db() as con:
        ensure_run_table(con)
        return {"runs": [dict(r) for r in con.execute("SELECT id, accelerator, status, started_at, finished_at, "
                                                       "company_ids_json, person_ids_json FROM linkedin_runs ORDER BY id DESC LIMIT 20")]}


@router.get("/run/{run_id}.xlsx")
def run_workbook(run_id: str):
    """One run, complete: every company found (researched or not), every person, emails, drafts."""
    import datetime as dt
    import json
    from fastapi.responses import FileResponse
    from openpyxl import Workbook
    from chanakya.outreach.workbook import _sheet
    from chanakya.api.app import EXPORTS
    with db() as con:
        r = _run(con, run_id)
        cids = json.loads(r["company_ids_json"] or "[]")
        pids = json.loads(r["person_ids_json"] or "[]")
        found = json.loads(r["companies_found_json"] or "[]")
        wb = Workbook()
        ws = wb.active
        ws.title = "Summary"
        ph = ",".join("?" * len(pids)) or "NULL"
        nd = con.execute(f"SELECT COUNT(*) FROM outreach_drafts WHERE person_id IN ({ph})", pids).fetchone()[0]
        ne = con.execute(f"SELECT COUNT(DISTINCT person_id) FROM email_records WHERE email IS NOT NULL AND person_id IN "
                         f"({ph})", pids).fetchone()[0]
        for row in [("Run", r["id"]), ("Accelerator", r["accelerator"]), ("Status", r["status"]),
                    ("Started", r["started_at"]), ("Finished", r["finished_at"]), ("Companies found", len(found)),
                    ("Companies researched", len(cids)), ("People", len(pids)), ("People with email", ne),
                    ("Drafts", nd), ("Exported at", dt.datetime.now().isoformat(timespec="seconds"))]:
            ws.append(list(row))
        ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 26, 40
        comp_rows = []
        for f in found:
            c = con.execute("SELECT c.id, c.name, c.domain, c.data_json FROM companies c WHERE "
                            "json_extract(c.data_json,'$.linkedin_url')=? LIMIT 1", (f["linkedin_url"],)).fetchone()
            d = json.loads(c["data_json"] or "{}") if c else {}
            n = con.execute(f"SELECT COUNT(DISTINCT r.person_id) FROM person_roles r WHERE r.company_id=? AND "
                            f"r.person_id IN ({ph})", [c["id"], *pids]).fetchone()[0] if c else 0
            buyers = con.execute(f"""SELECT p.full_name, MAX(r.title) title, p.linkedin_url,
                (SELECT e.email FROM email_records e WHERE e.person_id=p.id ORDER BY e.id DESC LIMIT 1) email,
                (SELECT e.status FROM email_records e WHERE e.person_id=p.id ORDER BY e.id DESC LIMIT 1) est
                FROM people p JOIN person_roles r ON r.person_id=p.id WHERE r.company_id=? AND p.id IN ({ph})
                GROUP BY p.id ORDER BY p.full_name""", [c["id"], *pids]).fetchall() if c else []
            # one line per person, same order in every column; numbered 1. 2. ... when a company has several
            col = lambda fn: "\n".join((f"{i}. " if len(buyers) > 1 else "") + fn(b) for i, b in enumerate(buyers, 1))
            comp_rows.append((f["name"], f["linkedin_url"], (f.get("lines") or [""])[0], (f.get("lines") or ["", ""])[1:2][0]
                              if len(f.get("lines") or []) > 1 else "", d.get("website") or (c["domain"] if c else ""),
                              d.get("description") or f.get("about") or "",
                              f.get("accelerator"), "; ".join(f"{m['accelerator']} {m['match_percent']}%" for m in
                                                              (f.get("matches") or [])[1:]),
                              f.get("match_percent"), f.get("match_why"), f.get("buy_why"),
                              "; ".join(f.get("buy_sources") or []),
                              "yes" if f.get("researched") else "no (beyond the company limit, not scored)",
                              n, col(lambda b: b["full_name"]), col(lambda b: b["title"] or "not found"),
                              col(lambda b: b["linkedin_url"] or "not found"),
                              col(lambda b: f"{b['email']} ({b['est']})" if b["email"] else "not found"),
                              f.get("query"), f.get("hq") or "",
                              {True: "yes", False: "no"}.get(f.get("in_geography"), ""),
                              f.get("filtered") or f.get("location_note") or "",
                              f.get("content_score"), f.get("content_best") or "", f.get("content_why") or "",
                              f.get("revenue_cr"), f.get("revenue_fy") or "", f.get("revenue_source") or "",
                              {True: "yes", False: "no"}.get(f.get("in_revenue_band"), "")))
        comp_rows.sort(key=lambda x: -(x[8] if isinstance(x[8], int) else -1))
        _sheet(wb, "Companies", ["Company", "LinkedIn", "Industry", "Location", "Website", "What they do",
                                 "Best accelerator", "Other matches", "Match %", "Why this match", "Why likely to buy",
                                 "Buying-signal sources", "Researched", "Leaders found", "Buyer name", "Buyer role",
                                 "Buyer LinkedIn", "Buyer email (status)", "Found by search",
                                 "HQ", "In run location", "Location note", "Content fit (0-10)", "Best authority content",
                                 "Why this content", "Revenue (₹ Cr)", "Revenue year", "Revenue source",
                                 "In revenue band"], comp_rows)
        settings = json.loads(r["settings_json"] or "{}") if "settings_json" in r.keys() else {}
        if settings:
            band = settings.get("revenue") or {}
            for row in [("Location", settings.get("geography") or "none"),
                        ("Revenue band", f"₹{band.get('min_cr') or 0:,.0f} Cr - ₹{band.get('max_cr') or 0:,.0f} Cr"
                         if band else "none"),
                        ("Buyer roles", ", ".join(settings.get("roles") or []) or "CEO, CFO only"),
                        ("Authority content", _content_label(settings)),
                        ("Company searches", "; ".join(settings.get("company_queries") or []) or "from the plan"),
                        ("Company size (LinkedIn codes)", settings.get("company_sizes") or "51+ employees")]:
                ws.append(list(row))
        # companies per accelerator: researched, kept (not filtered out by location/revenue), by best match
        pids_sel = [p for p in (r["product_id"] or "").split(",") if p]
        names = [n[0] for p in pids_sel for n in con.execute("SELECT name FROM products WHERE id=?", (p,))]
        kept = [f for f in found if f.get("researched") and not f.get("filtered")]
        ws.append([])
        ws.append(["Companies per accelerator", "(researched and kept, by best match)"])
        for nm in names or sorted({f.get("accelerator") for f in kept if f.get("accelerator")}):
            ws.append([f"  {nm}", sum(1 for f in kept if f.get("accelerator") == nm)])
        ws.append(["  Not scored", sum(1 for f in kept if not f.get("accelerator"))])
        ws.append(["  Filtered out (location / revenue)", sum(1 for f in found if f.get("researched") and f.get("filtered"))])
        events = json.loads(r["events_json"] or "[]") if "events_json" in r.keys() else []
        if events:
            _sheet(wb, "Events", ["Event", "Date", "City", "Country", "Organiser", "Kind", "Who attends",
                                  "Why it matters", "Link"],
                   [(e.get("name"), e.get("date"), e.get("city"), e.get("country"), e.get("organiser"), e.get("kind"),
                     e.get("audience"), e.get("why"), e.get("url")) for e in events])
        cols = ["Company", "Name", "Designation", "LinkedIn profile", "Source", "Verification", "Email",
                "Email status", "Draft subject", "Draft body", "Draft status", "In Outlook"]
        prow = con.execute(f"""SELECT c.name, p.full_name, r.title, p.linkedin_url,
            (SELECT ps.query FROM person_sources ps WHERE ps.person_id=p.id ORDER BY ps.id DESC LIMIT 1), p.verification,
            (SELECT e.email FROM email_records e WHERE e.person_id=p.id ORDER BY e.id DESC LIMIT 1),
            (SELECT e.status||' ('||e.method||')' FROM email_records e WHERE e.person_id=p.id ORDER BY e.id DESC LIMIT 1),
            d.subject, d.body, d.status, CASE WHEN d.outlook_message_id IS NOT NULL THEN 'yes' ELSE 'no' END
            FROM people p JOIN person_roles r ON r.person_id=p.id JOIN companies c ON c.id=r.company_id
            LEFT JOIN outreach_drafts d ON d.id=(SELECT MAX(id) FROM outreach_drafts x WHERE x.person_id=p.id)
            WHERE p.id IN ({ph}) GROUP BY p.id ORDER BY c.name, p.full_name""", pids).fetchall()
        _sheet(wb, "People", cols, [tuple(x) for x in prow])
    out = EXPORTS / f"chanakya_linkedin_run{r['id']}_{dt.datetime.now():%Y%m%d-%H%M%S}.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return FileResponse(out, filename=out.name)


@router.get("/run/{run_id}/events")
def run_events(run_id: str):
    import json
    with db() as con:
        r = _run(con, run_id)
        return {"run_id": r["id"], "events": json.loads(r["events_json"] or "[]")}


@router.get("/run/{run_id}/companies")
def run_companies(run_id: str):
    import json
    with db() as con:
        r = _run(con, run_id)
        rows = json.loads(r["companies_found_json"] or "[]")
    rows.sort(key=lambda x: -(x.get("match_percent") if isinstance(x.get("match_percent"), int) else -1))
    return {"run_id": r["id"], "companies": rows}


@router.post("/run/{run_id}/followup")
def run_followup(run_id: str, request: Request):
    """Resume a run's follow-up (emails + CEO/CFO drafts) for its people that have no draft yet."""
    import json
    import threading
    u = auth.current_user(request)
    with db() as con:
        r = _run(con, run_id)
        pids = [p for p in json.loads(r["person_ids_json"] or "[]") if not con.execute(
            "SELECT 1 FROM outreach_drafts WHERE person_id=?", (p,)).fetchone()]
        con.execute("UPDATE linkedin_runs SET status='writing_drafts' WHERE id=?", (r["id"],))
        con.commit()
    s = _s()
    threading.Thread(target=s._followup, args=(pids, {"product_id": r["product_id"]}, u["name"], u["id"], r["id"]),
                     daemon=True).start()
    return {"run_id": r["id"], "people": len(pids)}


@router.post("/run/{run_id}/outlook")
def run_outlook(run_id: str, request: Request):
    """Save every CEO/CFO draft of the run to Outlook Drafts (never sent). Returns where to open them."""
    import json
    from chanakya.linkedin import pipeline as PL
    u = auth.current_user(request)
    results = []
    with db() as con:
        r = _run(con, run_id)
        pids = json.loads(r["person_ids_json"] or "[]")
        ph = ",".join("?" * len(pids)) or "NULL"
        for d in con.execute(f"SELECT id, status, outlook_message_id FROM outreach_drafts WHERE person_id IN ({ph}) "
                             f"AND status NOT IN ('rejected','discarded','needs_editing')", pids).fetchall():
            if d["outlook_message_id"]:
                results.append({"draft_id": d["id"], "saved": True, "already": True})
                continue
            if results and "could not reach this mailbox" in (results[-1].get("error") or ""):
                results.append({"draft_id": d["id"], "saved": False, "error": "skipped: mailbox unreachable (see above)"})
                continue
            try:
                results.append({"draft_id": d["id"], **PL.save_to_outlook(con, d["id"], u["name"], user_id=u["id"])})
            except Exception as e:  # noqa: BLE001 - one failure is reported, the others still run
                results.append({"draft_id": d["id"], "saved": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})
    ok = [x for x in results if x.get("saved")]
    return {"results": results, "saved": len(ok), "failed": len(results) - len(ok),
            "open_url": "https://outlook.office.com/mail/drafts"}


@router.get("/leaders")
def leaders():
    """Everyone the Discover runs found: name, role, company, email, LinkedIn, best-fit accelerator(s). Newest first;
    every run adds its people here."""
    import json
    with db() as con:
        rows = con.execute("""SELECT p.id, p.full_name, p.linkedin_url, p.verification, r.title, c.id company_id, c.name company,
            c.domain, c.data_json, MIN(ps.fetched_at) found_at,
            (SELECT e.email FROM email_records e WHERE e.person_id=p.id ORDER BY e.id DESC LIMIT 1) email,
            (SELECT e.status FROM email_records e WHERE e.person_id=p.id ORDER BY e.id DESC LIMIT 1) email_status,
            (SELECT e.method FROM email_records e WHERE e.person_id=p.id ORDER BY e.id DESC LIMIT 1) email_method
            FROM person_sources ps JOIN people p ON p.id=ps.person_id JOIN person_roles r ON r.person_id=p.id
            AND r.company_id=ps.company_id JOIN companies c ON c.id=ps.company_id
            WHERE ps.tool='visible-chrome' GROUP BY p.id ORDER BY found_at DESC, p.id DESC""").fetchall()
        names = {x[0]: x[1] for x in con.execute("SELECT id, name FROM products")}
    out = []
    for r in rows:
        fit = (json.loads(r["data_json"] or "{}").get("linkedin_fit") or {})
        if not fit.get("accelerator") and fit.get("product_id") in names:
            fit["accelerator"] = names[fit["product_id"]]
        matches = fit.get("matches") or ([{"accelerator": fit.get("accelerator"), "match_percent": fit.get("match_percent")}]
                                         if fit.get("accelerator") else [])
        out.append({"id": r["id"], "name": r["full_name"], "role": r["title"], "company": r["company"],
                    "company_id": r["company_id"], "website": r["domain"], "email": r["email"],
                    "email_status": r["email_status"], "email_method": r["email_method"],
                    "linkedin_url": r["linkedin_url"], "best_fit": matches[:3], "match_percent": fit.get("match_percent"),
                    "buy_percent": fit.get("buy_percent"), "found_at": r["found_at"],
                    "employer_confirmed": "not named" not in (r["verification"] or "")})
    return {"rows": out}
