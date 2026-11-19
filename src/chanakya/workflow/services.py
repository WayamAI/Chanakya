"""Build orchestrator Services from the environment (shared by scripts/chanakya.py and the HTTP API)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from chanakya.workflow.orchestrator import ROOT, Services


def make_llm(env_file: Path | None = None, task: str = "company_research"):
    """The routed model for `task` (llm/routing.py), or None when that route's provider is not configured. There is
    no substitution: a Claude task never runs on Ollama and a Qwen task never runs on Claude. Ollama is used only for
    a task explicitly routed to it (CHANAKYA_ROUTE_<TASK>=ollama). None = deterministic steps still run."""
    from chanakya.llm import routing
    r = routing.route(task)
    if r.provider == "anthropic":
        if r.configured:
            return routing.AnthropicClient(r.model)
        print(f"(model route '{task}' not configured: {r.reason}; its AI step is skipped)", file=sys.stderr)
        return None
    if r.provider == "dashscope":
        return routing.QwenClient(r.model) if r.configured else None  # never another model in its place
    if not r.configured:
        print(f"(model route '{task}' not configured: {r.reason}; its AI step is skipped)", file=sys.stderr)
        return None
    from chanakya.config import ConfigError, load_settings
    try:
        s = load_settings(env_file or ROOT / ".env")
    except ConfigError as e:
        print(f"(LLM not configured: {e}; LLM steps are skipped, deterministic steps still run)", file=sys.stderr)
        return None
    from chanakya.llm.ollama_client import OllamaClient
    return OllamaClient(s)


def make_harness():
    from chanakya.resolver.harness_client import HarnessClient, HarnessError
    try:
        h = HarnessClient.from_env()
    except HarnessError:
        return None
    h.automation_allowed = linkedin_automation_allowed()
    return h


def linkedin_automation_allowed() -> bool:
    """settings.linkedin_automation == 'enabled' in the database (default disabled)."""
    import json
    import sqlite3
    path = os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data" / "chanakya.db"))
    try:
        con = sqlite3.connect(path, timeout=30)
        r = con.execute("SELECT value_json FROM settings WHERE key='linkedin_automation'").fetchone()
        con.close()
    except sqlite3.Error:
        return False
    return bool(r) and json.loads(r[0]) == "enabled"


def make_services(live: bool) -> Services:
    """LLM only for live runs; Clay factories are lazy (no connection until a confirmed live stage needs one);
    the PAID email lookup only with CHANAKYA_PAID_EMAIL_APPROVAL="reviewer|reason|max"."""
    from chanakya.clay import policy
    cache: dict = {}

    def llm_for(task: str):
        if task not in cache:
            cache[task] = make_llm(task=task)
        return cache[task]
    s = Services(llm=None, harness=make_harness(), llm_for=llm_for if live else None, real_sources=True)

    def factory(tools):
        def f():
            from chanakya.clay.mcp_client import ClayMCPClient
            return ClayMCPClient(allowed_tools=set(tools))
        return f
    s.clay_scout_factory = factory(policy.COMPANY_SCOUT)
    s.clay_people_factory = factory(policy.PEOPLE_FINDER)
    appr = os.getenv("CHANAKYA_PAID_EMAIL_APPROVAL", "").strip() or (_db_spend_approval() if live else "")
    if appr and live:
        from chanakya.emailhunter.clay import PaidEmailLookup, SpendApproval
        who, why, n = (appr.split("|") + ["", "", "1"])[:3]
        s.paid_email = PaidEmailLookup(factory(policy.EMAIL_HUNTER | policy.EMAIL_HUNTER_PAID),
                                       SpendApproval(who, why, int(n or 1)), os.getenv("CLAY_WORKSPACE_ID", "1461095"))
        print(f"PAID Clay email lookup ENABLED (approved by {who}, max {n})", file=sys.stderr)
    return s


def _db_spend_approval() -> str:
    """Active paid-email approval recorded in the UI (spend_approvals), as "reviewer|reason|remaining"; "" if none
    or exhausted. `remaining` = cap minus Clay email records created since the approval."""
    import sqlite3
    path = os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data" / "chanakya.db"))
    try:
        con = sqlite3.connect(path, timeout=30)
        r = con.execute("SELECT reviewer, reason, max_records, created_at FROM spend_approvals WHERE status='active' "
                        "AND kind='paid_email' ORDER BY id DESC LIMIT 1").fetchone()
        used = con.execute("SELECT COUNT(*) FROM email_records WHERE method='clay' AND created_at>=?",
                           (r[3],)).fetchone()[0] if r else 0
        con.close()
    except sqlite3.Error:
        return ""
    if not r or r[2] - used <= 0:
        return ""
    return f"{r[0]}|{r[1]}|{r[2] - used}"
