"""Workspace actions behind the original UI controls: user administration, profile, manual company / leader entry,
re-research, run defaults, notification read state and catalogue sync. Each one does real work on the shared tables
with the signed-in user recorded; none of them invents data or bypasses a review gate."""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Body, HTTPException, Request

from chanakya.api import auth
from chanakya.api import mapping as M
from chanakya.api.db import db
from chanakya.intel import entities as E
from chanakya.intel import settings as ST
from chanakya.storage.db import now

router = APIRouter(prefix="/api")
ROLES = ("owner", "member")
RUN_RANGES = {"company_signal_limit": (1, 25), "classify_limit": (0, 25), "brief_limit": (0, 10),
              "site_people_limit": (0, 10), "tender_days": (7, 365), "job_days": (7, 180),
              "auto_resolve_limit": (0, 50)}


def _owner(request: Request) -> dict:
    u = auth.current_user(request)
    if u.get("role") != "owner":
        raise HTTPException(403, "Only an owner can manage users.")
    return u


def _active_owners(con, *, excluding: int | None = None) -> int:
    return con.execute("SELECT COUNT(*) FROM ui_users WHERE role='owner' AND disabled=0 AND id IS NOT ?",
                       (excluding,)).fetchone()[0]


# ---------------- users ----------------
@router.get("/users")
def users(request: Request):
    _owner(request)
    with db() as con:
        return {"users": [auth.public_user(u) for u in con.execute("SELECT * FROM ui_users ORDER BY id")]}


@router.post("/users", status_code=201)
def add_user(request: Request, body: dict = Body(...)):
    _owner(request)
    name, email = (body.get("name") or "").strip(), (body.get("email") or "").strip().lower()
    role, pw = body.get("role") or "member", body.get("password")
    if not name or "@" not in email:
        raise HTTPException(400, "Name and a valid email are required.")
    if role not in ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(ROLES)}")
    if (p := auth.password_problem(pw)):
        raise HTTPException(400, p)
    with db() as con:
        if con.execute("SELECT 1 FROM ui_users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "A user with this email already exists.")
        uid = con.execute("INSERT INTO ui_users(email,name,role,password_hash,created) VALUES(?,?,?,?,?)",
                          (email, name, role, auth.hash_password(pw), auth._now().isoformat())).lastrowid
        con.commit()
        return {"user": auth.public_user(con.execute("SELECT * FROM ui_users WHERE id=?", (uid,)).fetchone())}


@router.put("/users/{uid}")
def update_user(uid: int, request: Request, body: dict = Body(...)):
    me = _owner(request)
    with db() as con:
        u = con.execute("SELECT * FROM ui_users WHERE id=?", (uid,)).fetchone()
        if u is None:
            raise HTTPException(404, "unknown user")
        role, disabled = body.get("role", u["role"]), body.get("disabled", bool(u["disabled"]))
        if role not in ROLES:
            raise HTTPException(400, f"role must be one of {', '.join(ROLES)}")
        losing_owner = u["role"] == "owner" and not u["disabled"] and (role != "owner" or disabled)
        if losing_owner and _active_owners(con, excluding=uid) == 0:
            raise HTTPException(409, "The workspace needs at least one active owner.")
        if disabled and uid == me["id"]:
            raise HTTPException(409, "You cannot disable your own account.")
        name = (body.get("name") or u["name"]).strip()
        con.execute("UPDATE ui_users SET role=?, disabled=?, name=? WHERE id=?", (role, int(bool(disabled)), name, uid))
        if "password" in body:
            if (p := auth.password_problem(body["password"])):
                raise HTTPException(400, p)
            con.execute("UPDATE ui_users SET password_hash=? WHERE id=?", (auth.hash_password(body["password"]), uid))
        if disabled or "password" in body:  # a disabled or reset account is signed out everywhere
            con.execute("DELETE FROM ui_sessions WHERE user_id=?", (uid,))
        con.commit()
        return {"user": auth.public_user(con.execute("SELECT * FROM ui_users WHERE id=?", (uid,)).fetchone())}


@router.put("/auth/me")
def update_me(request: Request, body: dict = Body(...)):
    me = auth.current_user(request)
    if not me.get("id"):
        raise HTTPException(409, "Sign-in is switched off (CHANAKYA_AUTH=off); there is no profile to edit.")
    name = (body.get("name") or me["name"]).strip()
    email = (body.get("email") or me["email"]).strip().lower()
    if not name or "@" not in email:
        raise HTTPException(400, "Name and a valid email are required.")
    with db() as con:
        if con.execute("SELECT 1 FROM ui_users WHERE email=? AND id!=?", (email, me["id"])).fetchone():
            raise HTTPException(409, "Another user already has this email.")
        con.execute("UPDATE ui_users SET name=?, email=? WHERE id=?", (name, email, me["id"]))
        con.commit()
        return {"user": auth.public_user(con.execute("SELECT * FROM ui_users WHERE id=?", (me["id"],)).fetchone())}


# ---------------- manual company and leader entry ----------------
@router.post("/companies", status_code=201)
def add_company(request: Request, body: dict = Body(...)):
    """Add a company by hand. It goes through the shared entity resolver (so an existing account is matched, not
    duplicated), is recorded as a manual top-down discovery with the user's name, and still needs qualification."""
    from chanakya.intel import scoring as SC
    by = auth.current_user(request)["name"]
    name, domain = (body.get("name") or "").strip(), (body.get("domain") or "").strip() or None
    li = (body.get("linkedin_url") or "").strip() or None
    if not name or not (domain or li):
        raise HTTPException(400, "Give the company name and its website domain or LinkedIn company URL.")
    if domain and not E.norm_domain(domain):
        raise HTTPException(400, "That does not look like a website domain (for example acme.com).")
    with db() as con:
        r = E.resolve_company(con, name=name, domain=domain, linkedin_url=li, direction=E.TOP,
                              source=f"manual:{by}", detail={"added_by": by})
        SC.rescore_company(con, r["company_id"])
        con.commit()
        c = con.execute("SELECT * FROM companies WHERE id=?", (r["company_id"],)).fetchone()
        return {"company": M.company_obj(con, c), "created": r["created"], "matched_by": r["matched_by"]}


@router.post("/companies/{cid}/leaders", status_code=201)
def add_leader(cid: int, request: Request, body: dict = Body(...)):
    """Add a person at a company by hand (name, title, optional LinkedIn profile URL). Stored as a manual import
    with the user's name; the role is `needs_manual_review` until approved on Review > People and roles."""
    from chanakya.bottomup.agent import approved_mappings
    from chanakya.intel import scoring as SC
    from chanakya.people.finder import upsert_person
    by = auth.current_user(request)["name"]
    name, title = (body.get("name") or "").strip(), (body.get("title") or "").strip()
    if not name or not title:
        raise HTTPException(400, "Name and job title are required.")
    li = (body.get("linkedin_url") or "").strip() or None
    if li and "linkedin.com/in/" not in li.lower():
        raise HTTPException(400, "The LinkedIn URL must be a profile link (linkedin.com/in/...).")
    with db() as con:
        if con.execute("SELECT 1 FROM companies WHERE id=?", (cid,)).fetchone() is None:
            raise HTTPException(404, "unknown company")
        rec = {"full_name": name, "job_title": title, "linkedin_url": li, "added_by": by,
               "evidence": (body.get("evidence") or "").strip() or None}
        pid, created = upsert_person(con, rec, cid, mappings=approved_mappings(con), run_id=None,
                                     raw_response_id=None, tool="manual-entry", task_id=None, query=f"added by {by}",
                                     source="manual-import")
        SC.rescore_company(con, cid)
        con.commit()
        return {"leader": M.leader_obj(con, pid), "created": created}


@router.post("/leaders/{pid}/research")
def leader_research(pid: int, request: Request):
    """'Research again': re-reads the person's company website for dated evidence (robots.txt respected) and rescores
    the account. LinkedIn profile research stays off while LinkedIn automation is disabled; the answer says so."""
    from chanakya.emailhunter.checks import default_fetcher
    from chanakya.intel import compliance as C
    from chanakya.intel import research_web as RW
    from chanakya.intel import scoring as SC
    auth.current_user(request)
    with db() as con:
        r = con.execute("SELECT r.company_id, c.identity_status, c.domain FROM person_roles r JOIN companies c ON "
                        "c.id=r.company_id WHERE r.person_id=? ORDER BY r.id DESC LIMIT 1", (pid,)).fetchone()
        if r is None:
            raise HTTPException(404, "this person has no company yet")
        if r["identity_status"] != "resolved":
            raise HTTPException(409, "The company has no confirmed website yet: resolve its domain on the account "
                                     "page first.")
        svc = request.app.state.services_factory(True)
        out = RW.company_web_signals(con, r["company_id"], getattr(svc, "fetcher", None) or default_fetcher())
        SC.rescore_company(con, r["company_id"])
        con.commit()
        sigs = out.get("signals", [])
        return {"company_id": r["company_id"], "domain": r["domain"], "items_found": len(sigs),
                "new_signals": sum(1 for x in sigs if x.get("created")), "pages": out.get("pages", []),
                "linkedin_profile_research": "enabled" if C.linkedin_automation_enabled(con) else
                "not run: LinkedIn automation is off by policy",
                "leader": M.leader_obj(con, pid)}


# ---------------- run defaults (Settings > Run settings) ----------------
def run_defaults(con) -> dict:
    return {**ST.DEFAULTS["run_defaults"], **(ST.get(con, "run_defaults") or {})}


@router.put("/settings")
def save_settings(request: Request, body: dict = Body(...)):
    """Saves the defaults every new run starts with (limits per stage, how far back the external sources look)."""
    by = auth.current_user(request)["name"]
    vals = body.get("run_defaults")
    if not isinstance(vals, dict) or not vals:
        raise HTTPException(400, "Send run_defaults: {" + ", ".join(RUN_RANGES) + "}")
    with db() as con:
        cur = run_defaults(con)
        for k, v in vals.items():
            if k not in RUN_RANGES:
                raise HTTPException(400, f"unknown run default {k!r}")
            lo, hi = RUN_RANGES[k]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise HTTPException(400, f"{k} must be a whole number from {lo} to {hi}")
            cur[k] = v
        ST.put(con, "run_defaults", cur, by=by)
        return {"run_defaults": cur, "ranges": RUN_RANGES, "updated_by": by, "updated_at": now()}


# ---------------- notifications: read state per user ----------------
def read_marker(con, user_id) -> str:
    r = con.execute("SELECT value_json FROM settings WHERE key=?", (f"notifications_read:{user_id}",)).fetchone()
    return json.loads(r[0]) if r else ""


@router.post("/notifications/read")
def notifications_read(request: Request):
    u = auth.current_user(request)
    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM workflows WHERE status IN ('waiting_human','blocked','failed') AND "
                        "updated_at>?", (read_marker(con, u["id"]),)).fetchone()[0]
        con.execute("INSERT INTO settings(key,value_json,updated_by,updated_at) VALUES(?,?,?,?) ON CONFLICT(key) DO "
                    "UPDATE SET value_json=excluded.value_json, updated_by=excluded.updated_by, "
                    "updated_at=excluded.updated_at", (f"notifications_read:{u['id']}", json.dumps(now()), u["name"],
                                                       now()))
        con.commit()
        return {"updated": n}


# ---------------- accelerator catalogue sync ----------------
@router.post("/offerings/import")
def offerings_import(request: Request):
    """Re-reads the catalogue YAML the products came from. Identical file: nothing changes and no model is called.
    Changed file: products are updated and customer needs re-extracted for the changed products (Haiku)."""
    from chanakya.catalog.products import run_catalog_yaml
    from chanakya.workflow.services import make_llm
    auth.current_user(request)
    with db() as con:
        before = {r["id"]: r["source_sha256"] for r in con.execute("SELECT id, source_sha256 FROM products")}
        src = con.execute("SELECT source_path FROM products WHERE source_path IS NOT NULL ORDER BY imported_at DESC "
                          "LIMIT 1").fetchone()
        path = os.getenv("CHANAKYA_CATALOG_PATH") or (src[0] if src else None)
        if not path or not os.path.exists(path):
            raise HTTPException(409, f"The catalogue file is not available ({path or 'no source recorded'}). Set "
                                     "CHANAKYA_CATALOG_PATH to wayam_products.yaml.")
        try:
            run_catalog_yaml(make_llm(task="extraction"), con, path)
        except Exception as e:  # noqa: BLE001 - shown to the user as the real reason
            raise HTTPException(502, f"Catalogue import failed: {type(e).__name__}: {str(e)[:300]}")
        after = {r["id"]: r["source_sha256"] for r in con.execute("SELECT id, source_sha256 FROM products")}
        return {"added": len(set(after) - set(before)), "paused": 0, "total": len(after),
                "updated": sum(1 for k in after if k in before and after[k] != before[k]), "source": path}


# ------------------------------------------------------------------ Clay people search on demand (primary provider)
def _clay_people_used_today(con) -> int:
    return con.execute("SELECT COUNT(*) FROM runs WHERE agent IN ('people_finder','linkedin_resolver_clay') AND "
                       "status IN ('ok','error') AND "
                       "substr(started_at,1,10)=substr(?,1,10)", (now(),)).fetchone()[0]


def clay_people_limit() -> int:
    try:
        return max(0, int(os.getenv("CHANAKYA_CLAY_PEOPLE_SEARCHES_PER_DAY", "3")))
    except ValueError:
        return 0


@router.post("/companies/{cid}/clay/people")
def clay_people(cid: int, request: Request, body: dict = Body(default={})):
    """Two steps. Without `confirm`: returns the exact Clay search-contacts query (built from approved personas) and
    the remaining daily budget; nothing is sent to Clay. With `confirm` equal to that query: runs it live through the
    Clay MCP (workspace and credit checks first) and saves people through the shared dedupe with source clay.
    Live searches per day are capped by CHANAKYA_CLAY_PEOPLE_SEARCHES_PER_DAY (default 3)."""
    import asyncio

    from chanakya.bottomup.agent import approved_mappings
    from chanakya.intel import scoring as SC
    from chanakya.people.finder import (ClayContactsProvider, PeopleGateway, PeopleRequest, build_contact_query,
                                        load_company_refs, run_people_finder)
    auth.current_user(request)
    with db() as con:
        c = con.execute("SELECT domain FROM companies WHERE id=?", (cid,)).fetchone()
        if c is None:
            raise HTTPException(404, "unknown company")
        refs, issues = load_company_refs(con, [c["domain"]])
        if issues:
            raise HTTPException(409, "; ".join(issues))
        # Personas only for the accelerators this account matches (best first), kept small: Clay rejects queries
        # that resolve more than 10 unique values.
        from dataclasses import replace
        pids = [body["product_id"]] if body.get("product_id") else [r[0] for r in con.execute(
            "SELECT product_id FROM opportunities WHERE company_id=? ORDER BY score DESC", (cid,))]
        maps = [m for pid in pids for m in approved_mappings(con, pid)][:2]
        if not maps:
            raise HTTPException(409, "No approved buyer persona for the accelerators this account matches; approve "
                                     "one under Scoring and rules first.")
        maps = [replace(m, titles=m.titles[:3], seniorities=m.seniorities[:2]) for m in maps]
        req = PeopleRequest(refs, maps, limit_per_company=max(1, min(3, int(body.get("limit") or 3))),
                            context={"trigger": "account page"})
        q = build_contact_query(req)
        left = clay_people_limit() - _clay_people_used_today(con)
        if not q.ready:
            raise HTTPException(409, "; ".join(q.issues))
        if body.get("confirm") != q.dsl:
            return {"live": False, "query": q.dsl, "searches_left_today": left,
                    "note": "Nothing was sent to Clay. Send this exact query back as confirm to run it."}
        if left <= 0:
            raise HTTPException(409, f"Daily Clay people-search cap reached ({clay_people_limit()}; "
                                     "CHANAKYA_CLAY_PEOPLE_SEARCHES_PER_DAY).")
        svc = request.app.state.services_factory(True)
        if getattr(svc, "clay_people_factory", None) is None:
            raise HTTPException(409, "Clay is not connected: Integrations > Clay > Connect.")
        ws = os.getenv("CLAY_WORKSPACE_ID", "1461095")

        async def go():
            async with svc.clay_people_factory() as clay:
                return await run_people_finder(ClayContactsProvider(PeopleGateway(clay), ws), req, con,
                                               allow_live=True)
        try:
            r = asyncio.run(go())
        except Exception as e:  # noqa: BLE001 - reauth, credits, rate limit: the real reason is returned
            from chanakya.clay.mcp_client import unwrap
            e = unwrap(e)
            code = 401 if "Reauth" in type(e).__name__ else 429 if "429" in str(e) or "rate" in str(e).lower() else 502
            raise HTTPException(code, f"Clay: {type(e).__name__}: {str(e)[:300]}")
        SC.rescore_company(con, cid)
        con.commit()
        return {"live": True, "run_id": r.run_id, "inserted": r.inserted, "updated": r.updated, "skipped": r.skipped,
                "searches_left_today": left - 1}


# ------------------------------------------------------------------ Companies House officers (UK register)
@router.get("/companies/{cid}/companies-house/search")
def ch_search(cid: int, request: Request):
    from chanakya.intel import companies_house as CH
    auth.current_user(request)
    with db() as con:
        c = con.execute("SELECT name FROM companies WHERE id=?", (cid,)).fetchone()
    if c is None:
        raise HTTPException(404, "unknown company")
    try:
        return {"query": c["name"], "matches": CH.search(c["name"])}
    except CH.CHError as e:
        raise HTTPException(502, str(e))


@router.post("/companies/{cid}/companies-house/officers")
def ch_officers(cid: int, request: Request, body: dict = Body(...)):
    """Import current officers of the chosen registered company (the user picks the company number)."""
    from chanakya.intel import companies_house as CH
    by = auth.current_user(request)["name"]
    with db() as con:
        if con.execute("SELECT 1 FROM companies WHERE id=?", (cid,)).fetchone() is None:
            raise HTTPException(404, "unknown company")
        try:
            return CH.import_officers(con, cid, str(body.get("number") or "").strip(), by=by)
        except CH.CHError as e:
            raise HTTPException(502, str(e))


@router.post("/companies/{cid}/people/acquire")
def acquire_people(cid: int, request: Request, body: dict = Body(default={})):
    """Provider fallback for decision-makers: CHANAKYA_PEOPLE_PROVIDERS in order (default clay,companies_house),
    reporting every provider's outcome. Clay first returns its query for confirmation (nothing spent); Companies
    House imports officers when the company number is given."""
    tried = []
    for p in [x.strip() for x in os.getenv("CHANAKYA_PEOPLE_PROVIDERS", "clay").split(",") if x]:
        try:
            if p == "clay":
                r = clay_people(cid, request, body)
                if not r["live"] or r["inserted"] or r["updated"]:
                    return {"provider": "clay", "tried": tried, **r}
                tried.append({"provider": p, "result": "no matches"})
            elif p == "companies_house":
                if body.get("number"):
                    return {"provider": p, "tried": tried, **ch_officers(cid, request, body)}
                r = ch_search(cid, request)
                return {"provider": p, "tried": tried, "pick_company_number": r["matches"]}
        except HTTPException as e:
            tried.append({"provider": p, "error": e.detail})
    raise HTTPException(502, {"message": "No people provider answered", "tried": tried})


def clay_email_total() -> int:
    try:
        return max(0, int(os.getenv("CHANAKYA_CLAY_EMAIL_LOOKUPS_TOTAL", "3")))
    except ValueError:
        return 0


@router.post("/leaders/{pid}/clay/email")
def clay_email(pid: int, request: Request, body: dict = Body(...)):
    """PAID Clay enrichment for ONE approved person: work email via add-contact-data-points on the Clay search that
    found them. Free methods (company website, pattern + MX) run first; Clay is used only if they leave the email
    unverified. Needs a reason; total lookups are capped by CHANAKYA_CLAY_EMAIL_LOOKUPS_TOTAL (default 3)."""
    from chanakya.clay import policy
    from chanakya.clay.mcp_client import ClayMCPClient, unwrap
    from chanakya.emailhunter.agent import run_email_hunter
    from chanakya.emailhunter.clay import PaidEmailLookup, SpendApproval
    by = auth.current_user(request)["name"]
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "A reason is required for a paid Clay lookup.")
    with db() as con:
        used = con.execute("SELECT COUNT(*) FROM raw_responses WHERE provider='clay-email' AND "
                           "tool='add-contact-data-points'").fetchone()[0]
        if used >= clay_email_total():
            raise HTTPException(409, f"Clay email lookup cap reached ({used}/{clay_email_total()}; "
                                     "CHANAKYA_CLAY_EMAIL_LOOKUPS_TOTAL).")
        lookup = PaidEmailLookup(lambda: ClayMCPClient(allowed_tools=set(policy.EMAIL_HUNTER | policy.EMAIL_HUNTER_PAID)),
                                 SpendApproval(by, reason, 1), os.getenv("CLAY_WORKSPACE_ID", "1461095"))
        try:
            rep = run_email_hunter(con, allow_live=True, limit=1, person_ids=[pid], paid=lookup)
        except Exception as e:  # noqa: BLE001
            e = unwrap(e)
            raise HTTPException(401 if "Reauth" in type(e).__name__ else 502, f"{type(e).__name__}: {str(e)[:300]}")
        if not rep.planned:
            raise HTTPException(409, "Not eligible: the person's role must be approved, the company qualified by a "
                                     "human, and no email already pending or approved.")
        e = con.execute("SELECT email, method, status, confidence FROM email_records WHERE person_id=? ORDER BY id "
                        "DESC LIMIT 1", (pid,)).fetchone()
        return {"paid_used": rep.paid_used, "counts": rep.counts, "email": dict(e) if e else None,
                "lookups_left": clay_email_total() - used - rep.paid_used}


@router.post("/leaders/{pid}/clay/email/refresh")
def clay_email_refresh(pid: int, request: Request):
    """FREE: re-read Clay's task context for a lookup already paid for (Clay enriches asynchronously) and update the
    person's email record. No credits are used (get-task-context only)."""
    import asyncio
    import json as _j

    from chanakya.clay import policy
    from chanakya.clay.mcp_client import ClayMCPClient, unwrap
    from chanakya.emailhunter import clay as EC
    by = auth.current_user(request)["name"]
    with db() as con:
        src = con.execute("SELECT task_id, raw_json FROM person_sources WHERE person_id=? AND tool='search-contacts' "
                          "AND task_id IS NOT NULL ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        rec = con.execute("SELECT id, company_id, status FROM email_records WHERE person_id=? AND method='clay' "
                          "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        if src is None or rec is None:
            raise HTTPException(409, "No paid Clay email lookup for this person yet.")
        raw = _j.loads(src["raw_json"] or "{}")
        ent = str(raw.get("entityId") or raw.get("profile_id") or "")

        class _GW:
            def __init__(self, c):
                self.c = c

            async def call(self, name, args):
                return EC.result_payload(await self.c.call_tool(name, args))

        async def go():
            async with ClayMCPClient(allowed_tools=set(policy.EMAIL_HUNTER)) as c:
                return await EC.poll_context(_GW(c), src["task_id"], ent, tries=3, wait=3)
        try:
            payloads = asyncio.run(go())
        except Exception as e:  # noqa: BLE001
            e = unwrap(e)
            raise HTTPException(401 if "Reauth" in type(e).__name__ else 502, f"{type(e).__name__}: {str(e)[:300]}")
        p = payloads[-1][1]
        con.execute("INSERT INTO raw_responses(provider,tool,task_id,query,response_json,fetched_at,run_id,sequence,"
                    "phase) VALUES(?,?,?,?,?,?,?,?,?)", ("clay-email", "get-task-context", src["task_id"],
                                                         f"entity:{ent}", _j.dumps(p, default=str), now(), None, 0,
                                                         "refresh"))
        email, st = EC.find_email_for(p, ent)
        state = EC.enrichment_state(p, ent)
        if email:
            import re as _re
            ok = bool(st and _re.search(r"\b(valid|verified|deliverable)\b", st.lower()))
            con.execute("UPDATE email_records SET email=?, status=?, confidence=?, review_status='pending_review' "
                        "WHERE id=?", (email, "valid" if ok else "unverified", 0.8 if ok else 0.5, rec["id"]))
            con.execute("INSERT INTO email_record_history(email_record_id,action,from_status,to_status,email,reviewer,reason,at) "
                        "VALUES(?,?,?,?,?,?,?,?)", (rec["id"], "clay_refresh", rec["status"],
                                                    "valid" if ok else "unverified", email, by,
                                                    f"Clay enrichment state {state}", now()))
        con.commit()
        return {"clay_state": state, "email": email, "stated_status": st, "credits_used": 0}


# ------------------------------------------------------------------ Bottom-up (person-first) through Clay
@router.post("/bottomup/clay")
def bottomup_clay(request: Request, body: dict = Body(...)):
    """Person-first search: an accelerator's approved buyer roles across a pool of up to 10 candidate employers
    (Clay's search-contacts always needs employers). The pool is the given domains, or the accelerator's best-scoring
    companies with a confirmed website that are NOT yet human-qualified (people lead the qualification). Same two-step
    preview/confirm and daily cap as the account search. Everyone found is recorded as discovered bottom_up, and so
    is their company, so one record keeps both origins."""
    import asyncio
    from dataclasses import replace

    from chanakya.bottomup.agent import approved_mappings
    from chanakya.intel import scoring as SC
    from chanakya.people import CompanyRef
    from chanakya.people.finder import (ClayContactsProvider, PeopleGateway, PeopleRequest, build_contact_query,
                                        run_people_finder)
    auth.current_user(request)
    pid = (body.get("product_id") or "").strip()
    if not pid:
        raise HTTPException(400, "product_id (accelerator) is required")
    with db() as con:
        # titles_only: drop the seniority filter (Clay's seniority labels are stricter than job titles)
        maps = [replace(m, titles=m.titles[:3], seniorities=[] if body.get("titles_only") else m.seniorities[:2])
                for m in approved_mappings(con, pid)][:1]
        if not maps:
            raise HTTPException(409, f"No approved buyer persona for {pid}; approve one under Scoring and rules.")
        if body.get("domains"):
            rows = [con.execute("SELECT id, domain, name FROM companies WHERE domain=?", (d,)).fetchone()
                    for d in body["domains"][:10]]
            rows = [r for r in rows if r]
        else:
            rows = con.execute(
                "SELECT c.id, c.domain, c.name FROM companies c JOIN opportunities o ON o.company_id=c.id WHERE "
                "o.product_id=? AND c.identity_status='resolved' AND COALESCE(c.qualification_status,'')!='qualified' "
                "ORDER BY o.score DESC LIMIT 10", (pid,)).fetchall()
        if not rows:
            raise HTTPException(409, "No candidate employers with a confirmed website for this accelerator.")
        refs = [CompanyRef(r["id"], r["domain"], r["name"]) for r in rows]
        req = PeopleRequest(refs, maps, limit_per_company=1, context={"trigger": "bottom-up", "product_id": pid})
        q = build_contact_query(req)
        if not q.ready:
            raise HTTPException(409, "; ".join(q.issues))
        left = clay_people_limit() - _clay_people_used_today(con)
        if body.get("confirm") != q.dsl:
            return {"live": False, "query": q.dsl, "employers": [r["domain"] for r in rows],
                    "persona": maps[0].persona, "searches_left_today": left,
                    "note": "Nothing was sent to Clay. Send this exact query back as confirm to run it."}
        if left <= 0:
            raise HTTPException(409, f"Daily Clay people-search cap reached ({clay_people_limit()}).")
        svc = request.app.state.services_factory(True)
        ws = os.getenv("CLAY_WORKSPACE_ID", "1461095")

        async def go():
            async with svc.clay_people_factory() as clay:
                return await run_people_finder(ClayContactsProvider(PeopleGateway(clay), ws), req, con,
                                               allow_live=True)
        try:
            r = asyncio.run(go())
        except Exception as e:  # noqa: BLE001
            from chanakya.clay.mcp_client import unwrap
            e = unwrap(e)
            raise HTTPException(401 if "Reauth" in type(e).__name__ else 502, f"Clay: {type(e).__name__}: {str(e)[:300]}")
        found = con.execute("SELECT DISTINCT person_id, company_id FROM person_sources WHERE run_id=?",
                            (r.run_id,)).fetchall()
        for f in found:
            E.record_discovery(con, "person", f["person_id"], E.BOTTOM, "clay:search-contacts", run_id=r.run_id,
                               detail={"product_id": pid, "persona": maps[0].persona})
            E.record_discovery(con, "company", f["company_id"], E.BOTTOM, "clay:person-first", run_id=r.run_id,
                               detail={"via_person": f["person_id"]})
            SC.rescore_company(con, f["company_id"])
        con.commit()
        return {"live": True, "run_id": r.run_id, "inserted": r.inserted, "updated": r.updated,
                "people": [dict(f) for f in found], "searches_left_today": left - 1}



@router.post("/resolver/clay")
def resolver_clay(request: Request, body: dict = Body(default={})):
    """LinkedIn Resolver via Clay for approved people without a profile URL (proposals go to Review > LinkedIn)."""
    from chanakya.clay.mcp_client import ClayMCPClient, unwrap
    from chanakya.resolver import clay_resolver as CR
    auth.current_user(request)
    with db() as con:
        if clay_people_limit() - _clay_people_used_today(con) <= 0:
            raise HTTPException(409, f"Daily Clay people-search cap reached ({clay_people_limit()}).")
        try:
            return CR.run(con, lambda: ClayMCPClient(allowed_tools=set(CR.TOOLS)),
                          workspace_id=os.getenv("CLAY_WORKSPACE_ID", "1461095"),
                          limit=int(body.get("limit") or 5), person_ids=body.get("person_ids"))
        except Exception as e:  # noqa: BLE001
            e = unwrap(e)
            raise HTTPException(401 if "Reauth" in type(e).__name__ else 502, f"Clay: {type(e).__name__}: {str(e)[:300]}")


@router.post("/catalog/persona-proposals")
def persona_proposals(request: Request):
    """Draft buyer personas (pending, for human approval) for accelerators that have none."""
    from chanakya.people.persona_proposals import propose_missing
    from chanakya.workflow.services import make_llm
    auth.current_user(request)
    llm = make_llm(task="accelerator_matching")
    if llm is None:
        raise HTTPException(409, "Claude Sonnet is not configured (ANTHROPIC_API_KEY).")
    with db() as con:
        return {"rows": propose_missing(con, llm)}
