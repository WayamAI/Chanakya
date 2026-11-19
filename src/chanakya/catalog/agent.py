"""Agent 1 - Catalog Agent: Offering DB loader, Needs Analyzer, ICP Builder.

Grounding rule: every extracted item must quote its source document verbatim. Items whose
quotes cannot be found in the source are dropped (not "repaired"), and reported.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from chanakya.catalog.models import ICP, Criterion, Evidence, Offering
from chanakya.storage.db import finish_run, now, start_run

log = logging.getLogger(__name__)
SUPPORTED = {".md", ".txt"}


class NoOfferingSources(RuntimeError):
    pass


@dataclass
class SourceDoc:
    path: str
    text: str
    sha256: str


@dataclass
class CatalogResult:
    offerings: list[Offering]
    icp: ICP
    icp_id: int
    dropped: list[str] = field(default_factory=list)


# ---------------- Offering DB: loading ----------------
def load_sources(folder: str | Path) -> list[SourceDoc]:
    docs = []
    for p in sorted(Path(folder).glob("*")):
        if p.suffix.lower() in SUPPORTED and p.is_file():
            t = p.read_text(encoding="utf-8")
            if t.strip():
                docs.append(SourceDoc(p.as_posix(), t, hashlib.sha256(t.encode()).hexdigest()))
    if not docs:
        raise NoOfferingSources(f"No Wayam offering documents in {folder}. Add .md/.txt files; nothing will be invented.")
    return docs


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_ok(ev: Evidence, docs: dict[str, SourceDoc]) -> bool:
    d = docs.get(ev.source_path)
    return bool(d and len(ev.quote.strip()) >= 8 and _norm(ev.quote) in _norm(d.text))


# ---------------- LLM helpers ----------------
def _ask_json(llm, system: str, user: str, attempts: int = 2) -> dict:
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last = ""
    for _ in range(attempts):
        r = llm.chat(msgs, json_format=True, temperature=0.0)
        try:
            return json.loads(r.content)
        except json.JSONDecodeError as e:
            last = str(e)
            msgs += [r.message, {"role": "user", "content": f"Invalid JSON ({last}). Return only valid JSON."}]
    raise ValueError(f"model did not return valid JSON: {last}")


def _docs_block(docs: list[SourceDoc]) -> str:
    return "\n\n".join(f'<document path="{d.path}">\n{d.text}\n</document>' for d in docs)


GROUNDING = ("Use ONLY the documents. Every item needs evidence: [{\"source_path\": <exact path attribute>, "
             "\"quote\": <verbatim excerpt, copied exactly>}]. If something is not stated, omit it. Never guess.")


# ---------------- Needs Analyzer ----------------
def analyze_needs(llm, docs: list[SourceDoc]) -> tuple[list[Offering], list[str]]:
    system = ("You are the Needs Analyzer for Wayam's catalog. Extract each Wayam offering and the customer "
              "problems/needs it addresses. " + GROUNDING +
              ' Output JSON: {"offerings": [{"name", "description", "problems_solved": [str], "evidence": [...]}]}')
    data = _ask_json(llm, system, _docs_block(docs))
    by_path = {d.path: d for d in docs}
    out, dropped = [], []
    for raw in data.get("offerings", []):
        try:
            o = Offering.model_validate(raw)
        except ValidationError:
            dropped.append(f"offering (invalid shape): {str(raw)[:80]}"); continue
        o.evidence = [e for e in o.evidence if quote_ok(e, by_path)]
        if o.evidence:
            out.append(o)
        else:
            dropped.append(f"offering '{o.name}': no verifiable quote")
    return out, dropped


# ---------------- ICP Builder ----------------
ICP_FIELDS = ["industries", "countries", "buyer_roles", "keywords", "disqualifiers"]


def build_icp(llm, docs: list[SourceDoc], offerings: list[Offering]) -> tuple[ICP, list[str]]:
    system = ("You are the ICP Builder for Wayam. From the documents, build the Ideal Customer Profile. " + GROUNDING +
              " Output JSON with optional keys: industries, countries, buyer_roles, keywords, disqualifiers "
              "(each {\"values\": [str], \"evidence\": [...]}), employee_min, employee_max (int, only if a size is "
              "explicitly stated), employee_evidence ([...]). Omit any key the documents do not support.")
    user = _docs_block(docs) + "\n\nExtracted offerings:\n" + json.dumps([o.name for o in offerings])
    data = _ask_json(llm, system, user)
    by_path = {d.path: d for d in docs}
    dropped: list[str] = []
    icp = ICP(offerings=[o.name for o in offerings])
    for f in ICP_FIELDS:
        raw = data.get(f)
        if not raw:
            continue
        try:
            c = Criterion.model_validate(raw)
        except ValidationError:
            dropped.append(f"{f}: invalid shape"); continue
        c.evidence = [e for e in c.evidence if quote_ok(e, by_path)]
        if c.evidence and c.values:
            setattr(icp, f, c)
        else:
            dropped.append(f"{f}: no verifiable quote")
    ev = []
    for e in data.get("employee_evidence") or []:
        try:
            e = Evidence.model_validate(e)
        except ValidationError:
            continue
        if quote_ok(e, by_path):
            ev.append(e)
    if ev and (isinstance(data.get("employee_min"), int) or isinstance(data.get("employee_max"), int)):
        icp.employee_min = data.get("employee_min") if isinstance(data.get("employee_min"), int) else None
        icp.employee_max = data.get("employee_max") if isinstance(data.get("employee_max"), int) else None
        icp.employee_evidence = ev
    elif data.get("employee_min") or data.get("employee_max"):
        dropped.append("employee range: no verifiable quote")
    icp.missing = [f for f in ICP_FIELDS if getattr(icp, f) is None] + \
                  ([] if icp.employee_evidence else ["employee_range"])
    return icp, dropped


# ---------------- Orchestration + Offering DB persistence ----------------
def run_catalog(llm, con, folder: str | Path = "data/offerings") -> CatalogResult:
    run_id = start_run(con, "catalog")
    try:
        docs = load_sources(folder)
        offerings, d1 = analyze_needs(llm, docs)
        if not offerings:
            raise NoOfferingSources("Offering documents contained no verifiable offerings.")
        icp, d2 = build_icp(llm, docs, offerings)
        sha_of = {d.path: d.sha256 for d in docs}
        for o in offerings:
            src = o.evidence[0].source_path
            con.execute(
                "INSERT INTO offerings(name,description,problems_json,source_path,source_sha256,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
                "problems_json=excluded.problems_json, source_path=excluded.source_path, "
                "source_sha256=excluded.source_sha256, updated_at=excluded.updated_at",
                (o.name, o.description, json.dumps(o.problems_solved), src, sha_of[src], now()))
        ver = (con.execute("SELECT COALESCE(MAX(version),0) FROM icps").fetchone()[0]) + 1
        cur = con.execute("INSERT INTO icps(version,icp_json,source_shas,created_at) VALUES(?,?,?,?)",
                          (ver, icp.model_dump_json(), json.dumps(sha_of), now()))
        con.commit()
        res = CatalogResult(offerings, icp, cur.lastrowid, d1 + d2)
        finish_run(con, run_id, "ok", {"offerings": len(offerings), "icp_id": res.icp_id,
                                       "missing": icp.missing, "dropped": res.dropped})
        return res
    except Exception as e:
        finish_run(con, run_id, "error", {"error": str(e)})
        raise
