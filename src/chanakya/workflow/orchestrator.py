"""Central workflow orchestrator for both discovery approaches.

  TOP_DOWN : catalog -> scout -> company_gate -> people -> role_gate -> resolve -> linkedin_gate -> email ->
             email_gate -> research -> research_gate -> qa -> qa_gate -> outreach
  BOTTOM_UP: bu_search -> bu_relevance -> bu_relevance_gate -> bu_profile -> bu_company -> bu_match ->
             company_gate -> role_gate -> email -> email_gate -> research -> research_gate -> qa -> qa_gate -> outreach

Stage outcomes: done | planned (dry run: nothing written, nothing called) | waiting_human (a review gate is open;
the message says exactly which command to run) | blocked (an external dependency or confirmation is missing) |
failed (exception; earlier results are kept). `run()` executes stages in order from the first one that is not done
and stops at the first waiting / blocked / failed stage, so `resume` continues from there. A failed stage is retried
on the next run; nothing before it is redone. `run_stage()` runs one stage on its own.

Every agent still enforces its own gates (qualified companies, approved roles, ...); the orchestrator only sequences
them, records workflow / stage / run ids and logs every step in workflow_events. Agents select their inputs from the
shared database, so a stage processes every eligible record, not only those found by this workflow.
Live mode never bypasses a confirmation: Clay searches still need the exact query confirmed in the workflow params,
and the harness / paid Clay email lookup need to be configured explicitly.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from chanakya.storage.db import jdump, now

ROOT = Path(__file__).resolve().parents[3]
# Shared tail: every direction feeds the same gates, agents and prospect records.
_TAIL = ["company_gate", "site_people", "people", "role_gate", "resolve", "linkedin_gate", "email", "email_gate", "research",
         "research_gate", "qa", "qa_gate", "outreach"]
_BU = ["bu_search", "bu_relevance", "bu_relevance_gate", "bu_profile", "bu_company", "bu_match"]
TOP_DOWN = ["catalog", "signal_sweep", "scout", "company_signals", "intel"] + _TAIL
BOTTOM_UP = _BU + ["company_signals", "intel"] + [x for x in _TAIL if x not in ("people", "resolve", "linkedin_gate")]
COMBINED = ["catalog", "signal_sweep", "scout"] + _BU + ["company_signals", "intel"] + _TAIL
KINDS = {"top_down": TOP_DOWN, "bottom_up": BOTTOM_UP, "combined": COMBINED}
CLI = "python scripts/chanakya.py"


@dataclass
class Outcome:
    status: str  # done | planned | waiting_human | blocked
    output: dict = field(default_factory=dict)
    message: str = ""
    run_ids: list[int] = field(default_factory=list)


@dataclass
class Services:
    """External clients. Anything left None is treated as not configured (the stage reports `blocked`)."""
    llm: Any = None
    harness: Any = None  # resolver.harness_client.HarnessClient
    clay_scout_factory: Callable | None = None  # -> async ctx manager with call_tool (Company Scout allowlist)
    clay_people_factory: Callable | None = None  # -> same, People Finder allowlist
    fetcher: Callable | None = None  # emailhunter website fetcher
    mx_lookup: Callable | None = None
    paid_email: Any = None  # emailhunter.clay.PaidEmailLookup (only with a SpendApproval)
    sync_poster: Callable | None = None
    lock_path: Path | None = None
    real_sources: bool = False  # True from make_services: external public sources (tenders) on by default
    tender_post: Callable | None = None  # tests: replaces the Contracts Finder HTTP POST
    jobs_get: Callable | None = None  # tests: replaces the job-board HTTP GET
    domain_suggest: Callable | None = None  # tests: replaces the Wikidata lookup
    llm_for: Callable | None = None  # task -> routed client (llm/routing.py); None = use `llm` for every task


def _llm(c: "Ctx", task: str):
    """The model routed for this task. Recorded in each stage's outcome so the run shows which model ran."""
    m = c.s.llm_for(task) if c.s.llm_for else c.s.llm
    c.models_used[task] = getattr(m, "model", None) if m is not None else None
    return m


class Ctx:
    def __init__(self, con, wf: dict, services: Services, live: bool):
        self.con, self.wf, self.s, self.live = con, wf, services, live
        self.params: dict = json.loads(wf["params_json"])
        self.models_used: dict = {}

    def log(self, stage, msg, level="info", **detail):
        self.con.execute("INSERT INTO workflow_events(workflow_id,stage,level,message,detail_json,at) "
                         "VALUES(?,?,?,?,?,?)", (self.wf["id"], stage, level, msg, jdump(detail or None), now()))
        self.con.commit()


# ---------------------------------------------------------------- helpers
def _one(con, sql, *a):
    return con.execute(sql, a).fetchone()[0]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"chanakya_script_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _li_disabled(c: Ctx) -> bool:
    """True when the configured harness would reach real LinkedIn but the LinkedIn policy is disabled."""
    from chanakya.resolver.harness_client import _default_runner
    h = c.s.harness
    return h is not None and h.runner is _default_runner and not h.automation_allowed


def _clay_allowance(c: Ctx) -> int:
    """Searches Clay may run today WITHOUT a per-query confirmation: CHANAKYA_CLAY_AUTO_SEARCHES_PER_DAY minus today's
    Clay runs. Default 0 = every live Clay search still needs its exact query confirmed (spend control)."""
    import datetime as _dt
    import os
    cap = int(os.getenv("CHANAKYA_CLAY_AUTO_SEARCHES_PER_DAY", "0") or 0)
    if cap <= 0:
        return 0
    used = _one(c.con, "SELECT COUNT(*) FROM runs WHERE agent IN ('company_scout','people_finder') AND "
                       "started_at >= ?", _dt.date.today().isoformat())
    return max(0, cap - used)


def _manual_route(c: Ctx, queries: list[str]) -> dict:
    from chanakya.intel.compliance import manual_route
    return manual_route(c.con, queries)


def _needs_harness(c: Ctx) -> Outcome | None:
    if c.s.harness is None:
        return Outcome("blocked", message="LinkedIn harness not configured: set LINKEDIN_HARNESS_DIR (a checkout of "
                                          "github.com/mrigankad/Linkedinharness-), install uv, run `uv run "
                                          "linkedin_harness.py login` there. See docs/SETUP.md.")
    return None


# ---------------------------------------------------------------- stages: top-down
def st_catalog(c: Ctx) -> Outcome:
    n = _one(c.con, "SELECT COUNT(*) FROM products")
    icps = _one(c.con, "SELECT COUNT(*) FROM icps WHERE product_id IS NOT NULL")
    path = c.params.get("catalog_path")
    if not path:
        if n and icps:
            return Outcome("done", {"products": n, "icps": icps, "note": "existing catalogue reused"})
        return Outcome("blocked", message="no catalogue imported; set params.catalog_path to the Wayam products YAML")
    if not c.live:
        return Outcome("planned", {"would_import": path, "products_now": n})
    llm = _llm(c, "extraction")
    if llm is None:
        return Outcome("blocked", message="catalog import needs a model for 'extraction' (ANTHROPIC_API_KEY)")
    from chanakya.catalog.products import run_catalog_yaml
    r = run_catalog_yaml(llm, c.con, path)
    return Outcome("done", {"products": _one(c.con, "SELECT COUNT(*) FROM products"), "icps": len(r.icps)})


def _scout_prepare(c: Ctx):
    from chanakya.catalog.models import ICP
    from chanakya.scout.agent import ClaySearchProvider
    from chanakya.scout.discovery import DiscoveryRequest
    from chanakya.scout.dsl import ClaySchema
    from chanakya.scout.industry_map import map_industries
    from chanakya.scout.pilot_scope import PilotScope
    product = c.params.get("product_id")
    row = c.con.execute("SELECT id, icp_json FROM icps WHERE product_id=? AND content_sha256 IS NOT NULL ORDER BY id "
                        "DESC LIMIT 1", (product,)).fetchone()
    if not row:
        return None, None, None, f"no stored ICP for product {product!r}"
    icp = ICP.model_validate_json(row["icp_json"])
    scope = PilotScope.load(c.params.get("pilot_scope", str(ROOT / "data/pilot_scope.json")))
    icp = scope.apply(icp) if scope else icp
    schema = ClaySchema.from_capabilities(ROOT / "docs/clay_capabilities.json")
    maps = map_industries(c.con, row["id"], icp.industries.values if icp.industries else [], schema.enums["industry"],
                          llm=None)
    ws = c.params.get("clay_workspace_id") or __import__("os").getenv("CLAY_WORKSPACE_ID", "1461095")
    req = DiscoveryRequest(icp, row["id"], int(c.params.get("company_limit", 1)), maps,
                           context={"product_id": product, "workspace_id": ws, "workflow_id": c.wf["id"]})
    prov = ClaySearchProvider(None, schema, expected_workspace_id=ws)
    return prov, req, prov.prepare(req), None


def st_scout(c: Ctx) -> Outcome:
    product = c.params.get("product_id")
    if not product:
        return Outcome("blocked", message="set params.product_id (e.g. PRD-SUTRADHAR)")
    existing = _one(c.con, "SELECT COUNT(*) FROM companies c JOIN icps i ON i.id=c.icp_id WHERE i.product_id=?",
                    product)
    if existing and not c.params.get("new_company_search"):
        return Outcome("done", {"companies_for_product": existing, "note": "reusing companies already discovered"})
    from chanakya.scout.dsl import DSLError
    try:
        prov, req, prep, err = _scout_prepare(c)
    except DSLError as e:  # e.g. "refusing an unfiltered search": not search-ready, not a failure
        prov = req = prep = None
        err = str(e)
    signal_cos = _one(c.con, "SELECT COUNT(DISTINCT s.company_id) FROM signal_fits f JOIN signals s ON s.id=f.signal_id "
                           "WHERE f.product_id=? AND s.company_id IS NOT NULL", product)
    if c.wf["kind"] == "top_down" and (err or not prep.ready) and signal_cos:
        # Accelerator without a Clay search profile: top-down continues from demand signals that fit it (no spend).
        return Outcome("done", {"companies_from_signals": signal_cos, "skipped_company_search": err or prep.issues,
                                "note": "no search-ready ICP for this accelerator; using companies whose demand "
                                        "signals fit it"})
    if c.wf["kind"] == "combined" and (err or not prep.ready):
        # Combined runs keep going people-first; a Clay company search needs a search-ready ICP (no spend skipped).
        return Outcome("done", {"skipped": err or prep.issues, "note": "company search not ready for this ICP; "
                                                                      "combined run continues with signals + people"})
    if err:
        return Outcome("blocked", message=err)
    if not prep.ready:
        return Outcome("blocked", {"issues": prep.issues}, "query not ready (approve industry mappings: "
                                                           "python scripts/review_mappings.py list)")
    if not c.live:
        return Outcome("planned", {"query": prep.text, "limit": req.limit})
    if c.params.get("confirm_scout_query") != prep.text and _clay_allowance(c) > 0:
        c.log("scout", "Clay query auto-confirmed under the daily allowance", query=prep.text)
        c.params["confirm_scout_query"] = prep.text
    if c.params.get("confirm_scout_query") != prep.text:
        return Outcome("blocked", {"query": prep.text}, "live Clay search needs params.confirm_scout_query set to "
                                                        "exactly this query (spend control)")
    if c.s.clay_scout_factory is None:
        return Outcome("blocked", message="Clay MCP client not configured")
    from chanakya.scout.agent import ClayGateway, run_discovery

    async def go():
        async with c.s.clay_scout_factory() as clay:
            prov.gateway = ClayGateway(clay)
            return await run_discovery(prov, req, c.con, allow_live=True)
    r = asyncio.run(go())
    rid = _one(c.con, "SELECT MAX(id) FROM runs WHERE agent='company_scout'")
    return Outcome("done", {"inserted": r.inserted, "updated": r.updated, "companies": r.companies}, run_ids=[rid])


def _scope_runs(c: Ctx) -> list[int]:
    """Agent run ids recorded by THIS workflow's stages (used to scope its review gates)."""
    ids: set[int] = set()
    for r in c.con.execute("SELECT run_ids_json FROM workflow_stages WHERE workflow_id=?", (c.wf["id"],)):
        ids.update(int(x) for x in json.loads(r[0] or "[]") if x)
    return sorted(ids)


def _in(ids: list[int]) -> str:
    return "(" + ",".join(str(int(i)) for i in ids) + ")" if ids else "(NULL)"


def _scope_companies(c: Ctx) -> list[int]:
    """Companies this workflow discovered: its Clay company-search runs, or its bottom-up leads."""
    runs = _in(_scope_runs(c))
    rows = c.con.execute(f"SELECT DISTINCT company_id FROM company_sources WHERE run_id IN {runs} UNION "
                         "SELECT company_id FROM bottomup_leads WHERE workflow_id=? AND company_id IS NOT NULL",
                         (c.wf["id"],)).fetchall()
    return [r[0] for r in rows]


def _scope_people(c: Ctx) -> str:
    """SQL id list of the people this workflow touches: found by its runs, imported for it, or at its companies."""
    runs = _in(_scope_runs(c))
    ids = {r[0] for r in c.con.execute(
        f"SELECT person_id FROM person_sources WHERE run_id IN {runs} UNION SELECT person_id FROM bottomup_leads "
        f"WHERE workflow_id=? AND person_id IS NOT NULL UNION SELECT person_id FROM person_roles WHERE company_id IN "
        f"{_in(_scope_or_all(c))}", (c.wf["id"],))}
    return _in(sorted(i for i in ids if i))


def st_company_gate(c: Ctx) -> Outcome:
    mine = _scope_companies(c)
    unreviewed = [r[0] for r in c.con.execute(
        f"SELECT domain FROM companies WHERE id IN {_in(mine)} AND qualification_source!='human' ORDER BY id")]
    if unreviewed:
        return Outcome("waiting_human", {"pending": unreviewed},
                       f"review the companies this run found: {', '.join(unreviewed)} (Review > Companies)")
    # Only companies in THIS run's scope count (its own discoveries, plus the accelerator's signal-fit accounts for
    # top-down); a company qualified long ago for another run must not open this gate.
    scope = _in(_scope_or_all(c))
    q = _one(c.con, f"SELECT COUNT(*) FROM companies WHERE id IN {scope} AND qualification_status='qualified' AND "
                    "qualification_source='human'")
    pending = [r[0] for r in c.con.execute(f"SELECT domain FROM companies WHERE id IN {scope} AND "
                                           "(qualification_source!='human' OR qualification_status='needs_manual_review')"
                                           " ORDER BY id")]
    if q:
        return Outcome("done", {"qualified": q, "still_pending": pending})
    return Outcome("waiting_human", {"pending": pending},
                   f"qualify at least one company: python scripts/review_companies.py qualify <domain> --by <you> "
                   f"--reason <why>   (pending: {', '.join(pending) or 'none - run discovery first'})")


def st_people(c: Ctx) -> Outcome:
    doms = [r[0] for r in c.con.execute(
        f"SELECT c.domain FROM companies c WHERE c.id IN {_in(_scope_or_all(c))} AND c.qualification_status='qualified' "
        "AND c.qualification_source='human' AND NOT EXISTS (SELECT 1 FROM person_roles r WHERE r.company_id=c.id) "
        "ORDER BY c.id")]
    if not doms:
        if not _one(c.con, "SELECT COUNT(*) FROM companies WHERE qualification_status='qualified' AND "
                           "qualification_source='human'"):
            return Outcome("waiting_human", message="no human-qualified company yet (see company_gate)")
        return Outcome("done", {"note": "every qualified company already has people"})
    fp = _load_script("find_people")
    plans, problems = {}, {}
    for d in doms:
        try:
            p = fp.prepare(c.con, d, c.params.get("product_id"), c.params.get("mapping_ids"))
            plans[d] = p
        except fp.FindPeopleError as e:
            problems[d] = str(e)
    if not plans:
        return Outcome("blocked", {"problems": problems}, "no company is ready for a contact search "
                                                          "(approve persona mappings: scripts/review_personas.py)")
    if not c.live:
        return Outcome("planned", {d: p["query"].dsl for d, p in plans.items()} | {"problems": problems})
    confirmed = c.params.get("confirm_people_query") or {}
    todo = {d: p for d, p in plans.items() if confirmed.get(d) == p["query"].dsl}
    allow = _clay_allowance(c)
    for d, p in plans.items():
        if d not in todo and allow > 0:
            c.log("people", "Clay contact query auto-confirmed under the daily allowance", domain=d, dsl=p["query"].dsl)
            todo[d], allow = p, allow - 1
    if not todo:
        have = _one(c.con, f"SELECT COUNT(DISTINCT c.id) FROM companies c JOIN person_roles r ON r.company_id=c.id "
                           f"WHERE c.id IN {_in(_scope_or_all(c))} AND c.qualification_status='qualified' AND "
                           "c.qualification_source='human'")
        if have:  # people already came from company websites, imports or Lakshya: a paid search is optional
            return Outcome("done", {"awaiting_clay_confirmation": {d: p["query"].dsl for d, p in plans.items()},
                                    "companies_with_people": have,
                                    "note": "no Clay contact search was confirmed; the run continues with the people "
                                            "already found (company websites, imports). Confirm a query under "
                                            "Review > Clay and spend to add Clay contacts for the listed companies."})
        return Outcome("blocked", {d: p["query"].dsl for d, p in plans.items()},
                       "live contact search needs params.confirm_people_query = {domain: exact DSL}")
    if c.s.clay_people_factory is None:
        return Outcome("blocked", message="Clay MCP client not configured")
    from chanakya.people.finder import ClayContactsProvider, PeopleGateway, run_people_finder
    import os
    ws = c.params.get("clay_workspace_id") or os.getenv("CLAY_WORKSPACE_ID", "1461095")
    out, runs = {}, []

    async def go(p):
        async with c.s.clay_people_factory() as clay:
            return await run_people_finder(ClayContactsProvider(PeopleGateway(clay), ws), p["request"], c.con,
                                           allow_live=True)
    for d, p in list(todo.items())[: int(c.params.get("people_companies_per_run", 3))]:
        r = asyncio.run(go(p))
        out[d] = {"inserted": r.inserted, "updated": r.updated, "skipped": r.skipped}
        runs.append(r.run_id)
    return Outcome("done", out, run_ids=runs)


def st_role_gate(c: Ctx) -> Outcome:
    """Passes once a human has approved at least one role at this run's companies. People still waiting for review
    do not hold the run back (a board page can list thirty names): they stay in Review > People and roles and are
    reported here."""
    runs = _in(_scope_runs(c))
    mine = _one(c.con, f"SELECT COUNT(*) FROM person_roles r WHERE r.review_status='needs_manual_review' AND "
                       f"r.person_id IN (SELECT person_id FROM person_sources WHERE run_id IN {runs} UNION SELECT "
                       f"person_id FROM bottomup_leads WHERE workflow_id={int(c.wf['id'])} AND person_id IS NOT NULL) "
                       f"AND r.company_id IN (SELECT id FROM companies WHERE qualification_status='qualified' AND "
                       f"qualification_source='human')")
    scope = _in(_scope_or_all(c))  # gate ownership: only this run's companies count
    a = _one(c.con, f"SELECT COUNT(*) FROM person_roles WHERE review_status='approved' AND company_id IN {scope} AND "
                    "company_id IN (SELECT id FROM companies WHERE qualification_status='qualified' AND "
                    "qualification_source='human')")
    p = _one(c.con, f"SELECT COUNT(*) FROM person_roles WHERE review_status='needs_manual_review' AND company_id IN "
                    f"{scope}")
    if a:
        return Outcome("done", {"approved_roles_in_this_run": a, "pending_roles": p,
                                "pending_roles_from_this_run": mine,
                                "note": "people still awaiting review stay in Review > People and roles"})
    return Outcome("waiting_human", {"pending_roles": p, "pending_roles_from_this_run": mine},
                   "approve at least one person/role at a qualified company (Review > People and roles)")


def st_resolve(c: Ctx) -> Outcome:
    from chanakya.resolver.agent import run_resolver, select_candidates
    cands = select_candidates(c.con)
    if not cands:
        return Outcome("done", {"note": "nobody needs LinkedIn resolution (URLs already known or no approved roles)"})
    if _li_disabled(c) and c.live and c.s.clay_people_factory is not None and c.params.get("resolve_via_clay", True):
        # authorised provider route: Clay's licensed people data, proposals go to human review
        import os
        from chanakya.api.workspace import _clay_people_used_today, clay_people_limit
        from chanakya.clay.mcp_client import ClayMCPClient
        from chanakya.resolver import clay_resolver as CR
        if clay_people_limit() - _clay_people_used_today(c.con) > 0:
            out = CR.run(c.con, lambda: ClayMCPClient(allowed_tools=set(CR.TOOLS)),
                         workspace_id=c.params.get("clay_workspace_id") or os.getenv("CLAY_WORKSPACE_ID", "1461095"),
                         limit=int(c.params.get("resolve_limit", 5)))
            return Outcome("done", {**out, "route": "clay", "next": "approve proposals on Review > LinkedIn"})
    if _li_disabled(c):
        from chanakya.intel.compliance import linkedin_people_search_link
        return Outcome("done", {"skipped": "LinkedIn automation disabled", "manual": [
            {"person_id": x.person_id, "name": x.full_name, "link": linkedin_people_search_link(x.full_name, x.company)}
            for x in cands], "next": "paste the profile URL on the Needs page (LinkedIn lookups); it is stored as a "
                                    "human-approved resolution"})
    blocked = _needs_harness(c)
    if blocked:
        return Outcome("blocked", {"candidates": len(cands)}, blocked.message)
    rep = run_resolver(c.s.harness, c.con, allow_live=c.live, limit=int(c.params.get("resolve_limit", 3)),
                       lock_path=c.s.lock_path)
    if rep.dry_run:
        return Outcome("planned", {"planned": rep.planned})
    if rep.stopped:
        return Outcome("blocked", rep.counts, f"harness stopped: {rep.stopped}", [rep.run_id])
    return Outcome("done", rep.counts, run_ids=[rep.run_id])


def st_linkedin_gate(c: Ctx) -> Outcome:
    p = _one(c.con, "SELECT COUNT(*) FROM linkedin_resolutions WHERE review_status='pending_review' AND status!='error'")
    if p:
        return Outcome("waiting_human", {"pending": p}, "review LinkedIn matches: python scripts/resolve_linkedin.py "
                       "list --status pending_review ; approve|reject <id> --by <you> --reason <why>")
    return Outcome("done", {"with_linkedin": _one(c.con, "SELECT COUNT(*) FROM people WHERE linkedin_url IS NOT NULL")})


def st_email(c: Ctx) -> Outcome:
    from chanakya.emailhunter.agent import run_email_hunter, select_candidates
    from chanakya.emailhunter.checks import check_mx
    if not select_candidates(c.con):
        return Outcome("done", {"note": "no approved prospect without an email finding"})
    rep = run_email_hunter(c.con, allow_live=c.live, limit=int(c.params.get("email_limit", 10)), fetcher=c.s.fetcher,
                           mx_lookup=c.s.mx_lookup or check_mx, paid=c.s.paid_email)
    if rep.dry_run:
        return Outcome("planned", {"planned": rep.planned})
    return Outcome("done", rep.counts | {"paid_used": rep.paid_used}, run_ids=[rep.run_id])


def st_email_gate(c: Ctx) -> Outcome:
    p = _one(c.con, f"SELECT COUNT(*) FROM email_records WHERE review_status='pending_review' AND person_id IN "
                    f"{_scope_people(c)}")
    if p:
        return Outcome("waiting_human", {"pending": p}, f"review emails: {CLI} email list --status pending_review ; "
                       f"{CLI} email approve|reject <id> --by <you> --reason <why>")
    return Outcome("done", {"approved": _one(c.con, "SELECT COUNT(*) FROM email_records WHERE review_status='approved'"
                                                     f" AND person_id IN {_scope_people(c)}")})


def st_research(c: Ctx) -> Outcome:
    from chanakya.research.agent import run_researcher, select_candidates
    cands = select_candidates(c.con)
    if not cands:
        return Outcome("done", {"note": "no approved prospect with a LinkedIn URL left to research"})
    if _li_disabled(c):
        # approved-sources route: role records (website, Clay, approved LinkedIn URL) + verified company signals
        from chanakya.research import sources as RS
        if not c.live:
            return Outcome("planned", {"candidates": len(RS.candidates(c.con)), "route": "approved_sources"})
        out = RS.run(c.con, _llm(c, "people_research"), limit=int(c.params.get("research_limit", 3)))
        return Outcome("done", {**out, "route": "approved_sources (no LinkedIn automation)"},
                       run_ids=[out["run_id"]])
    blocked = _needs_harness(c)
    if blocked:
        return Outcome("blocked", {"candidates": len(cands)}, blocked.message)
    rep = run_researcher(c.s.harness, c.con, llm=_llm(c, "people_research"), allow_live=c.live,
                         limit=int(c.params.get("research_limit", 2)), lock_path=c.s.lock_path)
    if rep.dry_run:
        return Outcome("planned", {"planned": rep.planned, "llm": c.models_used.get("people_research")})
    if rep.stopped:
        return Outcome("blocked", rep.counts, f"LinkedIn access stopped: {rep.stopped} (no research claimed)",
                       [rep.run_id])
    return Outcome("done", rep.counts, run_ids=[rep.run_id])


def st_research_gate(c: Ctx) -> Outcome:
    p = _one(c.con, "SELECT COUNT(*) FROM profile_research WHERE review_status='pending_review' AND status='researched'"
                    f" AND person_id IN {_scope_people(c)}")
    if p:
        return Outcome("waiting_human", {"pending": p}, f"review research: {CLI} research list ; {CLI} research "
                       "approve|reject <id> --by <you> --reason <why>")
    return Outcome("done", {"approved": _one(c.con, "SELECT COUNT(*) FROM profile_research WHERE "
                                                     "review_status='approved'")})


def st_qa(c: Ctx) -> Outcome:
    from chanakya.qa.agent import run_qa
    if not c.live:
        return Outcome("planned", {"people": _one(c.con, "SELECT COUNT(DISTINCT person_id) FROM person_roles")})
    r = run_qa(c.con)
    return Outcome("done", r, run_ids=[r["run_id"]])


def st_qa_gate(c: Ctx) -> Outcome:
    sp = _scope_people(c)
    a = _one(c.con, f"SELECT COUNT(*) FROM prospects WHERE review_status='approved' AND person_id IN {sp}")
    p = _one(c.con, "SELECT COUNT(*) FROM prospects WHERE review_status='pending_review' AND qa_status!='blocked' "
                    f"AND person_id IN {sp}")
    if a:
        return Outcome("done", {"approved": a, "pending": p})
    return Outcome("waiting_human", {"pending": p}, f"review prospects: {CLI} qa list ; {CLI} qa approve <id> --by "
                   "<you> --reason <why>")


def st_outreach(c: Ctx) -> Outcome:
    from chanakya.outreach.drafts import eligible, export_eml, generate
    from chanakya.qa.agent import sync
    el = eligible(c.con)
    if not c.live:
        return Outcome("planned", {"eligible_prospects": [e["pid"] for e in el]})
    ids = generate(c.con, llm=_llm(c, "outreach_draft"), sender=c.params.get("sender", "[Your name]"))
    checks = []
    chk = _llm(c, "simple_checks") if ids else None
    if chk is not None:
        from chanakya.intel.ai_agents import check_draft
        for did in ids:
            try:
                r = check_draft(c.con, did, llm=chk)
                checks.append({"draft_id": did, "ok": r["ok"], "issues": len(r["issues"])})
            except Exception as e:  # noqa: BLE001
                checks.append({"draft_id": did, "error": str(e)[:200]})
    out_dir = Path(c.params.get("out_dir", ROOT / "exports"))
    emls = export_eml(c.con, out_dir / "drafts")
    targets = c.params.get("sync_targets")
    from chanakya.qa.agent import parse_targets
    tg = parse_targets(targets) if isinstance(targets, str) else [
        ("csv", str(out_dir / "prospects.csv")), ("xlsx", str(out_dir / "prospects.xlsx"))]
    syn = sync(c.con, tg, allow_live=bool(c.params.get("allow_webhook")), only_approved=True, poster=c.s.sync_poster)
    return Outcome("done", {"drafts_created": ids, "ai_checks": checks, "eml_files": emls, "sync": syn,
                            "note": "drafts only - nothing was sent"})


# ---------------------------------------------------------------- stages: bottom-up
def st_bu_search(c: Ctx) -> Outcome:
    from chanakya.bottomup.agent import plan_queries, search_people
    queries = c.params.get("queries") or ([c.params["query"]] if c.params.get("query") else
                                          plan_queries(c.con, c.params.get("product_id")))
    if not queries:
        return Outcome("blocked", message="no query: set params.query, or approve persona titles "
                                          "(scripts/review_personas.py) so queries can be planned")
    manual = _one(c.con, "SELECT COUNT(*) FROM bottomup_leads WHERE workflow_id=?", c.wf["id"])
    if c.s.harness is None or _li_disabled(c):  # compliant route: links + manual import, never automation
        if manual:
            return Outcome("done", {"manual_leads": manual, "route": "manual import (LinkedIn automation disabled)"})
        if not c.live:
            return Outcome("planned", _manual_route(c, queries))
        return Outcome("waiting_human", _manual_route(c, queries),
                       "LinkedIn automation is disabled: open the search links, then import the people you choose "
                       "(Discovery > Import people) for this run")
    if not c.live:
        return Outcome("planned", {"queries": queries, "n": int(c.params.get("search_n", 10))})
    out, runs = {}, []
    for q in queries:
        r = search_people(c.s.harness, c.con, q, n=int(c.params.get("search_n", 10)), workflow_id=c.wf["id"],
                          allow_live=True, lock_path=c.s.lock_path)
        out[q] = r.counts or {"stopped": r.stopped}
        runs.append(r.run_id)
        if r.stopped:
            return Outcome("blocked", out, f"LinkedIn search stopped: {r.stopped}", runs)
    return Outcome("done", out, run_ids=runs)


def st_bu_relevance(c: Ctx) -> Outcome:
    from chanakya.bottomup.agent import assess_relevance
    n = _one(c.con, "SELECT COUNT(*) FROM bottomup_leads WHERE status='found'")
    if not c.live:
        return Outcome("planned", {"leads_to_assess": n})
    return Outcome("done", assess_relevance(c.con, llm=_llm(c, "people_research")).counts)


def st_bu_relevance_gate(c: Ctx) -> Outcome:
    rel = _one(c.con, "SELECT COUNT(*) FROM bottomup_leads WHERE status NOT IN ('found','not_relevant','unclear')")
    unclear = _one(c.con, "SELECT COUNT(*) FROM bottomup_leads WHERE status='unclear'")
    if rel:
        return Outcome("done", {"relevant_or_further": rel, "unclear": unclear})
    return Outcome("waiting_human", {"unclear": unclear}, f"no relevant lead yet; review unclear leads: {CLI} leads "
                   f"list --status unclear ; {CLI} leads mark <id> relevant|not_relevant --by <you> --reason <why>")


def _bu_harness_step(c: Ctx, fn, key: str) -> Outcome:
    if _li_disabled(c) or (c.s.harness is None and _one(c.con, "SELECT COUNT(*) FROM bottomup_leads WHERE "
                                                               "workflow_id=? AND harness_version='manual-import'",
                                                               c.wf["id"])):
        return Outcome("done", {"skipped": "LinkedIn automation disabled; imported leads carry their company and are "
                                           "verified against the company website at import"})
    blocked = _needs_harness(c)
    if blocked and c.live:
        return blocked
    if blocked:
        from chanakya.resolver.harness_client import HarnessClient
        harness = HarnessClient(Path("<LINKEDIN_HARNESS_DIR>"))
    else:
        harness = c.s.harness
    r = fn(harness, c.con, allow_live=c.live, limit=int(c.params.get(key, 5)), lock_path=c.s.lock_path)
    if r.dry_run:
        return Outcome("planned", {"planned": r.planned})
    if r.stopped:
        return Outcome("blocked", r.counts, f"LinkedIn access stopped: {r.stopped}", [r.run_id])
    return Outcome("done", r.counts or {"note": "nothing to do"}, run_ids=[x for x in [r.run_id] if x])


def st_bu_profile(c: Ctx) -> Outcome:
    from chanakya.bottomup.agent import profile_leads
    return _bu_harness_step(c, profile_leads, "profile_limit")


def st_bu_company(c: Ctx) -> Outcome:
    from chanakya.bottomup.agent import verify_companies
    return _bu_harness_step(c, verify_companies, "company_limit")


def st_bu_match(c: Ctx) -> Outcome:
    from chanakya.bottomup.agent import match_accelerators
    if not c.live:
        return Outcome("planned", {"linked_leads": _one(c.con, "SELECT COUNT(*) FROM bottomup_leads WHERE "
                                                               "status='linked'")})
    return Outcome("done", match_accelerators(c.con, llm=_llm(c, "accelerator_matching")).counts)


def st_signal_sweep(c: Ctx) -> Outcome:
    """Top-down demand signals. params.seed_path imports Lakshya research (idempotent by external id)."""
    from chanakya.intel import proof
    from chanakya.intel.signals import import_lakshya_seed
    path = c.params.get("seed_path")
    n = _one(c.con, "SELECT COUNT(*) FROM signals")
    # Default on only with the real service set (make_services); unit tests opt in with params.tender_sweep.
    tenders = c.params.get("tender_sweep", c.s.real_sources)
    jobs = c.params.get("job_sweep", c.s.real_sources)
    if not c.live:
        return Outcome("planned", {"would_import": path, "tender_sweep": tenders, "job_sweep": jobs,
                                   "signals_now": n})
    out: dict = {"signals_before": n}
    failed_sources: list[str] = []
    from chanakya.intel import lakshya as LK
    root = LK.locate(path) if (path or c.s.real_sources) else None
    if root is not None:  # full handover (signals, accounts, leads, deep dives, competitors); idempotent refresh
        proof.seed(c.con)
        r = LK.import_all(c.con, root, by=f"workflow #{c.wf['id']}", workflow_id=c.wf["id"])
        out["seed"] = r["signals"]
        out["lakshya"] = {"records": r["records"], "validation": r["validation"]}
        if not r["validation"]["ok"]:
            return Outcome("failed", out, "Lakshya import does not match its source: "
                           + json.dumps(r["validation"])[:300])
    elif path:
        proof.seed(c.con)
        out["seed"] = import_lakshya_seed(c.con, path, workflow_id=c.wf["id"])
    if tenders:
        import datetime as _dt
        from chanakya.intel.sources import tenders_uk
        kw = c.params.get("tender_keywords")
        if not kw and c.params.get("product_id"):
            kw = {c.params["product_id"]: tenders_uk.DEFAULT_KEYWORDS.get(c.params["product_id"], [])}
        since = _dt.date.today() - _dt.timedelta(days=int(c.params.get("tender_days", 120)))
        r = tenders_uk.sweep(c.con, keywords=kw or None, since=since, post=c.s.tender_post, workflow_id=c.wf["id"])
        out["tenders"] = {k: r[k] for k in ("source", "since", "queries", "created", "updated", "skipped", "errors")}
        out["tenders"]["new_signal_urls"] = [x["url"] for x in r["signals"] if x["created"]][:30]
        if r["errors"] and not r["queries"]:
            failed_sources.append("tenders: " + r["errors"][0])
    if jobs:  # hiring signals from free public job APIs: employers hiring for a need become top-down companies
        import datetime as _dt
        from chanakya.intel.sources import jobs_public
        kw = c.params.get("job_keywords")
        if not kw and c.params.get("product_id"):
            kw = {c.params["product_id"]: jobs_public.DEFAULT_KEYWORDS.get(c.params["product_id"], [])}
        since = _dt.date.today() - _dt.timedelta(days=int(c.params.get("job_days", 60)))
        r = jobs_public.sweep(c.con, keywords=kw or None, since=since, get=c.s.jobs_get, workflow_id=c.wf["id"],
                              feeds=tuple(c.params.get("job_feeds") or ("arbeitnow", "remotive")))
        out["jobs"] = {k: r[k] for k in ("feeds", "since", "created", "updated", "errors")}
        out["jobs"]["new_signal_urls"] = [x["url"] for x in r["signals"] if x["created"]][:30]
        if r["errors"] and len(r["errors"]) == len(r["feeds"]):
            failed_sources.append("job boards: " + r["errors"][0])
    if tenders or jobs:
        # Companies this run's external sources found come first; accounts only refreshed from the Lakshya
        # handover follow, so the resolver's budget goes to what is new.
        ext = [x[0] for x in c.con.execute(
            "SELECT DISTINCT company_id FROM signals WHERE workflow_id=? AND company_id IS NOT NULL AND provenance "
            "NOT LIKE 'lakshya%' ORDER BY id DESC", (c.wf["id"],))]
        rest = [x[0] for x in c.con.execute(
            "SELECT DISTINCT company_id FROM signals WHERE workflow_id=? AND company_id IS NOT NULL ORDER BY id",
            (c.wf["id"],)) if x[0] not in ext]
        new_cos = ext + rest
        if c.params.get("auto_resolve", True) and new_cos:
            from chanakya.emailhunter.checks import default_fetcher
            from chanakya.intel.auto_resolve import auto_resolve
            out["auto_resolve"] = auto_resolve(c.con, new_cos, fetcher=c.s.fetcher or default_fetcher(),
                                               suggest=c.s.domain_suggest,
                                               limit=int(c.params.get("auto_resolve_limit", 15)),
                                               workflow_id=c.wf["id"])
        out["new_companies_this_run"] = len(ext)
        # Every external source failed: that is a blocked stage, not a quiet success on old data.
        if failed_sources and len(failed_sources) == int(bool(tenders)) + int(bool(jobs)):
            return Outcome("blocked", out, "signal sources unreachable: " + "; ".join(failed_sources))
        if failed_sources:
            out["warnings"] = failed_sources
    if not path and not tenders and not jobs:
        out["note"] = "no seed_path and external sweeps off; existing signals used"
    out["signals_after"] = _one(c.con, "SELECT COUNT(*) FROM signals")
    return Outcome("done", out)


def st_site_people(c: Ctx) -> Outcome:
    """Named leaders from qualified companies' own websites (robots respected); every role waits for human review."""
    from chanakya.people.website_people import find_people
    scope = _scope_or_all(c)
    done_before = {json.loads(r[0] or "{}").get("company_id") for r in c.con.execute(
        "SELECT detail_json FROM runs WHERE agent='website_people'")}
    ids = [r[0] for r in c.con.execute(
        f"SELECT id FROM companies WHERE id IN {_in(scope)} AND qualification_status='qualified' AND "
        "qualification_source='human' AND identity_status='resolved' ORDER BY id")]
    todo = [i for i in ids if c.params.get("site_people_rescan") or i not in done_before]
    prio = c.params.get("priority_company_ids") or []  # set by the run coordinator (orchestrator model)
    todo.sort(key=lambda i: (prio.index(i) if i in prio else len(prio), i))
    todo = todo[: int(c.params.get("site_people_limit", 3))]
    if not c.live:
        return Outcome("planned", {"companies": todo})
    if not todo:
        return Outcome("done", {"note": "no newly qualified company with a resolved domain to scan",
                                "qualified_in_scope": len(ids)})
    if c.s.llm_for is None and not c.params.get("site_people_with_default_llm"):
        return Outcome("done", {"skipped": "no per-task model routing in this run (dry run, simulated, or test "
                                           "services); no names extracted", "companies": todo})
    llm = _llm(c, "extraction")
    from chanakya.emailhunter.checks import default_fetcher
    f = c.s.fetcher or default_fetcher()
    out, runs = [], []
    for cid in todo:
        try:
            r = find_people(c.con, cid, f, llm=llm, workflow_id=c.wf["id"])
        except Exception as e:  # noqa: BLE001 - reported per company, never hidden
            r = {"company_id": cid, "error": f"{type(e).__name__}: {e}"[:300]}
        out.append(r)
        if r.get("run_id"):
            runs.append(r["run_id"])
    return Outcome("done", {"companies": len(out), "people_kept": sum(r.get("kept", 0) for r in out),
                            "model": c.models_used.get("extraction"), "detail": out}, run_ids=runs)


def _scope_or_all(c: Ctx) -> list[int]:
    ids = set(_scope_companies(c))
    if c.wf["kind"] in ("top_down", "combined") and c.params.get("product_id"):
        ids |= {r[0] for r in c.con.execute("SELECT c.id FROM companies c JOIN icps i ON i.id=c.icp_id WHERE "
                                            "i.product_id=?", (c.params["product_id"],))}
        ids |= {r[0] for r in c.con.execute("SELECT DISTINCT s.company_id FROM signal_fits f JOIN signals s ON "
                                            "s.id=f.signal_id WHERE f.product_id=? AND s.company_id IS NOT NULL",
                                            (c.params["product_id"],))}
    if c.params.get("seed_path"):
        ids |= {r[0] for r in c.con.execute("SELECT DISTINCT company_id FROM signals WHERE workflow_id=? AND "
                                            "company_id IS NOT NULL", (c.wf["id"],))}
    return sorted(ids)


def st_company_signals(c: Ctx) -> Outcome:
    """Top-down evidence research on this run's companies from their own public websites (robots respected)."""
    from chanakya.intel.research_web import company_web_signals
    ids = [i for i in _scope_or_all(c) if _one(c.con, "SELECT COUNT(*) FROM companies WHERE id=? AND "
                                                      "identity_status='resolved' AND domain NOT LIKE '%:%'", i)]
    ids = ids[: int(c.params.get("company_signal_limit", 5))]
    if not c.live:
        return Outcome("planned", {"companies": ids})
    if c.s.fetcher is None and not ids:
        return Outcome("done", {"note": "no company with a resolved domain in scope"})
    from chanakya.emailhunter.checks import default_fetcher
    f = c.s.fetcher or default_fetcher()
    out = [company_web_signals(c.con, i, f, workflow_id=c.wf["id"]) for i in ids]
    return Outcome("done", {"companies": len(out), "signals": sum(len(o.get("signals", [])) for o in out),
                            "detail": out})


def st_intel(c: Ctx) -> Outcome:
    """Verify signal sources, recompute gap themes, rescore opportunities for this run's companies."""
    from chanakya.intel.research_web import refresh
    ids = _scope_or_all(c)
    if not c.live:
        return Outcome("planned", {"companies": ids})
    from chanakya.emailhunter.checks import default_fetcher
    f = c.s.fetcher or (default_fetcher() if c.params.get("verify_sources", True) else None)
    r = refresh(c.con, ids or None, f, verify_limit=int(c.params.get("verify_limit", 40)))
    out = {"verified": len(r["verified"]), "gap_themes": r["gap_themes"], "opportunities": r["opportunities"]}
    out["ai"] = _intel_ai(c, ids)
    return Outcome("done", out)


def _intel_ai(c: Ctx, ids: list[int]) -> dict:
    """Model-backed steps after deterministic scoring. Each reports what ran, on which model, or why it did not.
    Haiku labels unclassified evidence; Opus writes qualification briefs for the top accounts; Opus coordinates the
    run (one brief over both directions). No step changes a score or opens a gate."""
    from chanakya.intel import ai_agents as AI
    res: dict = {"classified": [], "briefs": [], "coordination": None, "errors": []}
    if not c.params.get("ai_steps", True):
        return {"skipped": "params.ai_steps is false"}
    if c.s.llm_for is None:
        return {"skipped": "no per-task model routing in this run (dry run, simulated, or test services)"}
    scope = ids or [r[0] for r in c.con.execute("SELECT DISTINCT company_id FROM opportunities")]
    cls = _llm(c, "signal_classification")
    q = _llm(c, "complex_qualification")
    from chanakya.llm import routing as _routing
    # Orchestrator model: Qwen Max (route chanakya_agent) when it is configured; otherwise the Opus coordination
    # route, and the run REPORTS that fallback with the reason. Never silent, never a third model.
    co, orch = _llm(c, _routing.ORCHESTRATOR_TASK), None
    if co is not None:
        orch = {"task": _routing.ORCHESTRATOR_TASK, "model": getattr(co, "model", None), "fallback": False,
                "reason": "intended orchestrator"}
    else:
        c.models_used.pop(_routing.ORCHESTRATOR_TASK, None)
        co = _llm(c, _routing.ORCHESTRATOR_FALLBACK_TASK)
        orch = {"task": _routing.ORCHESTRATOR_FALLBACK_TASK if co is not None else None,
                "model": getattr(co, "model", None) if co is not None else None, "fallback": co is not None,
                "intended_model": _routing.model_for_tier("qwen"),
                "reason": _routing.orchestrator()["reason"]}
    res["orchestrator"] = orch
    if cls is None or q is None or co is None:
        res["errors"].append("a model route is not configured (see Settings > Models); AI steps not run")
        return res
    todo = [i for i in scope if _one(c.con, "SELECT COUNT(*) FROM signals WHERE company_id=? AND "
                                          "COALESCE(verification_json,'') NOT LIKE '%evidence_class%' AND "
                                          "COALESCE(verification_status,'')!='rejected'", i)]
    for cid in todo[: int(c.params.get("classify_limit", 5))]:
        try:
            res["classified"].append({"company_id": cid, "labelled": AI.classify_signals(c.con, cid, llm=cls)["labelled"]})
        except Exception as e:  # noqa: BLE001 - recorded, never hidden
            res["errors"].append(f"classify {cid}: {e}"[:200])
    top = [r[0] for r in c.con.execute(f"SELECT company_id FROM opportunities WHERE company_id IN {_in(scope)} "
                                       "GROUP BY company_id ORDER BY MAX(score) DESC LIMIT ?",
                                       (int(c.params.get("brief_limit", 2)),))] if scope else []
    for cid in top:
        try:
            b = AI.analyse_account(c.con, cid, by=f"workflow:{c.wf['id']}", llm=q)
            res["briefs"].append({"company_id": cid, "analysis_id": b["id"], "confidence": b["confidence"]})
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"brief {cid}: {e}"[:200])
    try:
        res["coordination"] = AI.coordinate_run(c.con, c.wf, scope, llm=co, purpose=orch["task"])
        res["coordination"]["orchestrator"] = orch
        order = [p["company_id"] for p in res["coordination"].get("priorities", [])]
        if order:  # the coordinator's ranking decides which accounts later stages work on first
            c.params["priority_company_ids"] = order
            c.con.execute("UPDATE workflows SET params_json=? WHERE id=?", (json.dumps(c.params), c.wf["id"]))
            c.con.commit()
    except Exception as e:  # noqa: BLE001
        res["errors"].append(f"coordination: {e}"[:200])
    return res


STAGES: dict[str, Callable[[Ctx], Outcome]] = {
    "catalog": st_catalog, "scout": st_scout, "company_gate": st_company_gate, "people": st_people,
    "role_gate": st_role_gate, "resolve": st_resolve, "linkedin_gate": st_linkedin_gate, "email": st_email,
    "email_gate": st_email_gate, "research": st_research, "research_gate": st_research_gate, "qa": st_qa,
    "qa_gate": st_qa_gate, "outreach": st_outreach, "bu_search": st_bu_search, "bu_relevance": st_bu_relevance,
    "bu_relevance_gate": st_bu_relevance_gate, "bu_profile": st_bu_profile, "bu_company": st_bu_company,
    "bu_match": st_bu_match, "signal_sweep": st_signal_sweep, "company_signals": st_company_signals,
    "intel": st_intel, "site_people": st_site_people}
assert set(TOP_DOWN) | set(BOTTOM_UP) | set(COMBINED) <= set(STAGES)


def ensure_stages(con, wid: int) -> None:
    """Workflows created before a stage list changed get the new stages (pending) and the current order."""
    wf = con.execute("SELECT kind FROM workflows WHERE id=?", (wid,)).fetchone()
    have = {r[0] for r in con.execute("SELECT stage FROM workflow_stages WHERE workflow_id=?", (wid,))}
    for i, st in enumerate(KINDS[wf[0]]):
        if st not in have:
            con.execute("INSERT INTO workflow_stages(workflow_id,seq,stage,status,run_ids_json) VALUES(?,?,?,?,?)",
                        (wid, i, st, "pending", "[]"))
        else:
            con.execute("UPDATE workflow_stages SET seq=? WHERE workflow_id=? AND stage=?", (i, wid, st))
    con.commit()


# ---------------------------------------------------------------- engine
def create_workflow(con, kind: str, params: dict | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    wid = con.execute("INSERT INTO workflows(kind,params_json,status,current_stage,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?)", (kind, jdump(params or {}), "created", KINDS[kind][0], now(),
                                              now())).lastrowid
    for i, s in enumerate(KINDS[kind]):
        con.execute("INSERT INTO workflow_stages(workflow_id,seq,stage,status,run_ids_json) VALUES(?,?,?,?,?)",
                    (wid, i, s, "pending", "[]"))
    con.commit()
    return wid


def get_workflow(con, wid: int) -> dict:
    r = con.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
    if r is None:
        raise ValueError(f"unknown workflow {wid}")
    return dict(r)


def update_params(con, wid: int, **params) -> dict:
    wf = get_workflow(con, wid)
    p = {**json.loads(wf["params_json"]), **params}
    con.execute("UPDATE workflows SET params_json=?, updated_at=? WHERE id=?", (jdump(p), now(), wid))
    con.commit()
    return p


def status(con, wid: int) -> dict:
    ensure_stages(con, wid)
    wf = get_workflow(con, wid)
    stages = [dict(r) for r in con.execute("SELECT stage,status,attempts,run_ids_json,error,output_json,finished_at "
                                           "FROM workflow_stages WHERE workflow_id=? ORDER BY seq", (wid,))]
    for s in stages:
        s["run_ids"] = json.loads(s.pop("run_ids_json"))
        s["output"] = json.loads(s.pop("output_json") or "null")
    return {**{k: wf[k] for k in ("id", "kind", "status", "current_stage", "created_at", "updated_at")},
            "params": json.loads(wf["params_json"]), "stages": stages}


def _exec(con, wf: dict, stage: str, services: Services, live: bool) -> Outcome:
    c = Ctx(con, wf, services, live)
    row = con.execute("SELECT * FROM workflow_stages WHERE workflow_id=? AND stage=?", (wf["id"], stage)).fetchone()
    if not live:  # dry run: evaluate only, never persist a stage status
        try:
            o = STAGES[stage](c)
        except Exception as e:
            o = Outcome("blocked", {"error": f"{type(e).__name__}: {e}"}, "dry-run evaluation failed")
        c.log(stage, f"dry-run: {o.status} {o.message}".strip(), output=o.output)
        return o
    con.execute("UPDATE workflow_stages SET status='running', attempts=attempts+1, started_at=?, error=NULL WHERE id=?",
                (now(), row["id"]))
    con.execute("UPDATE workflows SET status='running', current_stage=?, updated_at=? WHERE id=?", (stage, now(),
                                                                                                    wf["id"]))
    con.commit()
    c.log(stage, "start")
    try:
        o = STAGES[stage](c)
    except Exception as e:
        con.rollback()
        err = f"{type(e).__name__}: {e}"
        con.execute("UPDATE workflow_stages SET status='failed', error=?, finished_at=? WHERE id=?",
                    (err[:2000], now(), row["id"]))
        con.execute("UPDATE workflows SET status='failed', updated_at=? WHERE id=?", (now(), wf["id"]))
        con.commit()
        c.log(stage, err, "error", traceback=traceback.format_exc()[-3000:])
        return Outcome("failed", {"error": err}, err)
    if c.models_used and isinstance(o.output, dict):
        o.output["models_used"] = c.models_used
    runs = json.loads(row["run_ids_json"]) + [r for r in o.run_ids if r]
    con.execute("UPDATE workflow_stages SET status=?, output_json=?, run_ids_json=?, error=?, finished_at=? WHERE id=?",
                (o.status, jdump(o.output), jdump(runs), o.message or None, now(), row["id"]))
    wstatus = {"done": "running", "waiting_human": "waiting_human", "blocked": "blocked"}.get(o.status, o.status)
    con.execute("UPDATE workflows SET status=?, updated_at=? WHERE id=?", (wstatus, now(), wf["id"]))
    con.commit()
    c.log(stage, f"{o.status} {o.message}".strip(), output=o.output)
    return o


def run(con, wid: int, services: Services, *, live: bool = False, until: str | None = None,
        should_stop: Callable[[], str | None] | None = None) -> dict:
    """Run from the first stage that is not done. Dry run (default) walks every stage and reports the plan.
    `should_stop()` is checked between stages (cooperative pause/cancel); a non-empty return stops the run there."""
    ensure_stages(con, wid)
    wf = get_workflow(con, wid)
    stages = KINDS[wf["kind"]]
    done = {r[0] for r in con.execute("SELECT stage FROM workflow_stages WHERE workflow_id=? AND status='done'",
                                      (wid,))}
    report = []
    for s in stages:
        if s in done:
            continue
        reason = should_stop() if should_stop else None
        if reason:
            report.append({"stage": s, "status": "stopped", "message": reason, "output": {}})
            break
        o = _exec(con, wf, s, services, live)
        report.append({"stage": s, "status": o.status, "message": o.message, "output": o.output})
        if live and o.status != "done":
            break
        if until and s == until:
            break
    if live and report and all(r["status"] == "done" for r in report) and (not until or until == stages[-1]):
        con.execute("UPDATE workflows SET status='completed', current_stage=NULL, updated_at=? WHERE id=?",
                    (now(), wid))
        con.commit()
    return {"workflow_id": wid, "live": live, "steps": report, "status": get_workflow(con, wid)["status"]}


def run_stage(con, wid: int, stage: str, services: Services, *, live: bool = False) -> dict:
    """Run one stage on its own (also re-runs a done stage, e.g. after new approvals)."""
    ensure_stages(con, wid)
    wf = get_workflow(con, wid)
    if stage not in KINDS[wf["kind"]]:
        raise ValueError(f"{stage!r} is not a stage of a {wf['kind']} workflow")
    o = _exec(con, wf, stage, services, live)
    sync_status(con, wid)
    return {"workflow_id": wid, "stage": stage, "status": o.status, "message": o.message, "output": o.output}


def sync_status(con, wid: int) -> None:
    """After a single-stage run, the workflow's status follows its first unfinished stage again (otherwise it stayed
    'running' with no job behind it, and nothing - neither a person nor the scheduler - could tell it was waiting)."""
    wf = get_workflow(con, wid)
    if wf["status"] in ("cancelled", "paused"):
        return
    r = con.execute("SELECT stage, status FROM workflow_stages WHERE workflow_id=? AND status NOT IN ('done','planned') "
                    "ORDER BY seq LIMIT 1", (wid,)).fetchone()
    if r is None:
        st, cur = "completed", wf["current_stage"]
    else:
        st = r["status"] if r["status"] in ("waiting_human", "blocked", "failed") else "paused"
        cur = r["stage"]
    con.execute("UPDATE workflows SET status=?, current_stage=?, updated_at=? WHERE id=?", (st, cur, now(), wid))
    con.commit()


def recover_interrupted(con) -> list[dict]:
    """Called when the API process starts: no job can be running yet, so any stage still marked 'running' was cut off
    by a crash or a restart. It is marked failed with that reason (its earlier results are kept; stages are
    idempotent), the workflow status follows it, and the event is logged, so Resume / Retry continues from there
    instead of the run hanging in 'running' forever."""
    out = []
    for r in con.execute("SELECT s.id, s.workflow_id, s.stage FROM workflow_stages s WHERE s.status='running'").fetchall():
        msg = "interrupted: the server stopped while this stage was running; press Resume or Retry to run it again"
        con.execute("UPDATE workflow_stages SET status='failed', error=?, finished_at=? WHERE id=?", (msg, now(), r["id"]))
        con.execute("INSERT INTO workflow_events(workflow_id,stage,level,message,at) VALUES(?,?,?,?,?)",
                    (r["workflow_id"], r["stage"], "error", msg, now()))
        out.append({"workflow_id": r["workflow_id"], "stage": r["stage"]})
    for (wid,) in con.execute("SELECT id FROM workflows WHERE status='running'").fetchall():
        sync_status(con, wid)
        if not any(o["workflow_id"] == wid for o in out):
            out.append({"workflow_id": wid, "stage": None})
    con.commit()
    return out


def reset_from(con, wid: int, stage: str) -> None:
    """Mark `stage` and every later stage pending again (data already produced is kept)."""
    wf = get_workflow(con, wid)
    seq = KINDS[wf["kind"]].index(stage)
    con.execute("UPDATE workflow_stages SET status='pending' WHERE workflow_id=? AND seq>=?", (wid, seq))
    con.execute("UPDATE workflows SET status='running', current_stage=?, updated_at=? WHERE id=?", (stage, now(), wid))
    con.commit()
    Ctx(con, wf, Services(), True).log(stage, f"reset from {stage}")


def events(con, wid: int, limit: int = 50) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM workflow_events WHERE workflow_id=? ORDER BY id DESC LIMIT ?",
                                         (wid, limit))][::-1]
