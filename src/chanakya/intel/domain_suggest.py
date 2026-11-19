"""Official-website suggestions for unresolved accounts, from Wikidata (CC0, free, no account).

Most top-down accounts arrive name-only (a signal names "Lytx", not lytx.com). Live website research needs the real
domain, and a wrong domain would attach someone else's evidence to the account, so resolution stays a HUMAN
decision (POST /api/intel/accounts/{cid}/resolve). This module only produces suggestions with their source:

  wbsearchentities(name) -> candidate items -> wbgetentities(P856 official website) -> domain

Every suggestion carries the Wikidata item URL so the reviewer can check it. Nothing is written to the database.
"""
from __future__ import annotations

import os

import httpx

from chanakya.scout.agent import normalize_domain
from chanakya.scout.providers_wikidata import API_URL, DEFAULT_UA, QID


class SuggestError(RuntimeError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=20, headers={"User-Agent": os.getenv("CHANAKYA_HTTP_USER_AGENT", DEFAULT_UA)})


def _warnings(name: str, hit: dict) -> list[str]:
    """Reasons to look twice. Found live: 'Omega Healthcare' matched a same-named REIT whose item has no description."""
    import re
    w = []
    if not (hit.get("description") or "").strip():
        w.append("Wikidata item has no description: confirm it is the same company")
    want = set(re.findall(r"[a-z0-9]{3,}", name.lower()))
    have = set(re.findall(r"[a-z0-9]{3,}", (hit.get("label") or "").lower()))
    if want and not want <= have:
        w.append(f"label '{hit.get('label')}' does not contain every word of '{name}'")
    return w


def suggest_domains(name: str, *, limit: int = 5, http: httpx.Client | None = None) -> list[dict]:
    """Candidates for `name` that declare an official website on Wikidata, best search match first."""
    if not (name or "").strip():
        return []
    own = http is None
    http = http or _client()
    try:
        r = http.get(API_URL, params={"action": "wbsearchentities", "search": name, "language": "en",
                                      "format": "json", "limit": limit, "type": "item"})
        r.raise_for_status()
        hits = [h for h in r.json().get("search", []) if QID.match(str(h.get("id", "")))]
        if not hits:
            return []
        r = http.get(API_URL, params={"action": "wbgetentities", "ids": "|".join(h["id"] for h in hits),
                                      "props": "claims", "format": "json"})
        r.raise_for_status()
        ents = r.json().get("entities", {})
    except httpx.HTTPError as e:
        raise SuggestError(f"Wikidata unavailable: {type(e).__name__}: {e}") from e
    finally:
        if own:
            http.close()
    out = []
    for rank, h in enumerate(hits):
        for claim in ents.get(h["id"], {}).get("claims", {}).get("P856", []):
            url = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
            d = normalize_domain(url) if isinstance(url, str) else None
            if d:
                out.append({"domain": d, "website": url, "qid": h["id"], "label": h.get("label"),
                            "description": h.get("description"), "search_rank": rank + 1,
                            "source_url": f"https://www.wikidata.org/wiki/{h['id']}",
                            "status": "suggestion (human must confirm)",
                            "warnings": _warnings(name, h)})
                break
    return out
