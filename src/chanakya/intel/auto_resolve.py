"""Automatic domain resolution for name-only accounts, under strict rules (otherwise the account stays unresolved
and the existing human route, Accounts > Resolve, still applies).

Accept a Wikidata suggestion only when ALL hold:
  1. it is the top search hit and has no warnings (has a description; its label contains every word of the name),
  2. the label equals the account name (case/punctuation-insensitive),
  3. the official website answers live (HTTP < 400, robots respected by the fetcher),
  4. the homepage title or text contains the account name.
Every acceptance is recorded as an alias and a discovery event with its evidence (Wikidata item, website check).
Qualification is untouched: an auto-resolved account still needs a human to qualify it.
"""
from __future__ import annotations

import re

from chanakya.intel import entities as E


def _norm(s: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower().replace("&amp;", "and").replace("&", "and")))


def auto_resolve(con, company_ids: list[int] | None = None, *, fetcher, suggest=None, limit: int = 10,
                 workflow_id=None) -> dict:
    from chanakya.intel.domain_suggest import SuggestError, suggest_domains
    suggest = suggest or suggest_domains
    q = "SELECT id, name FROM companies WHERE identity_status='unresolved' AND name IS NOT NULL"
    args: list = []
    if company_ids:
        q += f" AND id IN ({','.join('?' * len(company_ids))})"
        args = list(company_ids)
    out = {"resolved": [], "left_unresolved": [], "errors": []}
    rows_ = con.execute(q + " ORDER BY id", args).fetchall()
    if company_ids:  # keep the caller's priority order (newly discovered companies first)
        pos = {c: i for i, c in enumerate(company_ids)}
        rows_.sort(key=lambda r: pos.get(r[0], len(pos)))
    for cid, name in rows_[:limit]:
        try:
            rows = suggest(name)
        except SuggestError as e:
            out["errors"].append({"company_id": cid, "error": str(e)[:200]})
            continue
        top = rows[0] if rows else None
        why = None
        if top is None:
            why = "no Wikidata item with an official website"
        elif top["search_rank"] != 1 or top["warnings"]:
            why = "top hit not clean: " + "; ".join(top["warnings"] or ["not the first search result"])
        elif _norm(top["label"]) != _norm(name):
            why = f"label '{top['label']}' is not exactly '{name}'"
        page = None
        if why is None:
            page = fetcher(f"https://{top['domain']}/")
            if not page.text or (page.status or 0) >= 400:
                why = f"website check failed (HTTP {page.status}, {page.error or 'no text'})"
            elif _norm(name) not in _norm(page.text[:200000]):
                why = "homepage does not mention the account name"
        if why:
            out["left_unresolved"].append({"company_id": cid, "name": name, "reason": why,
                                           "candidate": top and top["domain"]})
            continue
        d = E.norm_domain(top["domain"])
        owner = con.execute("SELECT id FROM companies WHERE domain=? AND id!=?", (d, cid)).fetchone()
        src = f"auto-resolution:wikidata:{top['qid']}"
        if owner:
            E.merge_companies(con, keep=owner[0], drop=cid, by="auto-resolver",
                              reason=f"same official website {d} ({top['source_url']})")
            kept = owner[0]
        else:
            con.execute("UPDATE companies SET domain=?, identity_status='resolved' WHERE id=?", (d, cid))
            E.add_alias(con, cid, "domain", d, src)
            kept = cid
        E.record_discovery(con, "company", kept, E.TOP, src, workflow_id=workflow_id,
                           detail={"domain": d, "wikidata": top["source_url"], "website": top["website"],
                                   "website_http": page.status, "rule": "exact label + live homepage names it"})
        out["resolved"].append({"company_id": kept, "name": name, "domain": d, "evidence": top["source_url"],
                                "merged_from": cid if owner else None})
    con.commit()
    return out
