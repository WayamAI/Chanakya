"""Agent 5 - PAID Clay email lookup (`add-contact-data-points` with dataPoints=[{type: "Email"}]).

Never used unless the caller builds a SpendApproval (named reviewer + reason + record cap, max 5). Each lookup:
  workspace pin check -> credit check -> add-contact-data-points(taskId, [Email], [entityId]) -> get-task-context
  -> parse an email for that entity. Both raw responses are stored in raw_responses (provider 'clay-email').
The response shape of this tool has NOT been observed live; the parser is defensive and the value is stored as
`unverified` unless the response itself carries an explicit verified/valid status. Nothing is ever retried.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from chanakya.clay import policy
from chanakya.emailhunter.checks import valid_syntax
from chanakya.scout.agent import SpendBlocked, result_payload
from chanakya.storage.db import now

MAX_APPROVED_RECORDS = 5
PROVIDER = "clay-email"


@dataclass
class SpendApproval:
    reviewer: str
    reason: str
    max_records: int = 1
    used: int = field(default=0)

    def __post_init__(self):
        if not self.reviewer.strip() or not self.reason.strip():
            raise SpendBlocked("a paid Clay email lookup needs a named reviewer and a reason")
        if not 1 <= self.max_records <= MAX_APPROVED_RECORDS:
            raise SpendBlocked(f"max_records must be 1..{MAX_APPROVED_RECORDS}")


class PaidEmailGateway:
    def __init__(self, client, approval: SpendApproval):
        self._c, self.approval = client, approval

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        if name in policy.EMAIL_HUNTER_PAID:
            if self.approval.used >= self.approval.max_records:
                raise SpendBlocked(f"spend approval cap reached ({self.approval.max_records})")
            self.approval.used += 1
        elif name not in policy.EMAIL_HUNTER:
            raise SpendBlocked(f"Clay tool '{name}' is not allowed for Email Hunter")
        return result_payload(await self._c.call_tool(name, args))


def enrichment_state(payload: Any, entity_id: str) -> str | None:
    """Clay's state for the entity's Email enrichment (in-progress, completed, failed ...), if stated."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            if any(str(x.get(k)) == entity_id for k in ("entityId", "entity_id")):
                for en in x.get("enrichments") or []:
                    if isinstance(en, dict) and "email" in str(en.get("name", "")).lower():
                        found.append(str(en.get("state") or ""))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(payload)
    return found[0] if found else None


def find_email_for(payload: Any, entity_id: str) -> tuple[str | None, str | None]:
    """Search a payload for the entity's record and an email-like value; return (email, status-if-stated)."""
    hits: list[tuple[str, str | None]] = []

    def walk(x, in_entity):
        if isinstance(x, dict):
            mine = in_entity or any(str(x.get(k)) == entity_id for k in ("entityId", "entity_id", "id"))
            if mine:  # Clay MCP shape: enrichments: [{name: "Email", state, value}]
                for en in x.get("enrichments") or []:
                    if isinstance(en, dict) and "email" in str(en.get("name", "")).lower():
                        v = en.get("value")
                        if isinstance(v, str) and v.strip().startswith("{"):  # Clay: '{"Work Email":"a@b.com"}'
                            try:
                                v = json.loads(v)
                            except ValueError:
                                pass
                        if isinstance(v, dict):
                            v = next((x for x in v.values() if isinstance(x, str) and "@" in x), None)
                        if isinstance(v, str) and valid_syntax(v.strip().lower()):
                            hits.append((v.strip().lower(), str(en.get("verification") or en.get("status") or "")))
            for k, v in x.items():
                if mine and isinstance(v, str) and "email" in k.lower() and valid_syntax(v.strip().lower()):
                    st = next((str(x[s]).lower() for s in ("emailStatus", "email_status", "status", "verification")
                               if isinstance(x.get(s), str)), None)
                    hits.append((v.strip().lower(), st))
                walk(v, mine)
        elif isinstance(x, list):
            for v in x:
                walk(v, in_entity)
    walk(payload, False)
    return hits[0] if hits else (None, None)


class PaidEmailLookup:
    """Sync facade used by run_email_hunter. `client_factory()` returns an async context manager with call_tool
    (normally ClayMCPClient(allowed_tools=EMAIL_HUNTER | EMAIL_HUNTER_PAID))."""

    def __init__(self, client_factory: Callable, approval: SpendApproval, expected_workspace_id: str):
        self.factory, self.approval, self.ws = client_factory, approval, str(expected_workspace_id)

    def lookup(self, con, cand, run_id: int | None):
        from chanakya.emailhunter.agent import Finding
        if self.approval.used >= self.approval.max_records:
            return None
        payloads = asyncio.run(self._lookup(cand))
        ids = [con.execute("INSERT INTO raw_responses(provider,tool,task_id,query,response_json,fetched_at,run_id,"
                           "sequence,phase) VALUES(?,?,?,?,?,?,?,?,?)",
                           (PROVIDER, tool, cand.clay_task_id, f"entity:{cand.clay_entity_id}",
                            json.dumps(p, default=str), now(), run_id, i, "paid")).lastrowid
               for i, (tool, p) in enumerate(payloads)]
        email, st = None, None
        for _, p in reversed(payloads):
            email, st = find_email_for(p, cand.clay_entity_id)
            if email:
                break
        ev = [{"kind": "clay_enrichment", "task_id": cand.clay_task_id, "entity_id": cand.clay_entity_id,
               "raw_response_ids": ids, "stated_status": st, "approved_by": self.approval.reviewer,
               "approval_reason": self.approval.reason}]
        if not email:
            return Finding(None, "clay", "not_found", 0.0, evidence=ev, raw_response_id=ids[-1] if ids else None)
        verified = bool(st and re.search(r"\b(valid|verified|deliverable)\b", st) and "un" not in st[:2])
        return Finding(email, "clay", "valid" if verified else "unverified", 0.8 if verified else 0.5, None, None,
                       ev, ids[-1])

    async def _lookup(self, cand) -> list[tuple[str, Any]]:
        out = []
        async with self.factory() as client:
            gw = PaidEmailGateway(client, self.approval)
            w = await gw.call("get-current-workspace", {})
            if str(w.get("workspaceId")) != self.ws:
                raise SpendBlocked(f"connected to workspace {w.get('workspaceId')!r}, expected {self.ws!r}")
            c = await gw.call("get-credits-available", {})
            if c.get("exhaustedMessage") or not all(v for k, v in c.items() if k.startswith("has")):
                raise SpendBlocked(f"Clay credit check failed: {c}")
            out.append(("add-contact-data-points", await gw.call("add-contact-data-points", {
                "taskId": cand.clay_task_id, "dataPoints": [{"type": "Email"}], "entityIds": [cand.clay_entity_id]})))
            out.extend(await poll_context(gw, cand.clay_task_id, cand.clay_entity_id))
        return out


async def poll_context(gw, task_id: str, entity_id: str, tries: int = None, wait: float = None) -> list:
    """get-task-context is free: read it until Clay finishes the Email enrichment (or tries run out)."""
    import os
    tries = tries or int(os.getenv("CHANAKYA_CLAY_POLL_TRIES", "8"))
    wait = float(os.getenv("CHANAKYA_CLAY_POLL_SECONDS", "5")) if wait is None else wait
    out = []
    for i in range(tries):
        p = await gw.call("get-task-context", {"taskId": task_id, "entityIds": [entity_id]})
        out.append(("get-task-context", p))
        if (enrichment_state(p, entity_id) or "").lower() not in ("in-progress", "pending", "queued", "running"):
            break
        if i + 1 < tries:
            await asyncio.sleep(wait)
    return out
