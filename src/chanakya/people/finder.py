"""Agent 3 - People Finder: companies (from Company Scout) + reviewed persona mappings -> people in SQLite.

Safety:
  * Input companies must be human-`qualified` in Company Scout (overridable only for tests/demos with an
    explicit flag). People Finder never qualifies companies itself.
  * Contact queries use only human-`approved` persona mappings; otherwise the run is blocked.
  * Live calls happen only with allow_live=True, through PeopleGateway (allowlist clay.policy.PEOPLE_FINDER).
    Cross-entity `company.*` filters (possible paid feature) are never generated, and are refused if present.
  * Every raw response is stored in raw_responses (also on failure); every person observation gets a
    person_sources row with the verbatim record. People are `unverified`; roles are `needs_manual_review`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from chanakya.clay import policy
from chanakya.people import CompanyRef
from chanakya.people.personas import USABLE, RoleMapping
from chanakya.scout.agent import SpendBlocked, ToolCaller, find_task_id, result_payload
from chanakya.storage.db import finish_run, now, start_run

MAX_PER_COMPANY = 3
MAX_COMPANIES_PER_CALL = 10  # Clay companyIdentifiers limit
PROVIDER = "clay-contacts"
_PAID_RISK = re.compile(r"\bcompany\.|\bpeople\.|\bjobs\.")


class PeopleBlocked(RuntimeError):
    pass


# ---------------- gateway ----------------
class PeopleGateway:
    """The only path from People Finder to Clay. Enforces the People Finder allowlist."""

    def __init__(self, client: ToolCaller):
        self._c = client

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        if name in policy.HELD or name not in policy.PEOPLE_FINDER:
            raise SpendBlocked(f"Clay tool '{name}' is held/not approved for People Finder")
        return result_payload(await self._c.call_tool(name, args))

    async def assert_workspace(self, expected_id: int | str) -> dict:
        w = await self.call("get-current-workspace", {})
        if str(w.get("workspaceId")) != str(expected_id):
            raise SpendBlocked(f"connected to workspace {w.get('workspaceId')!r}, expected {expected_id!r}")
        return w

    async def assert_credits_ok(self) -> dict:
        c = await self.call("get-credits-available", {})
        if c.get("exhaustedMessage") or not all(v for k, v in c.items() if k.startswith("has")):
            raise SpendBlocked(f"Clay credit check failed: {c}")
        return c


# ---------------- request / query ----------------
@dataclass
class PeopleRequest:
    companies: list[CompanyRef]
    mappings: list[RoleMapping]
    limit_per_company: int = 1
    context: dict = field(default_factory=dict)
    response_log: list = field(default_factory=list)


@dataclass
class ContactQuery:
    dsl: str
    company_identifiers: list[str]
    ready: bool
    issues: list[str] = field(default_factory=list)
    mapping_ids: list[int] = field(default_factory=list)

    def arguments(self) -> dict:
        return {"companyIdentifiers": self.company_identifiers, "companyTenure": "current", "dslQuery": self.dsl}


def _q(values: list[str]) -> str:
    return "(" + ", ".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values) + ")"


def build_contact_query(req: PeopleRequest) -> ContactQuery:
    issues = []
    usable = [m for m in req.mappings if m.status in USABLE]
    if not usable:
        issues.append("needs_human_review: no approved persona mapping")
    doms = [c.domain for c in req.companies if c.domain]
    if not doms:
        issues.append("no input companies")
    if len(doms) > MAX_COMPANIES_PER_CALL:
        issues.append(f"too many companies for one call ({len(doms)} > {MAX_COMPANIES_PER_CALL})")
    n = req.limit_per_company
    if not isinstance(n, int) or not 1 <= n <= MAX_PER_COMPANY:
        issues.append(f"limit_per_company must be 1..{MAX_PER_COMPANY}")
    if issues:
        return ContactQuery("", doms, False, issues)
    clauses = []
    for m in usable:
        parts = []
        if m.titles:
            parts.append(f"job_title is_similar_to {_q(m.titles)}")
        if m.seniorities:
            parts.append(f"seniority in {_q(m.seniorities)}")
        clauses.append("(" + " and ".join(parts) + ")")
    dsl = (f"select from people where experiences.any(is_current = true and ({' or '.join(clauses)})) "
           f"limit {n} by clay_company_id")
    if _PAID_RISK.search(dsl.replace("clay_company_id", "")):
        return ContactQuery("", doms, False, ["cross-entity filter detected (may require a paid plan)"])
    return ContactQuery(dsl, doms, True, [], [m.id for m in usable])


# ---------------- provider ----------------
@dataclass
class PersonCandidateRecord:
    company_domain: str | None
    record: dict


@dataclass
class PeopleResult:
    candidates: list[PersonCandidateRecord]
    responses: list[dict]
    records_response_index: int = 0


@runtime_checkable
class PeopleProvider(Protocol):
    name: str
    cost_note: str

    async def discover(self, query: ContactQuery, req: PeopleRequest) -> PeopleResult: ...


class ClayContactsProvider:
    name = PROVIDER
    cost_note = ("search-contacts: per-search cost on this workspace is not confirmed; cross-entity filters are "
                 "never generated. Requires explicit approval to run live.")

    def __init__(self, gateway: PeopleGateway, expected_workspace_id: int | str):
        self.gw, self.ws = gateway, expected_workspace_id

    async def discover(self, query: ContactQuery, req: PeopleRequest) -> PeopleResult:
        await self.gw.assert_workspace(self.ws)
        await self.gw.assert_credits_ok()
        payload = await self.gw.call("search-contacts", query.arguments())
        resp = {"tool": "search-contacts", "task_id": find_task_id(payload), "phase": "initial", "payload": payload}
        req.response_log.append(resp)
        return PeopleResult(extract_people(payload, [c.domain for c in req.companies]), [resp])


# ---------------- parsing ----------------
_NAME_KEYS = ("full_name", "fullName", "name")
_LI_KEYS = ("linkedin_url", "linkedinUrl", "profile_url", "url")
_TITLE_KEYS = ("job_title", "title", "jobTitle", "latest_experience_title", "headline")
_DOMAIN_KEYS = ("company_domain", "companyDomain", "domain", "company_website")


def _first(rec: dict, keys) -> Any:
    return next((rec[k] for k in keys if isinstance(rec.get(k), str) and rec[k].strip()), None)


def normalize_linkedin(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    m = re.search(r"linkedin\.com/in/([A-Za-z0-9\-_%]+)", v.strip(), re.I)
    return f"https://www.linkedin.com/in/{m.group(1).lower()}" if m else None


def _norm_domain(v: Any) -> str | None:
    from chanakya.scout.agent import normalize_domain
    return normalize_domain(v)


def extract_people(payload: Any, company_domains: list[str]) -> list[PersonCandidateRecord]:
    """Find person-like records anywhere in a payload. Response shape is NOT assumed (no real payload seen yet)."""
    out: list[PersonCandidateRecord] = []

    def walk(x):
        if isinstance(x, dict):
            if _first(x, _NAME_KEYS) and (_first(x, _TITLE_KEYS) or normalize_linkedin(_first(x, _LI_KEYS)) or
                                          isinstance(x.get("experiences"), list)):
                out.append(PersonCandidateRecord(_company_of(x, company_domains), x))
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(payload)
    return out


def _company_of(rec: dict, company_domains: list[str]) -> str | None:
    d = _norm_domain(_first(rec, _DOMAIN_KEYS))
    if not d:
        for e in rec.get("experiences") or []:
            if isinstance(e, dict) and e.get("is_current") is not False:
                d = _norm_domain(_first(e, _DOMAIN_KEYS)) or _norm_domain((e.get("company") or {}).get("domain")
                                                                           if isinstance(e.get("company"), dict) else None)
                if d:
                    break
    if d in company_domains:
        return d
    return company_domains[0] if d is None and len(company_domains) == 1 else None


def current_role(rec: dict) -> tuple[str | None, str | None, bool | None]:
    for e in rec.get("experiences") or []:
        if isinstance(e, dict) and e.get("is_current"):
            return _first(e, ("job_title", "title")), e.get("seniority"), True
    return _first(rec, ("job_title", "title", "jobTitle", "latest_experience_title")), rec.get("seniority"), None


def _has_token(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])", text.lower()) is not None


def match_role(title: str | None, seniority: str | None, mappings: list[RoleMapping]) -> dict:
    """Local, deterministic check of the observed role against the approved mappings. Clay's is_similar_to expands
    titles, so a local non-match is `not_confirmed_locally`, never a rejection."""
    hits = [m.id for m in mappings if m.status in USABLE and (
        (title and any(_has_token(title, t) for t in m.titles)) or (seniority and seniority in m.seniorities))]
    return {"status": "matches_approved_role" if hits else "not_confirmed_locally", "mapping_ids": hits,
            "title": title, "seniority": seniority}


_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "professor", "sir", "dame", "lord", "lady", "baroness",
               "rt", "hon", "cbe", "obe", "mbe", "kbe", "dbe", "kcb", "cb", "fba", "frsa", "frs", "freng", "qc", "kc",
               "phd", "mba", "jr", "sr"}


def normalize_name(n: str) -> str:
    """Lower-case, punctuation-free, without titles and post-nominals, so "Professor Julia Black CBE FBA" and
    "Julia Black" are the same person at the same company."""
    toks = re.sub(r"[^\w\s]", " ", n.lower()).split()
    core = [t for t in toks if t not in _HONORIFICS]
    return " ".join(core if len(core) >= 2 else toks)


# ---------------- persistence ----------------
def upsert_person(con, rec: dict, company_id: int, *, mappings: list[RoleMapping], run_id: int | None,
                  raw_response_id: int | None, tool: str, task_id: str | None, query: str,
                  source: str = PROVIDER) -> tuple[int, bool]:
    """Dedupe: LinkedIn profile URL if present (global); else normalized name + company (never merged across
    companies without a profile URL). The verbatim record is always kept in person_sources."""
    name = " ".join(_first(rec, _NAME_KEYS).split())
    li = normalize_linkedin(_first(rec, _LI_KEYS))
    t = now()
    row = con.execute("SELECT id, linkedin_url FROM people WHERE linkedin_url=?", (li,)).fetchone() if li else None
    name_key = f"name:{normalize_name(name)}|company:{company_id}"
    if row is None:
        r2 = con.execute("SELECT id, linkedin_url FROM people WHERE dedupe_key=?", (name_key,)).fetchone()
        if r2 is not None and (li is None or r2["linkedin_url"] in (None, li)):
            row = r2
    if row is None and li is None:  # same name already at this company under a LinkedIn key
        for r3 in con.execute("SELECT DISTINCT p.id, p.full_name, p.linkedin_url FROM people p JOIN person_roles r "
                              "ON r.person_id=p.id WHERE r.company_id=?", (company_id,)):
            if normalize_name(r3["full_name"]) == normalize_name(name):
                row = r3
                break
    created = row is None
    if created:
        key, basis = (f"linkedin:{li}", "linkedin_url") if li else (name_key, "name_and_company")
        pid = con.execute("INSERT INTO people(dedupe_key,dedupe_basis,full_name,linkedin_url,first_seen,last_seen) "
                          "VALUES(?,?,?,?,?,?)", (key, basis, name, li, t, t)).lastrowid
    else:
        pid = row["id"]
        con.execute("UPDATE people SET last_seen=?, linkedin_url=COALESCE(linkedin_url, ?) WHERE id=?", (t, li, pid))
    from chanakya.intel import entities as _E  # discovery provenance: which direction found this person
    _E.record_discovery(con, "person", pid, _E.BOTTOM if source in ("linkedin-harness", "manual-import") else _E.TOP,
                        source, run_id=run_id, detail={"company_id": company_id, "created": created,
                                                        "dedupe": "linkedin_url" if li else "name_and_company"})
    title, seniority, is_current = current_role(rec)
    rm = json.dumps(match_role(title, seniority, mappings))
    con.execute("INSERT INTO person_roles(person_id,company_id,title,seniority,is_current,role_match_json,"
                "first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(person_id,company_id,title) DO UPDATE SET "
                "last_seen=excluded.last_seen, seniority=excluded.seniority, role_match_json=excluded.role_match_json",
                (pid, company_id, title, seniority, None if is_current is None else int(is_current), rm, t, t))
    con.execute("INSERT INTO person_sources(person_id,company_id,run_id,raw_response_id,source,tool,task_id,query,"
                "raw_json,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pid, company_id, run_id, raw_response_id, source, tool, task_id, query, json.dumps(rec), t))
    return pid, created


def load_company_refs(con, domains: list[str], *, require_qualified: bool = True) -> tuple[list[CompanyRef], list[str]]:
    refs, issues = [], []
    for d in domains:
        r = con.execute("SELECT id, domain, name, qualification_status, qualification_source FROM companies "
                        "WHERE domain=?", (d,)).fetchone()
        if r is None:
            issues.append(f"{d}: not in the Companies DB (People Finder only consumes Company Scout output)")
        elif require_qualified and not (r["qualification_status"] == "qualified" and
                                        r["qualification_source"] == "human"):
            issues.append(f"{d}: company status is {r['qualification_status']} ({r['qualification_source']}); "
                          "a human must qualify it before contact search")
        else:
            refs.append(CompanyRef(r["id"], r["domain"], r["name"]))
    return refs, issues


@dataclass
class PeopleRunResult:
    run_id: int
    query: ContactQuery
    dry_run: bool = True
    inserted: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)


def _save_responses(con, run_id, query, responses, suffix="") -> list[int]:
    return [con.execute("INSERT INTO raw_responses(provider,tool,task_id,query,response_json,fetched_at,run_id,"
                        "sequence,phase) VALUES(?,?,?,?,?,?,?,?,?)",
                        (PROVIDER, r.get("tool", "?"), r.get("task_id"), query, json.dumps(r.get("payload"), default=str),
                         now(), run_id, i, (r.get("phase") or "") + suffix)).lastrowid
            for i, r in enumerate(responses)]


async def run_people_finder(provider: PeopleProvider | None, req: PeopleRequest, con, *,
                            allow_live: bool = False) -> PeopleRunResult:
    q = build_contact_query(req)
    ctx = {"provider": PROVIDER, "query": q.dsl, "arguments": q.arguments(), "mapping_ids": q.mapping_ids,
           "companies": [c.domain for c in req.companies], **req.context}
    run_id = start_run(con, "people_finder", ctx)
    res = PeopleRunResult(run_id, q)
    if not q.ready or not allow_live or provider is None:
        status = "blocked" if not q.ready else "dry_run"
        finish_run(con, run_id, status, {"issues": q.issues, "search_context": ctx,
                                         "cost_note": getattr(provider, "cost_note", None)})
        if allow_live and not q.ready:
            raise PeopleBlocked("; ".join(q.issues))
        return res
    res.dry_run = False
    by_domain = {c.domain: c for c in req.companies}
    try:
        out = await provider.discover(q, req)
        raw_ids = _save_responses(con, run_id, q.dsl, out.responses)
        rec_raw = raw_ids[out.records_response_index] if raw_ids else None
        task_id = next((r.get("task_id") for r in out.responses if r.get("task_id")), None)
        counts: dict[int, int] = {}
        for c in out.candidates:
            comp = by_domain.get(c.company_domain)
            if comp is None:
                res.skipped.append(f"person not attributable to an input company: {_first(c.record, _NAME_KEYS)}")
                continue
            if counts.get(comp.company_id, 0) >= req.limit_per_company:
                res.skipped.append(f"over per-company limit at {comp.domain}")
                continue
            counts[comp.company_id] = counts.get(comp.company_id, 0) + 1
            pid, new = upsert_person(con, c.record, comp.company_id, mappings=req.mappings, run_id=run_id,
                                     raw_response_id=rec_raw, tool="search-contacts", task_id=task_id, query=q.dsl,
                                     source=provider.name)
            res.inserted += new; res.updated += (not new)
            res.people.append({"person_id": pid, "company": comp.domain, "name": _first(c.record, _NAME_KEYS)})
        con.commit()
        finish_run(con, run_id, "ok", {"inserted": res.inserted, "updated": res.updated, "skipped": res.skipped,
                                       "raw_response_ids": raw_ids, "search_context": ctx})
        return res
    except Exception as e:
        con.rollback()
        saved = _save_responses(con, run_id, q.dsl, list(req.response_log), "@failed_run")
        con.commit()
        finish_run(con, run_id, "error", {"error_type": type(e).__name__, "error": str(e)[:1000],
                                          "raw_response_ids": saved, "search_context": ctx})
        raise
