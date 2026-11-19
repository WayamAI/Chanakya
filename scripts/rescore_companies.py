"""Re-assess stored companies LOCALLY from their saved records, using the SEARCH-TIME context. No external calls
except the optional Ollama relevance check. data_json and company_sources are never modified.

Context priority (never the *current* pilot scope):
  1. search_run: the `search_context` saved in the discovery run that produced the company's latest observation.
  2. reconstructed_from_executed_query: for records from before contexts were saved, the industry/country values
     are parsed from the exact DSL query that was executed (stored in company_sources.query).
  3. none: no ICP or query available; only data quality is assessed, filters are reported as not assessed.
Human decisions are re-attached from the human_reviews table.

Usage: python scripts/rescore_companies.py [--domain icai.org] [--llm-relevance]
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from chanakya.catalog.models import ICP, Criterion, Evidence  # noqa: E402
from chanakya.scout.agent import assess_filter_match  # noqa: E402
from chanakya.scout.qualification import apply_human_decision, assess_company, load_product_context  # noqa: E402
from chanakya.storage.db import connect, now  # noqa: E402

_IN_LIST = r'\(\s*((?:"(?:[^"\\]|\\.)*"\s*,?\s*)+)\)'


def _values(fragment: str) -> list[str]:
    return [v.replace('\\"', '"') for v in re.findall(r'"((?:[^"\\]|\\.)*)"', fragment)]


def context_from_query(query: str | None) -> dict:
    """Parse the executed Clay DSL for the company-only constraints this pipeline generates."""
    if not query:
        return {}
    out = {}
    if m := re.search(r"\bindustry in " + _IN_LIST, query):
        out["industry_values"] = _values(m.group(1))
    if m := re.search(r"locations\.any\(country_name in " + _IN_LIST + r"\)", query):
        out["countries"] = _values(m.group(1))
    for op, key in ((">=", "employee_min"), ("<=", "employee_max")):
        if m := re.search(r"estimated_employee_count " + op + r" (\d+)", query):
            out[key] = int(m.group(1))
    return out


def _latest_source(con, company_id: int):
    return con.execute("SELECT * FROM company_sources WHERE company_id=? ORDER BY id DESC LIMIT 1",
                       (company_id,)).fetchone()


def _run_context(con, run_id):
    if run_id is None:
        return None
    r = con.execute("SELECT detail_json FROM runs WHERE id=?", (run_id,)).fetchone()
    return (json.loads(r["detail_json"] or "{}").get("search_context") if r else None) or None


def effective_context(con, company) -> tuple[ICP, list[str], str, str | None]:
    """Returns (effective ICP, approved industry values, context_source, executed query)."""
    src = _latest_source(con, company["id"])
    ctx = _run_context(con, src["run_id"] if src is not None and "run_id" in src.keys() else None)
    if ctx and ctx.get("effective_icp") is not None:
        return ICP.model_validate(ctx["effective_icp"]), list(ctx.get("approved_industry_values") or []), \
            "search_run", ctx.get("query")
    stored = None
    if company["icp_id"] is not None:
        row = con.execute("SELECT icp_json FROM icps WHERE id=?", (company["icp_id"],)).fetchone()
        stored = ICP.model_validate_json(row["icp_json"]) if row else None
    query = src["query"] if src is not None else None
    q = context_from_query(query)
    if not q and stored is None:
        return ICP(), [], "none", query
    icp = stored.model_copy(deep=True) if stored else ICP()
    ev = [Evidence(source_path="company_sources.query", quote=query or "", field="executed_query",
                   verification="reconstructed_from_executed_query")]
    if q.get("industry_values") and icp.industries is None:
        icp.industries = Criterion(values=q["industry_values"], evidence=ev)
    if q.get("countries"):
        icp.countries = Criterion(values=q["countries"], evidence=ev)
    for k in ("employee_min", "employee_max"):
        if k in q:
            setattr(icp, k, q[k])
    return icp, list(q.get("industry_values") or []), "reconstructed_from_executed_query", query


def rescore_company(con, company, llm=None) -> dict:
    rec = json.loads(company["data_json"])
    icp, approved, ctx_source, query = effective_context(con, company)
    src = _latest_source(con, company["id"])
    provider_source = f"{src['source']}:{src['tool']}" if src is not None else "unknown"
    fm = assess_filter_match(rec, icp, approved, source=provider_source)
    if ctx_source == "none":
        fm["note"] = "search filters not assessed: no search-time ICP or executed query is available"
    product = next((p for p in (load_product_context(con, n) for n in icp.offerings) if p), None)
    try:
        asm = assess_company(rec, icp, fm, product, llm)
    except Exception as e:  # never let one company stop the batch
        asm = assess_company(rec, icp, fm, product, None)
        asm["product_relevance"]["reason"] = f"assessor failed ({type(e).__name__}); relevance not assessed"
    asm = apply_human_decision(con, company["id"], asm)
    has_raw = con.execute("SELECT COUNT(*) FROM company_sources WHERE company_id=? AND raw_response_id IS NOT NULL",
                          (company["id"],)).fetchone()[0]
    asm.update({"context_source": ctx_source, "executed_query": query, "provider_source": provider_source,
                "rescored_at": now(),
                "provenance_note": ("full provider response(s) stored in raw_responses" if has_raw else
                                    "full provider response NOT captured (search ran before raw_responses existed); "
                                    "verbatim record is in company_sources.raw_json")})
    con.execute("UPDATE companies SET fit_score=NULL, fit_json=NULL, search_filter_json=?, search_filter_score=?, "
                "qualification_status=?, qualification_source=?, assessment_json=? WHERE id=?",
                (json.dumps(asm["search_filter_match"]), asm["search_filter_match"]["score"],
                 asm["qualification_status"], asm["qualification_source"], json.dumps(asm), company["id"]))
    con.commit()
    return {"domain": company["domain"], "name": company["name"], **asm}


def rescore_all(con, llm=None, scope_path=None, domain: str | None = None) -> list[dict]:
    """scope_path is accepted for CLI compatibility but deliberately ignored: re-scoring uses search-time context."""
    q = "SELECT * FROM companies" + (" WHERE domain=?" if domain else "") + " ORDER BY id"
    return [rescore_company(con, c, llm) for c in con.execute(q, (domain,) if domain else ()).fetchall()]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain")
    p.add_argument("--llm-relevance", action="store_true")
    a = p.parse_args()
    con = connect(os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data/chanakya.db")))
    llm = None
    if a.llm_relevance:
        from chanakya.config import load_settings
        from chanakya.llm.ollama_client import OllamaClient
        llm = OllamaClient(load_settings())
    try:
        for r in rescore_all(con, llm, domain=a.domain):
            print(json.dumps(r, indent=2))
    finally:
        if llm:
            llm.close()


if __name__ == "__main__":
    main()
