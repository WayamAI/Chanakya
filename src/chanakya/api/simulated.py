"""SIMULATED external services for UI / end-to-end tests. Enabled ONLY with CHANAKYA_SIMULATED_EXTERNALS=1.

The real API, orchestrator, agents, review gates and SQLite run unchanged; only the external clients are replaced:
Clay MCP (search-companies / search-contacts), the LinkedIn harness subprocess, company websites and DNS.
All data is SYNTHETIC (.example domains, invented names) and the UI shows "Demo data" while this mode is on.
No LLM is simulated: LLM steps run only if a real OLLAMA key is configured, otherwise they are skipped (honestly).
Refuses to start on a database that already holds non-simulated products, so it can never mix into real data.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

from chanakya.catalog.models import ICP, Criterion, Evidence
from chanakya.emailhunter.checks import MXResult, Page
from chanakya.resolver.harness_client import CompletedRun, HarnessClient
from chanakya.storage.db import now
from chanakya.workflow.orchestrator import Services

PRODUCT = "PRD-SIM-FINOPS"
SRC = "SIMULATED (synthetic test data, not Wayam)"
INDUSTRY = "IT System Custom Software Development"


def enabled() -> bool:
    return os.getenv("CHANAKYA_SIMULATED_EXTERNALS", "") == "1"


def seed(con) -> None:
    """Synthetic catalogue: one product, its ICP (industry + country), one approved persona mapping."""
    real = con.execute("SELECT COUNT(*) FROM products WHERE id!=?", (PRODUCT,)).fetchone()[0]
    if real:
        raise RuntimeError("CHANAKYA_SIMULATED_EXTERNALS=1 refuses to run on a database with real products; "
                           "point CHANAKYA_DB_PATH at an empty test database")
    if con.execute("SELECT 1 FROM products WHERE id=?", (PRODUCT,)).fetchone():
        return
    summary = "Simulated finance-operations accelerator that cuts reconciliation time for finance teams."
    con.execute("INSERT INTO products(id,name,status,summary,needs_json,buyer_roles_json,verification,source_path,"
                "source_sha256,raw_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (PRODUCT, "SimFinOps (synthetic)", "active", summary,
                 json.dumps([{"quote": "cuts reconciliation time", "kind": "capability"}]), json.dumps(["CFO"]),
                 "unverified", SRC, "sim", "{}", now()))
    ev = [Evidence(source_path=SRC, quote="synthetic")]
    icp = ICP(industries=Criterion(values=[INDUSTRY], evidence=ev), countries=Criterion(values=["India"], evidence=ev),
              offerings=["SimFinOps (synthetic)"])
    icp_id = con.execute("INSERT INTO icps(version,icp_json,source_shas,created_at,content_sha256,product_id) "
                         "VALUES(1,?,?,?,?,?)", (icp.model_dump_json(), "{}", now(), "sim-icp", PRODUCT)).lastrowid
    mid = con.execute("INSERT INTO persona_role_mappings(icp_id,product_id,persona,persona_source,titles_json,"
                      "seniorities_json,status,evidence_json,created_at,reviewed_by,reviewed_at,reason) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (icp_id, PRODUCT, "Finance leader", "human",
                       json.dumps(["Chief Financial Officer", "Head of Finance"]), json.dumps(["C-suite", "Head"]),
                       "approved", json.dumps([{"text": "synthetic seed"}]), now(), "simulation-seed",
                       now(), "synthetic seed for UI tests")).lastrowid
    con.execute("INSERT INTO persona_mapping_history(mapping_id,action,from_status,to_status,snapshot_json,reviewer,"
                "reason,evidence_json,at) VALUES(?,?,?,?,?,?,?,?,?)",
                (mid, "approve", "pending", "approved", "{}", "simulation-seed", "synthetic seed", "[]", now()))
    con.commit()


# ---------------- fake Clay ----------------
COMPANY = {"name": "Acme Analytics (synthetic)", "domain": "acme-analytics.example", "industry": INDUSTRY,
           "country": "India", "size": "51-200", "url": "https://www.linkedin.com/company/acme-analytics-sim",
           "description": "Synthetic company used by the simulated end-to-end test.", "entityId": "sim-co-1"}
CONTACT = {"full_name": "Asha Simulated", "job_title": "Chief Financial Officer", "entityId": "sim-person-1",
           "experiences": [{"job_title": "Chief Financial Officer", "seniority": "C-suite", "is_current": True,
                            "company_domain": COMPANY["domain"]}]}


class FakeClay:
    def __init__(self):
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def call_tool(self, name, args):
        self.calls.append(name)
        payloads = {"get-current-workspace": {"workspaceId": int(os.getenv("CLAY_WORKSPACE_ID", "1461095"))},
                    "get-credits-available": {"hasCredits": True, "hasActions": True},
                    "search-companies": {"taskId": "mcp-task_SIMULATED_CO", "companies": [COMPANY]},
                    "search-contacts": {"taskId": "mcp-task_SIMULATED_PEOPLE", "results": [CONTACT]}}
        if name not in payloads:
            raise RuntimeError(f"simulated Clay has no tool {name}")
        return SimpleNamespace(is_error=False, structured_content=payloads[name], content=[])


# ---------------- fake harness ----------------
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _ok(data):
    return {"ok": True, "data": data, "saved": "outputs/SIMULATED.json"}


def harness_runner(argv, cwd, timeout):
    cmd, arg = argv[3], (argv[4] if len(argv) > 4 else "")
    if cmd == "status":
        out = _ok({"url": "https://www.linkedin.com/feed/", "logged_in": True})
    elif cmd == "resolve":
        url = f"https://www.linkedin.com/in/sim-{_slug(arg)}/"
        out = _ok({"input": {"name": arg}, "status": "found", "profile_url": url, "confidence": "high",
                   "best": {"url": url, "title": arg, "lines": ["Chief Financial Officer"], "score": 0.95},
                   "alternatives": [{"url": f"https://www.linkedin.com/in/sim-{_slug(arg)}-2/", "title": arg,
                                     "lines": ["Student"], "score": 0.4}], "searches": ["SIMULATED"]})
    elif cmd == "research":
        url = arg.rstrip("/") + "/"
        post = "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000099/"
        out = _ok({"profile_url": url, "name": "Simulated Person", "headline_block": ["Chief Financial Officer"],
                   "about": ["Leads finance transformation and audit readiness."], "education": [],
                   "experience": ["Chief Financial Officer", "Synthetic Co · Full-time", "2021 - Present",
                                  "Responsible for month-end close and treasury."],
                   "posts": [{"type": "post", "approx_date": "2026-10-20", "exact_date": None, "in_window": True,
                              "text": "Our month-end close still takes 9 days; reconciliation is the bottleneck.",
                              "url": post, "source_page": url, "reactions": 3, "comments": 0, "reposts": 0}],
                   "comments": [], "window_days": 90, "counts": {"posts": 1, "comments": 0},
                   "evidence_screenshots": [], "collected_at": now(), "pages_loaded": 4, "clicks": []})
    elif cmd == "people":
        out = _ok({"kind": "people", "query": arg, "search_url": "SIMULATED", "new_count": 2, "results": [
            {"url": "https://www.linkedin.com/in/sim-ravi-example/", "title": "Ravi Example (synthetic)",
             "lines": [f"{arg} at Beta Foods", "Mumbai"], "new": True},
            {"url": "https://www.linkedin.com/in/sim-meera-sample/", "title": "Meera Sample (synthetic)",
             "lines": ["Graphic designer", "Delhi"], "new": True}]})
    elif cmd == "profile":
        out = _ok({"url": arg, "name": "Ravi Example (synthetic)", "top": ["Ravi Example", "Head of Finance"],
                   "sections": {"Experience": ["Head of Finance", "Beta Foods Pvt Ltd · Full-time", "2022 - Present"]},
                   "recent_post_urls": []})
    elif cmd == "companies":
        out = _ok({"kind": "companies", "query": arg, "search_url": "SIMULATED", "new_count": 1, "results": [
            {"url": "https://www.linkedin.com/company/beta-foods-sim/", "title": "Beta Foods", "lines": ["Food"]}]})
    elif cmd == "company":
        out = _ok({"url": arg, "title": "Beta Foods | About",
                   "text": "Overview\nSynthetic snack maker.\nWebsite\nhttps://www.betafoods.example\nIndustry\nFood"})
    else:
        out = {"ok": False, "error": f"simulated harness: {cmd} not supported"}
    return CompletedRun(0 if out["ok"] else 1, "SIMULATED\nJSON: " + json.dumps(out), "")


def harness() -> HarnessClient:
    d = Path(tempfile.gettempdir()) / "chanakya-simulated-harness"
    d.mkdir(exist_ok=True)
    (d / "linkedin_harness.py").write_text("# SIMULATED harness placeholder (never executed)\n", encoding="utf-8")
    return HarnessClient(d, runner=harness_runner)


# ---------------- fake web + DNS ----------------
def fetcher(url: str) -> Page:
    if url.rstrip("/").endswith("acme-analytics.example/team"):
        return Page(url, 200, "<p>Asha Simulated, CFO - asha.simulated@acme-analytics.example</p>", True)
    return Page(url, 404, "", True)


def mx(domain: str) -> MXResult:
    return MXResult("ok", ["mx." + domain], "simulated")


def services_factory(live: bool) -> Services:
    from chanakya.workflow.services import make_llm
    lock = Path(tempfile.gettempdir()) / "chanakya-simulated.lock"
    return Services(llm=None, llm_for=(lambda t: make_llm(task=t)) if live and os.getenv("CHANAKYA_SIMULATED_LLM") == "1" else None, harness=harness(),
                    clay_scout_factory=FakeClay, clay_people_factory=FakeClay, fetcher=fetcher, mx_lookup=mx,
                    lock_path=lock)
