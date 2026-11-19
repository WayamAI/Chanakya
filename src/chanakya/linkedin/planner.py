"""Accelerator-led LinkedIn search plan: from one Wayam accelerator's catalogue entry to concrete LinkedIn searches.

Claude (route `accelerator_matching`, Sonnet) reads ONLY the catalogue record (summary, problems solved, target
industries, buyer roles, approved persona titles) and proposes company searches (who has the problem) and buyer
titles (who decides). Every query must be justified by a quoted catalogue phrase; queries without one are dropped.
Without a configured model, a rule-based plan is built from the same fields, and the plan says which was used.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field


class Query(BaseModel):
    query: str = Field(description="LinkedIn search keywords: company queries 1-3 industry words, titles a job title")
    why: str = Field(description="short reason")
    catalogue_quote: str = Field(description="verbatim phrase from the catalogue record that justifies it")


class Plan(BaseModel):
    company_queries: list[Query] = Field(description="2-4 searches for companies likely to have the problem")
    buyer_titles: list[Query] = Field(description="2-4 job titles of people who would buy or sponsor it")
    geography: str | None = Field(default=None, description="only if the catalogue names one")


def _j(x, d):
    try:
        v = json.loads(x) if isinstance(x, str) else x
        return v if v is not None else d
    except Exception:  # noqa: BLE001
        return d


def catalogue(con, product_id: str) -> dict:
    r = con.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if r is None:
        raise KeyError(product_id)
    titles = []
    for m in con.execute("SELECT titles_json FROM persona_role_mappings WHERE product_id=? AND status='approved'",
                         (product_id,)):
        titles += _j(m[0], [])
    return {"id": r["id"], "name": r["name"], "summary": r["summary"] or "",
            "problems_solved": _j(r["problems_solved_json"], []), "target_industries": _j(r["target_industries_json"], []),
            "industries_note": r["target_industries_note"], "buyer_roles": _j(r["buyer_roles_json"], []),
            "needs": _j(r["needs_json"], []), "approved_titles": sorted(set(titles)),
            "use_cases": _use_cases(con, product_id)}


def _use_cases(con, product_id: str) -> list[dict]:
    """Client and target use cases from the master sheet (industry, function, challenge): real context for planning."""
    if not con.execute("SELECT name FROM sqlite_master WHERE name='catalogue_use_cases'").fetchone():
        return []
    return [{"title": r[0], "industry": r[1], "function": r[2], "challenge": (r[3] or "")[:200], "kind": r[4]}
            for r in con.execute("SELECT title, industry, function, challenge, kind FROM catalogue_use_cases "
                                 "WHERE product_id=? ORDER BY kind, id LIMIT 8", (product_id,))]


def _text(c: dict) -> str:
    return json.dumps(c, ensure_ascii=False).lower()


def rule_plan(c: dict) -> dict:
    inds = [str(i) for i in c["target_industries"]][:3] or ["enterprise"]
    titles = (c["approved_titles"] or [str(b) for b in c["buyer_roles"]])[:4] or \
        ["Chief Technology Officer", "Head of Data", "Chief Digital Officer"]
    return {"source": "rules (no model configured)",
            "company_queries": [{"query": i, "why": "target industry in catalogue", "catalogue_quote": i} for i in inds],
            "buyer_titles": [{"query": t, "why": "buyer role in catalogue" if c["buyer_roles"] or c["approved_titles"]
                              else "generic technology buyer (catalogue has no buyer roles)", "catalogue_quote": t}
                             for t in titles]}


def plan(con, product_id: str, llm=None) -> dict:
    c = catalogue(con, product_id)
    if llm is None:
        return {"accelerator": c["name"], "product_id": product_id, **rule_plan(c)}
    from chanakya.llm.structured import ask_structured
    p = ask_structured(
        llm, Plan, agent="linkedin_planner", purpose="accelerator search plan", con=con,
        system="You plan LinkedIn searches for Wayam's business development. Use ONLY the catalogue record given. "
               "Company queries name KINDS of organisations that would have the problems this accelerator solves. "
               "LinkedIn company search matches company names and industry words, so each company query must be 1-3 "
               "plain words such as 'retail bank', 'insurance', 'NHS trust', 'logistics', 'pharmaceutical'; never "
               "product jargon like 'migration' or 'dashboards'. The why must link the industry to a catalogue "
               "problem. Buyer titles are real job titles. "
               "Every item must quote a phrase copied verbatim from the record. No invented capabilities or clients.",
        user=json.dumps(c, ensure_ascii=False))
    text = _text(c)
    keep = lambda qs: [q.model_dump() for q in qs if q.catalogue_quote.strip().lower()[:60] in text][:4]
    out = {"accelerator": c["name"], "product_id": product_id, "source": f"model {getattr(llm, 'model', '')}".strip(),
           "company_queries": keep(p.company_queries), "buyer_titles": keep(p.buyer_titles), "geography": p.geography}
    fb = rule_plan(c)
    for k in ("company_queries", "buyer_titles"):
        if not out[k]:
            out[k] = fb[k]
            out["source"] += f"; {k} from rules (model quotes not found in catalogue)"
    return out
