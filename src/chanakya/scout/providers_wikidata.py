"""Alternative public-source discovery: Wikidata Query Service (free, no account, CC0 data).

Used if Clay company search is unavailable or not approved. Limits to be aware of:
  * Coverage is uneven: well-known companies are present, many SMEs are not.
  * Industry / country are Wikidata items (QIDs). QIDs are NEVER guessed: the provider only runs with a
    human-approved mapping file (data/wikidata_mappings.json). `propose_qids` can suggest candidates via the
    public wbsearchentities API for a reviewer to approve.
  * Employee counts are optional statements, often stale; size filters only apply when a value exists.
  * Wikimedia asks automated clients to send a descriptive User-Agent; set CHANAKYA_HTTP_USER_AGENT.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

from chanakya.scout.agent import normalize_domain
from chanakya.scout.discovery import (CompanyCandidate, DiscoveryBlocked, DiscoveryRequest, DiscoveryResult,
                                      PreparedQuery)

SPARQL_URL = "https://query.wikidata.org/sparql"
API_URL = "https://www.wikidata.org/w/api.php"
DEFAULT_UA = "ChanakyaGTM/0.2 (https://wayam.ai; internal company-research tool) python-httpx"  # Wikimedia UA policy: name + contact URL, else HTTP 403
QID = re.compile(r"^Q[1-9]\d*$")
MAX_LIMIT = 5


class WikidataMappings:
    """Approved term -> [QID] mappings. Format:
    {"industries": {"Healthcare": {"qids": ["Q..."], "approved_by": "...", "approved_at": "..."}},
     "countries": {"India": {...}}}"""

    def __init__(self, data: dict | None = None):
        self.data = data or {"industries": {}, "countries": {}}

    @classmethod
    def load(cls, path: str | Path = "data/wikidata_mappings.json") -> "WikidataMappings":
        p = Path(path)
        return cls(json.loads(p.read_text(encoding="utf-8")) if p.exists() else None)

    def lookup(self, kind: str, term: str) -> list[str] | None:
        for k, v in (self.data.get(kind) or {}).items():
            if k.lower() == term.lower() and v.get("approved_by"):
                qids = [q for q in v.get("qids", []) if QID.match(q)]
                return qids or None
        return None


def build_sparql(industry_qids: list[str], country_qids: list[str], emp_min: int | None, emp_max: int | None,
                 limit: int) -> str:
    for q in industry_qids + country_qids:
        if not QID.match(q):
            raise ValueError(f"invalid QID {q!r}")
    vals = " ".join(f"wd:{q}" for q in industry_qids)
    lines = ["SELECT DISTINCT ?item ?itemLabel ?website ?employees ?industryLabel ?countryLabel WHERE {",
             f"  VALUES ?industry {{ {vals} }}",
             "  ?item wdt:P452 ?industry ;", "        wdt:P856 ?website .",
             "  OPTIONAL { ?item wdt:P1128 ?employees . }", "  OPTIONAL { ?item wdt:P17 ?country . }"]
    if country_qids:
        lines.insert(2, f"  VALUES ?country {{ {' '.join('wd:' + q for q in country_qids)} }}")
        lines[-1] = "  ?item wdt:P17 ?country ."
    if emp_min is not None:
        lines.append(f"  FILTER(!BOUND(?employees) || ?employees >= {int(emp_min)})")
    if emp_max is not None:
        lines.append(f"  FILTER(!BOUND(?employees) || ?employees <= {int(emp_max)})")
    lines += ['  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }', f"}} LIMIT {int(limit)}"]
    return "\n".join(lines)


class WikidataProvider:
    name = "wikidata"
    cost_note = "Free public endpoint (no account). Subject to Wikimedia rate limits and usage policy."

    def __init__(self, mappings: WikidataMappings, http: httpx.AsyncClient | None = None):
        self.mappings = mappings
        self._http = http

    def prepare(self, req: DiscoveryRequest) -> PreparedQuery:
        issues, unmapped, ind, cty = [], [], [], []
        if not 1 <= req.limit <= MAX_LIMIT:
            issues.append(f"limit must be 1..{MAX_LIMIT}")
        for t in (req.icp.industries.values if req.icp.industries else []):
            q = self.mappings.lookup("industries", t)
            (ind.extend(q) if q else issues.append(f"industry '{t}': no approved Wikidata QID mapping"))
        for t in (req.icp.countries.values if req.icp.countries else []):
            q = self.mappings.lookup("countries", t)
            (cty.extend(q) if q else issues.append(f"country '{t}': no approved Wikidata QID mapping"))
        for f in ("keywords", "buyer_roles"):
            if getattr(req.icp, f):
                unmapped.append(f"{f}: not expressible in this Wikidata query")
        if not ind:
            issues.append("no approved industry QIDs; refusing an unfiltered query")
        text = "" if issues else build_sparql(ind, cty, req.icp.employee_min, req.icp.employee_max, req.limit)
        return PreparedQuery(self.name, text, ready=not issues, issues=issues, unmapped=unmapped,
                             params={"industry_qids": ind, "country_qids": cty})

    def _client(self) -> httpx.AsyncClient:
        return self._http or httpx.AsyncClient(timeout=60, headers={
            "User-Agent": os.getenv("CHANAKYA_HTTP_USER_AGENT", DEFAULT_UA),
            "Accept": "application/sparql-results+json"})

    async def discover(self, prepared: PreparedQuery, req: DiscoveryRequest) -> DiscoveryResult:
        if not prepared.ready:
            raise DiscoveryBlocked("Wikidata query not ready: " + "; ".join(prepared.issues))
        client = self._client()
        try:
            r = await client.get(SPARQL_URL, params={"query": prepared.text, "format": "json"})
            if r.status_code == 429:
                raise DiscoveryBlocked("Wikidata rate limit (429); retry later")
            r.raise_for_status()
            data = r.json()
            req.response_log.append({"tool": "wikidata-sparql", "task_id": None, "phase": "initial", "payload": data})
        finally:
            if self._http is None:
                await client.aclose()
        cands, skipped, seen = [], [], set()
        for b in data.get("results", {}).get("bindings", []):
            v = {k: x.get("value") for k, x in b.items()}
            d = normalize_domain(v.get("website"))
            if not d:
                skipped.append(f"no usable website for {v.get('item')}"); continue
            if d in seen:
                continue  # SPARQL row fan-out (multiple industries/countries per item)
            seen.add(d)
            emp = v.get("employees")
            rec = {"name": v.get("itemLabel"), "domain": d, "website": v.get("website"),
                   "industry": v.get("industryLabel"), "country": v.get("countryLabel"),
                   "estimated_employee_count": int(float(emp)) if emp else None, "wikidata_item": v.get("item")}
            cands.append(CompanyCandidate(d, rec["name"], rec, self.name, "wikidata-sparql", v.get("item")))
        return DiscoveryResult(prepared, cands, raw=data, skipped=skipped,
                               responses=[{"tool": "wikidata-sparql", "task_id": None, "phase": "initial",
                                           "payload": data}], records_response_index=0)


async def propose_qids(term: str, http: httpx.AsyncClient, limit: int = 5) -> list[dict]:
    """Candidate QIDs for a reviewer (wbsearchentities). Suggestions only; never auto-approved."""
    r = await http.get(API_URL, params={"action": "wbsearchentities", "search": term, "language": "en",
                                        "format": "json", "limit": limit, "type": "item"})
    r.raise_for_status()
    return [{"qid": s.get("id"), "label": s.get("label"), "description": s.get("description")}
            for s in r.json().get("search", []) if QID.match(str(s.get("id", "")))]
