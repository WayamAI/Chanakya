"""LinkedIn Resolver through Clay (authorised provider), used when LinkedIn browser automation is off.

For people with a human-approved role but no LinkedIn URL, Clay's `search-contacts-by-name` looks up
"<full name> at <company domain>" in Clay's licensed people data. A profile is proposed only when the returned
name matches (honorific- and order-insensitive) AND the person's current employer domain equals the company's
confirmed domain. Every proposal is stored in linkedin_resolutions as `pending_review` with the raw Clay record;
a human approves it on Review > LinkedIn, which is what writes people.linkedin_url. Nothing touches linkedin.com.

One call resolves up to 10 people and counts as one Clay people search against the daily cap.
"""
from __future__ import annotations

import asyncio
import json

from chanakya.people.finder import normalize_linkedin, normalize_name
from chanakya.resolver.agent import select_candidates, store_resolution
from chanakya.scout.agent import result_payload
from chanakya.storage.db import finish_run, start_run

TOOLS = frozenset({"get-current-workspace", "get-credits-available", "search-contacts-by-name", "get-task-context"})


def _contacts(payload) -> list[dict]:
    if isinstance(payload, dict):
        c = payload.get("contacts")
        if isinstance(c, list):
            return [x for x in c if isinstance(x, dict)]
    return []


def match(cand_name: str, domain: str, contacts: list[dict]) -> dict | None:
    want = normalize_name(cand_name)
    for x in contacts:
        dom = (x.get("domain") or x.get("companyIdentifier") or "").lower().removeprefix("www.")
        if normalize_name(x.get("name") or "") == want and dom == domain.lower() and x.get("url"):
            return x
    return None


def run(con, client_factory, *, workspace_id: str, limit: int = 5, person_ids: list[int] | None = None) -> dict:
    cands = []
    for c in select_candidates(con, person_ids=person_ids):
        d = con.execute("SELECT domain, identity_status FROM companies WHERE id=?", (c.company_id,)).fetchone()
        if d and d["identity_status"] == "resolved":
            cands.append((c, d["domain"]))
    cands = cands[:max(1, min(limit, 10))]
    if not cands:
        return {"candidates": 0, "note": "nobody with an approved role, a confirmed company website and no URL"}
    args = {"contactIdentifiers": [{"contactName": c.full_name, "companyIdentifier": dom} for c, dom in cands]}
    run_id = start_run(con, "linkedin_resolver_clay", {"arguments": args})

    async def go():
        async with client_factory() as cl:
            ws = result_payload(await cl.call_tool("get-current-workspace", {}))
            if str(ws.get("workspaceId")) != str(workspace_id):
                raise RuntimeError(f"connected to Clay workspace {ws.get('workspaceId')!r}, expected {workspace_id!r}")
            first = result_payload(await cl.call_tool("search-contacts-by-name", args))
            tid = first.get("taskId") if isinstance(first, dict) else None
            ctx = result_payload(await cl.call_tool("get-task-context", {"taskId": tid})) if tid else first
            return first, ctx, tid
    try:
        first, ctx, tid = asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        finish_run(con, run_id, "error", {"error": f"{type(e).__name__}: {str(e)[:500]}"})
        raise
    contacts = _contacts(ctx) or _contacts(first)
    out = {"candidates": len(cands), "proposed": 0, "not_found": 0, "resolution_ids": [], "task_id": tid}
    for c, dom in cands:
        x = match(c.full_name, dom, contacts)
        fields = ({"status": "found", "confidence": "high", "score": 1.0, "profile_url": normalize_linkedin(x["url"]),
                   "best": {"name": x.get("name"), "title": x.get("latest_experience_title"),
                            "company": x.get("latest_experience_company"), "domain": dom, "url": x.get("url"),
                            "provider": "clay:search-contacts-by-name", "task_id": tid}}
                  if x else {"status": "not_found", "confidence": None, "error_kind": "no_match",
                             "error": "Clay returned no record with the same name at this company's domain"})
        rid = store_resolution(con, c, None, fields, run_id, argv=["clay", "search-contacts-by-name"],
                               version="clay-mcp", raw={"task_id": tid, "match": x})
        out["resolution_ids"].append(rid)
        out["proposed" if x else "not_found"] += 1
    finish_run(con, run_id, "ok", out)
    con.commit()
    return out
