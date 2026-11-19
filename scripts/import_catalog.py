"""Import the provisional Wayam catalogue -> Offering DB -> ICPs -> industry mappings -> dry-run Clay queries.
No Clay calls are made. Writes docs/CATALOG_IMPORT_REPORT.md.
Usage: python scripts/import_catalog.py <path/to/wayam_products.yaml>
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from chanakya.catalog.products import run_catalog_yaml  # noqa: E402
from chanakya.config import load_settings  # noqa: E402
from chanakya.llm.ollama_client import OllamaClient  # noqa: E402
from chanakya.scout.agent import run_scout  # noqa: E402
from chanakya.scout.dsl import ClaySchema  # noqa: E402
from chanakya.scout.industry_map import map_industries  # noqa: E402
from chanakya.storage.db import connect  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


async def main(src: str) -> None:
    s = load_settings()
    con = connect(s.db_path)
    schema = ClaySchema.from_capabilities(ROOT / "docs/clay_capabilities.json")
    with OllamaClient(s) as llm:
        r = run_catalog_yaml(llm, con, src, force_needs=FORCE)
        maps = {pid: map_industries(con, icp_id, icp.industries.values, schema.enums["industry"], llm)
                for pid, (icp_id, icp) in r.icps.items()}
    proposals = {}
    for pid, (icp_id, icp) in r.icps.items():
        q = await run_scout(icp, icp_id, con, None, schema, limit=1, industry_mappings=maps[pid])
        proposals[pid] = q

    cat = r.catalogue
    L = ["# Catalog import report (provisional, UNVERIFIED)", "",
         f"- Source: `{cat.path}` (read-only)", f"- SHA-256: `{cat.sha256}`",
         f"- catalogue_version: {cat.version}; company: {cat.company}", f"- Model: {s.ollama_model}",
         "- Every record is stored with `verification = unverified`.", f"- Import stats: {r.stats}", "", "## Unresolved references"]
    L += [f"- {u}" for u in cat.unresolved_refs] or ["- none"]
    L += ["", "## Imported products", "",
          "| ID | Name | Status | Stated industries | Needs extracted | Missing fields | Upstream review |",
          "|---|---|---|---|---|---|---|"]
    for p in cat.products:
        L.append(f"| {p.id} | {p.name} | {p.status} | {', '.join(p.target_industries) or '-'} | {len(p.needs)} | "
                 f"{', '.join(p.missing_fields())} | {p.upstream_review.get('status', '?')} |")
    L += ["", "## Extracted needs (LLM, verbatim-quote checked)", ""]
    for p in cat.products:
        for n in p.needs:
            L.append(f"- **{p.name}** [{n['kind']}]: {n['need']} (quote: \"{n['quote']}\")")
    L += ["", f"Dropped (unverifiable): {len(r.dropped_needs)}"] + [f"- {d}" for d in r.dropped_needs]
    L += ["", "## Offerings requiring human verification", ""]
    for p in cat.products:
        flags = []
        if p.status != "existing_offering": flags.append(f"status={p.status}")
        if not p.summary: flags.append("no summary")
        if not any(e.get("product_specific") for e in p.evidence): flags.append("no product-specific evidence page")
        flags += [c for c in p.caveats if "verify" in c.lower() or "not confirmed" in c.lower()]
        if flags:
            L.append(f"- **{p.name}** ({p.id}): " + "; ".join(flags))
    L += ["", "All 25 are upstream `unreviewed`; the items above have specific open questions.", "",
          "## ICPs (only products with stated criteria)", ""]
    for pid, (icp_id, icp) in r.icps.items():
        L += [f"### {pid} (icp id {icp_id})", f"- Industries (stated): {icp.industries.values}",
              f"- Missing: {icp.missing}"] + [f"- {n}" for n in icp.notes] + [""]
    L += ["Not searchable (no stated criteria): " +
          ", ".join(p.name for p in cat.products if p.id not in r.icps), "",
          "## Industry mappings needing approval", "",
          "| # | Product | Original term | Proposed Clay values | Status | Confidence | Alternatives | Rationale |",
          "|---|---|---|---|---|---|---|---|"]
    for pid, ms in maps.items():
        for m in ms:
            L.append(f"| {m.id} | {pid} | {m.original_term} | {m.canonical} | {m.status} | {m.confidence:.2f} | "
                     f"{m.alternatives[:5]} | {m.rationale[:200]} |")
    L += ["", "## Dry-run Clay queries (NOT executed, limit 1)", ""]
    for pid, q in proposals.items():
        L += [f"- **{pid}**: " + (f"`{q.query}`" if q.query else "_blocked: no approved criteria yet_"),
              f"  - pending review: {q.pending_review or 'none'}"]
    out = ROOT / "docs/CATALOG_IMPORT_REPORT.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    FORCE = "--reanalyze-needs" in sys.argv
    asyncio.run(main([a for a in sys.argv[1:] if not a.startswith("--")][0]))
