"""Agent 1 - structured catalogue path: YAML -> Offering DB -> Needs Analyzer -> ICP Builder.

Provisional input: the source file is read-only and every imported record is `unverified`.
* Offering DB: deterministic copy of the YAML. The `unknown` marker becomes None. Original text and
  provenance (path, sha256, catalogue_version, evidence URLs, raw record) are kept.
* Needs Analyzer: LLM extracts customer needs from each summary. Every need must quote that product's own
  summary verbatim, otherwise it is dropped.
* ICP Builder: deterministic, one ICP per product, built only from explicitly stated fields. Free-text
  `note`s (e.g. "the description concerns solar plants") are surfaced as review hints, never as criteria.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from chanakya.catalog.models import ICP, Criterion, Evidence
from chanakya.storage.db import finish_run, now, start_run, upsert_icp

UNVERIFIED = "unverified"


class CatalogueError(ValueError):
    pass


@dataclass
class Product:
    id: str
    name: str
    status: str | None
    summary: str | None
    problems_solved: Any
    target_industries: list[str]
    target_industries_note: str | None
    buyer_roles: Any
    integrations: Any
    deployment: str | None
    differentiators: Any
    caveats: list[str]
    evidence: list[dict]
    capability_ids: list[str]
    upstream_review: dict
    raw: dict
    needs: list[dict] = field(default_factory=list)
    verification: str = UNVERIFIED

    def missing_fields(self) -> list[str]:
        out = [f for f in ("summary", "problems_solved", "buyer_roles", "integrations", "deployment", "differentiators")
               if getattr(self, f) in (None, [], "")]
        if not self.target_industries:
            out.append("target_industries")
        return out


@dataclass
class Catalogue:
    path: str
    sha256: str
    version: str | None
    company: str | None
    products: list[Product]
    unresolved_refs: list[str]


def _unk(v, marker):
    if isinstance(v, str) and v.strip().lower() == marker:
        return None
    return v


def load_catalogue(path: str | Path) -> Catalogue:
    p = Path(path)
    raw_bytes = p.read_bytes()  # read-only: the source file is never written
    data = yaml.safe_load(raw_bytes.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise CatalogueError(f"{p}: expected a mapping with a 'products' list")
    marker = str(data.get("unknown_marker", "unknown")).lower()
    products, seen = [], set()
    for r in data["products"]:
        if not isinstance(r, dict) or not r.get("id") or not r.get("name"):
            raise CatalogueError(f"product record without id/name: {str(r)[:80]}")
        if r["id"] in seen:
            raise CatalogueError(f"duplicate product id {r['id']}")
        seen.add(r["id"])
        ti = r.get("target_industries") or {}
        stated = ti.get("stated") if isinstance(ti, dict) else None
        products.append(Product(
            id=r["id"], name=r["name"], status=r.get("status"),
            summary=_unk(r.get("summary"), marker),
            problems_solved=_unk(r.get("problems_solved"), marker),
            target_industries=[s for s in (stated or []) if _unk(s, marker)],
            target_industries_note=ti.get("note") if isinstance(ti, dict) else None,
            buyer_roles=_unk(r.get("buyer_roles"), marker),
            integrations=_unk(r.get("integrations"), marker),
            deployment=_unk(r.get("deployment"), marker),
            differentiators=_unk(r.get("differentiators"), marker),
            caveats=list(r.get("caveats") or []),
            evidence=list(r.get("evidence") or []),
            capability_ids=list(r.get("capability_ids") or []) + list(r.get("adjacent_capability_ids") or []),
            upstream_review=r.get("review") or {}, raw=r))
    caps = sorted({c for pr in products for c in pr.capability_ids})
    unresolved = [f"capability_ids {caps} refer to {data.get('derived_from_kb_version', '?')} "
                  "capability KB, which was not provided; not resolved"] if caps else []
    return Catalogue(p.as_posix(), hashlib.sha256(raw_bytes).hexdigest(), data.get("catalogue_version"),
                     data.get("company"), products, unresolved)


# ---------------- Offering DB ----------------
def _sha(s: str | None) -> str | None:
    return hashlib.sha256(s.encode()).hexdigest() if s else None


def reuse_cached_needs(con, products: list[Product]) -> list[Product]:
    """Copy stored needs for products whose summary is unchanged. Returns products still needing analysis."""
    todo = []
    for p in products:
        row = con.execute("SELECT summary_sha256, needs_json FROM products WHERE id=?", (p.id,)).fetchone()
        cached = json.loads(row["needs_json"]) if row and row["needs_json"] else None
        if row and cached is not None and row["summary_sha256"] == _sha(p.summary) and \
                all("needs analysis failed" not in str(n) for n in cached):
            p.needs = cached
        elif p.summary:
            todo.append(p)
    return todo


def store_products(con, cat: Catalogue) -> dict:
    """Upsert products. Records whose raw content, source hash and needs are unchanged are not touched,
    so imported_at reflects the import that last changed them."""
    j = json.dumps
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    for p in cat.products:
        prev = con.execute("SELECT raw_json, source_sha256, needs_json FROM products WHERE id=?", (p.id,)).fetchone()
        if prev and prev["raw_json"] == j(p.raw, default=str) and prev["source_sha256"] == cat.sha256 \
                and prev["needs_json"] == j(p.needs):
            stats["unchanged"] += 1
            # backfill the needs-cache key on rows created before the column existed (no content change)
            con.execute("UPDATE products SET summary_sha256=? WHERE id=? AND summary_sha256 IS NULL",
                        (_sha(p.summary), p.id))
            continue
        stats["updated" if prev else "inserted"] += 1
        con.execute(
            "INSERT INTO products(id,name,status,summary,problems_solved_json,target_industries_json,"
            "target_industries_note,buyer_roles_json,integrations_json,deployment,differentiators_json,caveats_json,"
            "evidence_json,capability_ids_json,needs_json,verification,upstream_review_json,source_path,source_sha256,"
            "catalogue_version,raw_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,status=excluded.status,summary=excluded.summary,"
            "problems_solved_json=excluded.problems_solved_json,target_industries_json=excluded.target_industries_json,"
            "target_industries_note=excluded.target_industries_note,buyer_roles_json=excluded.buyer_roles_json,"
            "integrations_json=excluded.integrations_json,deployment=excluded.deployment,"
            "differentiators_json=excluded.differentiators_json,caveats_json=excluded.caveats_json,"
            "evidence_json=excluded.evidence_json,capability_ids_json=excluded.capability_ids_json,"
            "needs_json=excluded.needs_json,verification=excluded.verification,"
            "upstream_review_json=excluded.upstream_review_json,source_path=excluded.source_path,"
            "source_sha256=excluded.source_sha256,catalogue_version=excluded.catalogue_version,"
            "raw_json=excluded.raw_json,imported_at=excluded.imported_at",
            (p.id, p.name, p.status, p.summary, j(p.problems_solved), j(p.target_industries), p.target_industries_note,
             j(p.buyer_roles), j(p.integrations), p.deployment, j(p.differentiators), j(p.caveats), j(p.evidence),
             j(p.capability_ids), j(p.needs), p.verification, j(p.upstream_review), cat.path, cat.sha256,
             cat.version, j(p.raw, default=str), now()))
        con.execute("UPDATE products SET summary_sha256=? WHERE id=?", (_sha(p.summary), p.id))
    con.commit()
    return stats


# ---------------- Needs Analyzer ----------------
NEEDS_SYSTEM = (
    "You are the Needs Analyzer. For each product, list what its summary says it addresses. Use ONLY the summary "
    "text. Each item must include `quote`: an excerpt copied EXACTLY from that product's summary, and `kind`:\n"
    "- 'stated_problem': the quote itself NAMES an undesirable condition the customer has (a pain, risk, failure, "
    "defect, noise, threat, disruption, complaint). Also give `problem_phrase`: the exact words inside the quote "
    "that name that condition.\n"
    "- 'capability': anything else - a feature, function, output or integration, even if it implies a benefit.\n"
    "Examples: 'compress alert noise into incidents' -> stated_problem, problem_phrase 'alert noise'. "
    "'predictive maintenance' -> capability (no problem is named). 'threat detection' -> capability "
    "(detecting is a feature; the summary does not say the customer suffers threats). "
    "Do not infer problems from features. Do not add industries, buyers or benefits. At most 5 items per product. "
    'Output JSON: {"products": {"<product id>": [{"need": str, "kind": str, "quote": str, "problem_phrase": str}]}}'
)

# Deterministic check on top of the model's label: a stated_problem's problem_phrase must appear in the quote and
# contain a word that names an undesirable condition. Otherwise it is downgraded to 'capability' and flagged.
PROBLEM_CUES = re.compile(
    r"\b(noise|noisy|risk|risks|at-risk|failure|failures|fail|defects?|anomal\w*|disruption\w*|complaints?|"
    r"intrusions?|flood|threats?|outages?|incidents?|downtime|backlog|delays?|errors?|fraud|leak\w*|breach\w*|"
    r"bias|drift|hotspots?|cracked|soiling|loitering|blast radius|why customers call)\b", re.I)


def classify_need(n: dict, quote: str) -> tuple[str, str | None]:
    """Returns (kind, downgrade_reason)."""
    if n.get("kind") != "stated_problem":
        return "capability", None
    phrase = str(n.get("problem_phrase") or "")
    if not phrase or _norm(phrase) not in _norm(quote):
        return "capability", "downgraded: problem_phrase missing or not in quote"
    if not PROBLEM_CUES.search(phrase):
        return "capability", f"downgraded: '{phrase}' names no undesirable condition"
    return "stated_problem", None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _needs_batch(llm, batch: list[Product], attempts: int = 2) -> dict:
    payload = json.dumps({p.id: p.summary for p in batch}, indent=1)
    for _ in range(attempts):
        r = llm.chat([{"role": "system", "content": NEEDS_SYSTEM}, {"role": "user", "content": payload}],
                     json_format=True, temperature=0.0, max_tokens=6000)
        try:
            out = json.loads(r.content).get("products", {})
            if isinstance(out, dict):
                return out
        except (json.JSONDecodeError, AttributeError):
            pass
    raise CatalogueError(f"Needs Analyzer returned invalid JSON for {[p.id for p in batch]}")


def analyze_product_needs(llm, products: list[Product], batch_size: int = 4) -> list[str]:
    """Fills product.needs in place. Returns descriptions of dropped (unverifiable/failed) needs.
    A failing batch is retried product-by-product; a product that still fails is reported, not guessed."""
    described = [p for p in products if p.summary]
    out: dict = {}
    dropped: list[str] = []
    for i in range(0, len(described), batch_size):
        batch = described[i:i + batch_size]
        try:
            out.update(_needs_batch(llm, batch))
        except CatalogueError:
            for p in batch:
                try:
                    out.update(_needs_batch(llm, [p]))
                except CatalogueError:
                    dropped.append(f"{p.id}: needs analysis failed (invalid model output); needs left empty")
    by_id = {p.id: p for p in described}
    for pid, needs in (out or {}).items():
        p = by_id.get(pid)
        if p is None:
            dropped.append(f"unknown product id from model: {pid}")
            continue
        for n in needs or []:
            q = str((n or {}).get("quote", ""))
            if len(q.strip()) >= 4 and _norm(q) in _norm(p.summary):
                kind, why = classify_need(n, q)
                item = {"need": str(n.get("need", ""))[:200], "kind": kind, "quote": q,
                        "source": f"products[{pid}].summary", "method": "llm_extracted", "verification": UNVERIFIED}
                if kind == "stated_problem":
                    item["problem_phrase"] = n.get("problem_phrase")
                if why:
                    item["label_note"] = why
                p.needs.append(item)
            else:
                dropped.append(f"{pid}: need without verbatim quote: {str(n)[:80]}")
    return dropped


# ---------------- ICP Builder ----------------
def build_product_icp(p: Product, cat: Catalogue) -> ICP:
    ev_url = next((e.get("url") for e in p.evidence if e.get("product_specific")), None) or \
        next((e.get("url") for e in p.evidence), None)
    icp = ICP(offerings=[p.name], verification=UNVERIFIED)
    if p.target_industries:
        icp.industries = Criterion(values=list(p.target_industries), evidence=[Evidence(
            source_path=cat.path, quote=", ".join(p.target_industries),
            field=f"products[{p.id}].target_industries.stated", url=ev_url)])
    if isinstance(p.buyer_roles, list) and p.buyer_roles:
        icp.buyer_roles = Criterion(values=[str(x) for x in p.buyer_roles], evidence=[Evidence(
            source_path=cat.path, quote=", ".join(map(str, p.buyer_roles)), field=f"products[{p.id}].buyer_roles")])
    icp.missing = [f for f in ("industries", "countries", "buyer_roles", "keywords", "disqualifiers")
                   if getattr(icp, f) is None] + ["employee_range"]
    if p.target_industries_note:
        icp.notes.append(f"source note (not used as a criterion): {p.target_industries_note}")
    icp.notes += [f"caveat: {c}" for c in p.caveats]
    return icp


def icp_searchable(icp: ICP) -> bool:
    """A Clay search needs at least one stated criterion; currently only industries can be stated."""
    return icp.industries is not None


@dataclass
class CatalogImport:
    catalogue: Catalogue
    icps: dict[str, tuple[int, ICP]]  # product id -> (icp row id, icp)
    dropped_needs: list[str]
    stats: dict = field(default_factory=dict)


def run_catalog_yaml(llm, con, path: str | Path, *, force_needs: bool = False) -> CatalogImport:
    """Idempotent: identical YAML -> no product rewrites, no new ICP rows, no LLM calls.
    force_needs=True re-runs the Needs Analyzer (e.g. after a prompt change)."""
    run_id = start_run(con, "catalog", {"source": str(path)})
    try:
        cat = load_catalogue(path)
        todo = [p for p in cat.products if p.summary] if force_needs else reuse_cached_needs(con, cat.products)
        if llm is None:
            dropped = [f"{p.id}: needs analysis skipped (no LLM)" for p in todo]
        else:
            dropped = analyze_product_needs(llm, todo) if todo else []
        stats = store_products(con, cat)
        if llm is None and todo:  # not analysed: clear the cache key so a later LLM run analyses them
            con.executemany("UPDATE products SET summary_sha256=NULL WHERE id=?", [(p.id,) for p in todo])
            con.commit()
        stats["needs_analyzed"] = len(todo) if llm is not None else 0
        icps: dict[str, tuple[int, ICP]] = {}
        new_icps = 0
        for p in cat.products:
            icp = build_product_icp(p, cat)
            if not icp_searchable(icp):
                continue
            icp_id, created = upsert_icp(con, icp.model_dump_json(), {cat.path: cat.sha256}, product_id=p.id)
            new_icps += created
            icps[p.id] = (icp_id, icp)
        con.commit()
        stats["icps_created"], stats["icps_reused"] = new_icps, len(icps) - new_icps
        finish_run(con, run_id, "ok", {"products": len(cat.products), "icps": list(icps), "sha256": cat.sha256,
                                       "dropped_needs": dropped, **stats})
        return CatalogImport(cat, icps, dropped, stats)
    except Exception as e:
        finish_run(con, run_id, "error", {"error": str(e)})
        raise
