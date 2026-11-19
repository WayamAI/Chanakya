"""Agent 2 - Company Scout: discovery (Clay search) -> parse -> dedupe -> fit -> SQL Companies DB.

Consumes Agent 1's ICP. Spend controls:
  * Gateway allowlist = the approved discovery tools only; enrichment tools are held.
  * Live search requires allow_live_search=True AND a passing credit check, and is capped at 5.
  * Clay response shapes are parsed defensively; raw responses are always kept for audit.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from chanakya.catalog.models import ICP
from chanakya.clay import policy
from chanakya.scout.discovery import (CompanyCandidate, DiscoveryBlocked, DiscoveryProvider, DiscoveryRequest,
                                      DiscoveryResult, PreparedQuery)
from chanakya.scout.dsl import MAX_COMPANIES, ClaySchema, build_company_query
from chanakya.scout.qualification import assess_company, load_product_context
from chanakya.storage.db import finish_run, now, start_run

log = logging.getLogger(__name__)

# Company Scout's Clay allowlist. search-contacts belongs to People Finder and is NOT available here.
APPROVED_TOOLS = policy.COMPANY_SCOUT
HELD_TOOLS = policy.HELD


class SpendBlocked(DiscoveryBlocked):
    pass


class ClayToolError(RuntimeError):
    pass


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


def result_payload(res: Any) -> Any:
    """Extract JSON from an MCP CallToolResult (structured content preferred, else text blocks)."""
    if getattr(res, "is_error", False) or getattr(res, "isError", False):
        texts = [getattr(c, "text", "") for c in getattr(res, "content", [])]
        raise ClayToolError(" ".join(texts)[:500] or "Clay tool returned an error")
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if sc:
        return sc
    for c in getattr(res, "content", []) or []:
        t = getattr(c, "text", None)
        if t:
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return {"text": t}
    return {}


class ClayGateway:
    """The only path from Company Scout to Clay. Enforces the approved allowlist."""

    def __init__(self, client: ToolCaller):
        self._c = client

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        if name in HELD_TOOLS or name not in APPROVED_TOOLS:
            raise SpendBlocked(f"Clay tool '{name}' is held/not approved")
        return result_payload(await self._c.call_tool(name, args))

    async def assert_workspace(self, expected_id: int | str) -> dict:
        """Refuse to proceed unless the MCP connection is pinned to the expected Clay workspace."""
        w = await self.call("get-current-workspace", {})
        if str(w.get("workspaceId")) != str(expected_id):
            raise SpendBlocked(f"connected to workspace {w.get('workspaceId')!r}, expected {expected_id!r}")
        return w

    async def assert_credits_ok(self) -> dict:
        c = await self.call("get-credits-available", {})
        if c.get("exhaustedMessage") or not all(v for k, v in c.items() if k.startswith("has")):
            raise SpendBlocked(f"Clay credit check failed: {c}")
        return c


# ---------------- parsing ----------------
_DOMAIN_KEYS = ("domain", "website", "company_domain", "url", "website_url")


def normalize_domain(v: Any) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    d = re.sub(r"^[a-z]+://", "", v.strip().lower()).split("/")[0].split("?")[0]
    d = d.removeprefix("www.").rstrip(".")
    return d if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d) else None


def extract_companies(payload: Any) -> list[dict]:
    """Find company-like records anywhere in a Clay payload (shape not assumed)."""
    found: list[dict] = []

    def walk(x):
        if isinstance(x, dict):
            if any(normalize_domain(x.get(k)) for k in _DOMAIN_KEYS) and ("name" in x or "company_name" in x or "domain" in x):
                found.append(x); return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(payload)
    return found


def find_task_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for k in ("taskId", "task_id"):
            if isinstance(payload.get(k), str):
                return payload[k]
        for v in payload.values():
            t = find_task_id(v)
            if t:
                return t
    return None


def source_url(rec: dict) -> str | None:
    for k in ("linkedin_url", "linkedinUrl", "url", "website", "website_url"):
        v = rec.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    d = next((normalize_domain(rec.get(k)) for k in _DOMAIN_KEYS if normalize_domain(rec.get(k))), None)
    return f"https://{d}" if d else None


# ---------------- search-filter match (deterministic, explainable) ----------------
def _get(rec: dict, *keys):
    for k in keys:
        if rec.get(k) not in (None, "", []):
            return rec[k]
    return None


def _nv(v) -> str:
    """Normalised value for exact comparison: case-folded, whitespace collapsed."""
    return re.sub(r"\s+", " ", str(v)).strip().casefold()


def _has_token(text: str, phrase: str) -> bool:
    """Bounded phrase match: 'India' matches 'New Delhi, India' but not 'Indian' or 'British Indian Ocean'."""
    return re.search(r"(?<![\w-])" + re.escape(_nv(phrase)) + r"(?![\w-])", _nv(text)) is not None


def _record_countries(rec: dict) -> list[str] | None:
    """Structured country values only (exact values). Free-text locations are split on commas into segments."""
    vals: list[str] = []
    for k in ("country", "country_name", "location_country", "hq_country"):
        v = rec.get(k)
        vals += [x for x in (v if isinstance(v, list) else [v]) if isinstance(x, str) and x.strip()]
    for loc in rec.get("locations") or []:
        if isinstance(loc, dict):
            vals += [loc[k] for k in ("country_name", "country") if isinstance(loc.get(k), str) and loc[k].strip()]
    if not vals and isinstance(rec.get("location"), str):
        vals = [seg for seg in rec["location"].split(",") if seg.strip()]
    return vals or None


def _text_values(rec: dict) -> list[str]:
    out = []
    for v in rec.values():
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out += [x for x in v if isinstance(x, str)]
    return out


def assess_filter_match(rec: dict, icp: ICP, industry_values: list[str] | None = None,
                        source: str = "clay:search-companies") -> dict:
    """Did the record match the SEARCH FILTERS? Only data present in the record is scored; missing data is
    reported in `unknown`, never assumed. Comparisons are exact (normalised) values or bounded tokens.
    industry_values: approved provider-taxonomy values the ICP industry terms map to (e.g. Clay enum)."""
    checks, unknown = [], []

    def add(name, ok, observed, wanted):
        checks.append({"criterion": name, "pass": ok, "observed": observed, "wanted": wanted, "source": source})

    if icp.industries:
        ind = _get(rec, "industry", "industries")
        if ind is None:
            unknown.append("industry")
        else:
            vals = ind if isinstance(ind, list) else [ind]
            wanted = list(dict.fromkeys(list(icp.industries.values) + list(industry_values or [])))
            add("industry", any(_nv(v) in {_nv(w) for w in wanted} for v in vals), ind, wanted)
    if icp.countries:
        cs = _record_countries(rec)
        if cs is None:
            unknown.append("country")
        else:
            add("country", any(_nv(c) in {_nv(w) for w in icp.countries.values} for c in cs), cs, icp.countries.values)
    if icp.employee_min is not None or icp.employee_max is not None:
        n = _get(rec, "estimated_employee_count", "employee_count", "employees")
        if not isinstance(n, (int, float)) or isinstance(n, bool):
            unknown.append("employee_count")
        else:
            add("employee_count", (icp.employee_min is None or n >= icp.employee_min) and
                (icp.employee_max is None or n <= icp.employee_max), n, [icp.employee_min, icp.employee_max])
    if icp.keywords:
        desc = [rec[k] for k in ("description", "products_and_services") if isinstance(rec.get(k), str)]
        if not desc:
            unknown.append("description")
        else:
            hits = [k for k in icp.keywords.values if any(_has_token(d, k) for d in desc)]
            add("keywords", bool(hits), hits, icp.keywords.values)
    if icp.disqualifiers:
        texts = _text_values(rec)
        hit = [d for d in icp.disqualifiers.values if any(_has_token(t, d) for t in texts)]
        if hit:
            add("disqualifier", False, hit, "none of " + ", ".join(icp.disqualifiers.values))
    decided = [c for c in checks if c["pass"] is not None]
    score = round(100 * sum(bool(c["pass"]) for c in decided) / len(decided)) if decided else None
    return {"score": score, "checks": checks, "unknown": unknown,
            "note": "Score covers only search-filter criteria with observed data; see 'unknown'."}


assess_fit = assess_filter_match  # backward-compatible name; returns a SEARCH-FILTER match, not a fit


# ---------------- persistence with dedupe + provenance ----------------
def record_domain(rec: dict) -> str | None:
    return next((normalize_domain(rec.get(k)) for k in _DOMAIN_KEYS if normalize_domain(rec.get(k))), None)


def upsert_company(con, rec: dict, *, icp_id: int | None, fit: dict, tool: str, task_id: str | None,
                   query: str | None, provider: str = "clay", src_url: str | None = None,
                   domain: str | None = None, assessment: dict | None = None,
                   raw_response_id: int | None = None, run_id: int | None = None,
                   direction: str | None = None, workflow_id: int | None = None) -> tuple[int, bool]:
    """Upsert by domain. `fit` is the SEARCH-FILTER match (parameter name kept for compatibility).
    * data_json merges every observation (no provider field is dropped); each observation's verbatim record goes
      to company_sources, linked to its raw provider response and discovery run.
    * Human decisions (human_reviews table) always override the system default status, and are re-attached to the
      new assessment with reviewer, reason and timestamp.
    * Legacy fit_score/fit_json are not written (see legacy_fit_archive)."""
    from chanakya.scout.qualification import apply_human_decision
    domain = domain or record_domain(rec)
    name = rec.get("name") or rec.get("company_name")
    # Shared intelligence layer: route every company sighting through entity resolution first, so a company already
    # known from another direction (e.g. by name from a demand signal) is upgraded/merged instead of duplicated.
    from chanakya.intel import entities as _E
    li_url = rec.get("linkedin_url") or (rec.get("url") if "linkedin.com/company" in str(rec.get("url") or "") else None)
    _res = _E.resolve_company(con, name=name, domain=domain, linkedin_url=li_url,
                       direction=direction or (_E.BOTTOM if provider in ("linkedin-harness", "manual-import")
                                               else _E.TOP),
                       source=f"{provider}:{tool}", workflow_id=workflow_id, run_id=run_id, record=rec)
    t = now()
    base = assessment or {"search_filter_match": fit}
    row = con.execute("SELECT id, data_json FROM companies WHERE domain=?", (domain,)).fetchone()
    if row:
        a = apply_human_decision(con, row["id"], base)
        merged = {**json.loads(row["data_json"]), **{k: v for k, v in rec.items() if v not in (None, "", [])}}
        con.execute("UPDATE companies SET name=COALESCE(?,name), data_json=?, fit_score=NULL, fit_json=NULL, icp_id=?, "
                    "search_filter_json=?, search_filter_score=?, qualification_status=?, qualification_source=?, "
                    "assessment_json=?, last_seen=? WHERE id=?",
                    (name, json.dumps(merged), icp_id, json.dumps(a.get("search_filter_match", fit)),
                     a.get("search_filter_match", fit).get("score"), a["qualification_status"],
                     a["qualification_source"], json.dumps(a), t, row["id"]))
        cid, new = row["id"], _res["created"]  # created by entity resolution just above = a new company
    else:
        a = apply_human_decision(con, None, base)
        cur = con.execute("INSERT INTO companies(domain,name,data_json,icp_id,search_filter_json,search_filter_score,"
                          "qualification_status,qualification_source,assessment_json,first_seen,last_seen) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (domain, name, json.dumps(rec), icp_id, json.dumps(a.get("search_filter_match", fit)),
                           a.get("search_filter_match", fit).get("score"), a["qualification_status"],
                           a["qualification_source"], json.dumps(a), t, t))
        cid, new = cur.lastrowid, True
    con.execute("INSERT INTO company_sources(company_id,source,tool,clay_task_id,source_url,query,raw_json,fetched_at,"
                "raw_response_id,run_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cid, provider, tool, task_id, src_url or source_url(rec), query, json.dumps(rec), t, raw_response_id,
                 run_id))
    return cid, new


# ---------------- Clay provider ----------------
# Cross-entity filters "may require a paid plan" (search-contacts tool description). The Scout never emits them.
_PAID_RISK = re.compile(r"\b(people|jobs)\.(exists|count|any)\b|\bcompany\.\w+|\bexperiences\.any\b")


class ClaySearchProvider:
    name = "clay"
    cost_note = ("search-companies cost on this workspace is UNCONFIRMED (credit tool returns booleans only); "
                 "see docs/clay_cost_review.md")

    def __init__(self, gateway: ClayGateway | None, schema: ClaySchema, expected_workspace_id: int | str | None = None):
        self.gateway, self.schema = gateway, schema
        self.expected_workspace_id = expected_workspace_id

    def prepare(self, req: DiscoveryRequest) -> PreparedQuery:
        q = build_company_query(req.icp, self.schema, limit=req.limit, industry_mappings=req.industry_mappings)
        issues = list(q.pending_review)
        if q.text and _PAID_RISK.search(q.text):
            issues.append("query uses a cross-entity filter that may require a paid Clay plan")
        return PreparedQuery(self.name, q.text, ready=bool(q.text) and not issues, issues=issues,
                             unmapped=q.unmapped)

    async def discover(self, prepared: PreparedQuery, req: DiscoveryRequest) -> DiscoveryResult:
        if not prepared.ready:
            raise SpendBlocked("Live search refused: " + "; ".join(prepared.issues or ["query not ready"]))
        if self.gateway is None:
            raise SpendBlocked("no Clay gateway configured")
        if self.expected_workspace_id is not None:
            await self.gateway.assert_workspace(self.expected_workspace_id)
        await self.gateway.assert_credits_ok()
        responses: list[dict] = []

        def log_response(tool: str, payload, phase: str) -> None:
            entry = {"tool": tool, "task_id": find_task_id(payload), "phase": phase, "payload": payload}
            responses.append(entry)
            req.response_log.append(entry)  # survives a later failure

        initial = await self.gateway.call("search-companies", {"dslQuery": prepared.text})
        log_response("search-companies", initial, "initial")
        task_id = find_task_id(initial)
        recs, recs_idx = extract_companies(initial), 0
        # Per the tool description, terminal hosts read result values via get-task-context;
        # get-task is the documented fallback. One attempt each, no polling loop. Each follow-up is stored separately.
        for tool in ("get-task-context", "get-task"):
            if recs or not task_id:
                break
            try:
                follow = await self.gateway.call(tool, {"taskId": task_id})
            except SpendBlocked:
                raise
            except Exception as e:  # tool error / transport failure -> try the documented fallback
                log.warning("%s failed for %s: %s", tool, task_id, type(e).__name__)
                req.response_log.append({"tool": tool, "task_id": task_id, "phase": "follow_up_error",
                                         "payload": {"error": type(e).__name__, "message": str(e)[:500]}})
                responses.append(req.response_log[-1])
                continue
            log_response(tool, follow, "follow_up")
            recs = extract_companies(follow)
            if recs:
                recs_idx = len(responses) - 1
        src_tool = responses[recs_idx]["tool"] if responses else "search-companies"
        cands = []
        for rec in recs:
            d = record_domain(rec)
            if d:
                cands.append(CompanyCandidate(d, rec.get("name") or rec.get("company_name"), rec, self.name,
                                              "search-companies", source_url(rec), task_id))
        return DiscoveryResult(prepared, cands, raw=initial, responses=responses,
                               records_response_index=recs_idx if recs else None,
                               skipped=[f"record without valid domain: {str(r)[:60]}" for r in recs
                                        if not record_domain(r)] + ([] if src_tool == "search-companies" else
                                                                    [f"records read via {src_tool}"]))


@dataclass
class ScoutResult:
    query: str
    unmapped: list[str]
    pending_review: list[str] = field(default_factory=list)
    task_id: str | None = None
    inserted: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)
    companies: list[dict] = field(default_factory=list)
    dry_run: bool = True
    provider: str = "clay"


def approved_industry_values(mappings: list | None) -> list[str]:
    from chanakya.scout.industry_map import USABLE
    return [v for m in (mappings or []) if m.status in USABLE for v in m.canonical]


def _save_responses(con, provider_name: str, run_id: int, query: str, responses: list[dict],
                    phase_suffix: str = "") -> list[int]:
    ids = []
    for i, r in enumerate(responses):
        ids.append(con.execute(
            "INSERT INTO raw_responses(provider,tool,task_id,query,response_json,fetched_at,run_id,sequence,phase) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (provider_name, r.get("tool", "?"), r.get("task_id"), query, json.dumps(r.get("payload"), default=str),
             now(), run_id, i, (r.get("phase") or "") + phase_suffix)).lastrowid)
    return ids


def search_context(req: DiscoveryRequest, provider_name: str, prepared) -> dict:
    """Everything needed to re-score later exactly as at search time."""
    return {"provider": provider_name, "query": prepared.text, "icp_id": req.icp_id,
            "effective_icp": json.loads(req.icp.model_dump_json()),
            "industry_mappings": [{"id": m.id, "original_term": m.original_term, "canonical": m.canonical,
                                   "status": m.status} for m in (req.industry_mappings or [])],
            "approved_industry_values": approved_industry_values(req.industry_mappings), **req.context}


async def run_discovery(provider: DiscoveryProvider, req: DiscoveryRequest, con, *,
                        allow_live: bool = False) -> ScoutResult:
    """Provider-agnostic Scout pipeline: prepare -> (optionally) discover -> dedupe -> assess -> persist."""
    prepared = provider.prepare(req)
    res = ScoutResult(query=prepared.text, unmapped=prepared.unmapped, pending_review=prepared.issues,
                      provider=provider.name)
    if allow_live and not prepared.ready:
        raise SpendBlocked(f"{provider.name}: live discovery refused: " +
                           "; ".join(prepared.issues or ["query not ready"]))
    ctx = search_context(req, provider.name, prepared)
    run_id = start_run(con, "company_scout", {"provider": provider.name, "query": prepared.text, "live": allow_live,
                                              "search_context": ctx})
    try:
        if not allow_live:
            finish_run(con, run_id, "dry_run", {"query": prepared.text, "unmapped": prepared.unmapped,
                                                "issues": prepared.issues, "cost_note": provider.cost_note,
                                                "search_context": ctx})
            return res
        res.dry_run = False
        out = await provider.discover(prepared, req)
        responses = out.responses or [{"tool": "discover", "task_id": None, "phase": "initial", "payload": out.raw}]
        raw_ids = _save_responses(con, provider.name, run_id, prepared.text, responses)
        rec_raw_id = raw_ids[out.records_response_index] if out.records_response_index is not None else raw_ids[0]
        product = next((p for p in (load_product_context(con, n) for n in req.icp.offerings) if p), None)
        res.skipped += out.skipped
        res.task_id = next((c.task_id for c in out.candidates if c.task_id), None)
        seen: set[str] = set()
        approved = approved_industry_values(req.industry_mappings)
        for c in out.candidates[:req.limit]:
            if c.domain in seen:
                res.skipped.append(f"duplicate domain in results: {c.domain}"); continue
            seen.add(c.domain)
            fm = assess_filter_match(c.record, req.icp, approved, source=f"{c.provider}:{c.tool}")
            assessment = assess_company(c.record, req.icp, fm, product)
            _, new = upsert_company(con, c.record, icp_id=req.icp_id, fit=assessment["search_filter_match"],
                                    tool=c.tool, task_id=c.task_id, query=prepared.text, provider=c.provider,
                                    src_url=c.source_url, domain=c.domain, assessment=assessment,
                                    raw_response_id=rec_raw_id, run_id=run_id)
            res.inserted += new; res.updated += (not new)
            stored = con.execute("SELECT qualification_status, qualification_source FROM companies WHERE domain=?",
                                 (c.domain,)).fetchone()
            res.companies.append({"domain": c.domain, "name": c.name, "source_url": c.source_url,
                                  "search_filter_match": assessment["search_filter_match"],
                                  "qualification_status": stored["qualification_status"],
                                  "qualification_source": stored["qualification_source"],
                                  "assessment": assessment})
        con.commit()
        finish_run(con, run_id, "ok", {"provider": provider.name, "task_id": res.task_id, "inserted": res.inserted,
                                       "updated": res.updated, "skipped": res.skipped, "raw_response_ids": raw_ids,
                                       "raw_keys": list(out.raw)[:20] if isinstance(out.raw, dict) else None,
                                       "search_context": ctx})
        return res
    except Exception as e:
        con.rollback()
        # Keep whatever the provider returned before the failure, plus the error, for debugging and audit.
        saved = _save_responses(con, provider.name, run_id, prepared.text, list(req.response_log), "@failed_run")
        con.commit()
        finish_run(con, run_id, "error", {"provider": provider.name, "error_type": type(e).__name__,
                                          "error": str(e)[:1000], "raw_response_ids": saved,
                                          "responses_received": len(req.response_log), "search_context": ctx})
        raise


async def run_scout(icp: ICP, icp_id: int | None, con, gateway: ClayGateway | None, schema: ClaySchema, *,
                    allow_live_search: bool = False, limit: int = MAX_COMPANIES,
                    industry_mappings: list | None = None) -> ScoutResult:
    """Clay-backed Scout (kept for existing callers); delegates to run_discovery."""
    if allow_live_search and gateway is None:
        allow_live_search = False  # no gateway -> dry run, as before
    req = DiscoveryRequest(icp, icp_id, limit, industry_mappings)
    return await run_discovery(ClaySearchProvider(gateway, schema), req, con, allow_live=allow_live_search)
