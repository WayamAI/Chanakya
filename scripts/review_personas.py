"""Persona Review CLI (Agent 3, People Finder). LOCAL ONLY: reads/writes SQLite, makes no network calls.

A human reviewer enters buyer personas, searchable job titles and Clay seniority values for a product. Nothing is
generated or auto-approved: every mapping starts `pending`, and only `approve` makes it usable by People Finder.
Every change is recorded in persona_mapping_history. The catalogue (products table and the YAML) is never modified.

Usage:
  python scripts/review_personas.py products [--missing]
  python scripts/review_personas.py show PRODUCT_ID
  python scripts/review_personas.py add PRODUCT_ID --persona P --title T [--title T2] [--seniority S]
                                        --by NAME --reason R --evidence TEXT [--evidence-url URL]
        (any missing value is prompted for interactively)
  python scripts/review_personas.py list [--product PRODUCT_ID] [--status pending|approved|rejected]
  python scripts/review_personas.py approve|reject MAPPING_ID --by NAME --reason R
  python scripts/review_personas.py update MAPPING_ID [--persona P] [--title T ...] [--seniority S ...]
                                        [--clear-seniority] --by NAME --reason R --evidence TEXT [--evidence-url URL]
  python scripts/review_personas.py history MAPPING_ID
  python scripts/review_personas.py seniorities
DB: $CHANAKYA_DB_PATH or data/chanakya.db
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from chanakya.people import personas as P  # noqa: E402
from chanakya.storage.db import connect  # noqa: E402


def _j(v):
    try:
        return json.loads(v) if isinstance(v, str) else v
    except json.JSONDecodeError:
        return v


def product_overview(con) -> list[dict]:
    rows = []
    for r in con.execute("SELECT id, name, summary, buyer_roles_json, evidence_json, verification FROM products "
                         "ORDER BY id"):
        roles = _j(r["buyer_roles_json"])
        ms = P.mappings_for(con, None, r["id"])
        rows.append({"id": r["id"], "name": r["name"], "description": r["summary"],
                     "buyer_roles": roles if roles not in (None, [], "") else None,
                     "buyer_roles_missing": roles in (None, [], ""), "evidence": _j(r["evidence_json"]) or [],
                     "verification": r["verification"],
                     "mappings": {s: sum(m.status == s for m in ms) for s in sorted(P.STATUSES)}})
    return rows


def _product(con, pid: str) -> dict:
    p = next((x for x in product_overview(con) if x["id"] == pid), None)
    if p is None:
        raise SystemExit(f"error: unknown product {pid!r} (see `products`)")
    return p


def _icp_for(con, pid: str):
    r = con.execute("SELECT id FROM icps WHERE product_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    return r["id"] if r else None


def _fmt_mapping(m: P.RoleMapping) -> str:
    ev = "; ".join(f"\"{e.get('quote', '')[:70]}\"" + (f" <{e['url']}>" if e.get("url") else "") for e in m.evidence)
    return (f"  #{m.id} [{m.status}] persona={m.persona!r} (source: {m.persona_source})\n"
            f"      titles={m.titles} seniorities={m.seniorities}\n"
            f"      last action by {m.reviewed_by or '-'}: {m.reason or '-'}\n      evidence: {ev or '-'}")


def _ask(value, prompt: str, *, required=True, many=False):
    if value:
        return value
    raw = input(f"{prompt}{' (comma-separated)' if many else ''}: ").strip()
    if not raw and required:
        raise SystemExit(f"error: {prompt} is required")
    return [x.strip() for x in raw.split(",") if x.strip()] if many else raw


def cmd(argv: list[str], con) -> int:
    a = parser().parse_args(argv)
    out = print
    try:
        if a.cmd == "products":
            rows = product_overview(con)
            miss = sum(r["buyer_roles_missing"] for r in rows)
            out(f"{len(rows)} products; {miss} with NO buyer persona in the catalogue (needs human review)\n")
            for r in rows:
                if a.missing and not r["buyer_roles_missing"]:
                    continue
                flag = "MISSING buyer_roles - needs human review" if r["buyer_roles_missing"] else \
                    f"buyer_roles={r['buyer_roles']}"
                c = r["mappings"]
                out(f"{r['id']:20s} {r['name']:14s} {flag} | mappings: approved={c['approved']} "
                    f"pending={c['pending']} rejected={c['rejected']}")
        elif a.cmd == "show":
            p = _product(con, a.product)
            out(f"{p['id']}  {p['name']}  (catalogue verification: {p['verification']})")
            out(f"description: {p['description'] or '(none in catalogue)'}")
            out("buyer_roles: " + ("MISSING - the catalogue does not state a buyer persona; needs human review"
                                   if p["buyer_roles_missing"] else str(p["buyer_roles"])))
            out("supporting evidence in catalogue:" + ("" if p["evidence"] else " (none)"))
            for e in p["evidence"]:
                out(f"  - {e.get('url') if isinstance(e, dict) else e}"
                    + (f"  supports: {e.get('supports')}" if isinstance(e, dict) and e.get("supports") else ""))
            ms = P.mappings_for(con, None, p["id"])
            out(f"persona mappings ({len(ms)}):" + ("" if ms else " none"))
            for m in ms:
                out(_fmt_mapping(m))
        elif a.cmd == "add":
            _product(con, a.product)
            persona = _ask(a.persona, "persona")
            titles = _ask(a.title, "searchable job titles", required=False, many=True)
            sen = a.seniority if a.seniority else _ask(None, "seniority values", required=False, many=True)
            by, reason = _ask(a.by, "reviewer name"), _ask(a.reason, "reason")
            evidence = _ask(a.evidence, "supporting evidence")
            mid = P.add_human_mapping(con, _icp_for(con, a.product), persona, titles=titles, seniorities=sen,
                                      reviewer=by, reason=reason, evidence=P.make_evidence(evidence, a.evidence_url),
                                      product_id=a.product)
            out(f"created mapping #{mid} [pending]. It is NOT usable until approved:\n"
                f"  python scripts/review_personas.py approve {mid} --by NAME --reason R")
        elif a.cmd == "list":
            ms = [P._row(r) for r in con.execute("SELECT * FROM persona_role_mappings ORDER BY id")]
            ms = [m for m in ms if (not a.product or m.product_id == a.product) and (not a.status or m.status == a.status)]
            out(f"{len(ms)} mapping(s)")
            for m in ms:
                out(f"{m.product_id}\n" + _fmt_mapping(m))
        elif a.cmd in ("approve", "reject"):
            P.review_mapping(con, a.mapping_id, "approved" if a.cmd == "approve" else "rejected",
                             reviewer=a.by, reason=a.reason)
            out(f"mapping #{a.mapping_id} -> {P.get_mapping(con, a.mapping_id).status}")
        elif a.cmd == "update":
            sen = [] if a.clear_seniority else a.seniority
            m = P.update_mapping(con, a.mapping_id, reviewer=a.by, reason=a.reason,
                                 evidence=P.make_evidence(a.evidence, a.evidence_url), persona=a.persona,
                                 titles=a.title, seniorities=sen)
            out("updated (previous version kept in history; mapping is pending re-approval):\n" + _fmt_mapping(m))
        elif a.cmd == "history":
            P.get_mapping(con, a.mapping_id)
            for h in P.history(con, a.mapping_id):
                snap = h["snapshot"]
                before = f" | before: titles={snap['titles']} seniorities={snap['seniorities']}" if snap else ""
                out(f"{h['at']} {h['action']:8s} {h['from_status'] or '-'} -> {h['to_status']} by {h['reviewer']}: "
                    f"{h['reason']}{before}")
        elif a.cmd == "seniorities":
            out("\n".join(sorted(P.CLAY_SENIORITIES)))
    except ValueError as e:
        out(f"error: {e}")
        return 2
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local persona review (no network calls).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("products"); sp.add_argument("--missing", action="store_true")
    sub.add_parser("show").add_argument("product")
    sub.add_parser("seniorities")
    sp = sub.add_parser("list"); sp.add_argument("--product"); sp.add_argument("--status", choices=sorted(P.STATUSES))
    for name in ("approve", "reject"):
        sp = sub.add_parser(name); sp.add_argument("mapping_id", type=int)
        sp.add_argument("--by", required=True); sp.add_argument("--reason", required=True)
    sub.add_parser("history").add_argument("mapping_id", type=int)
    sp = sub.add_parser("add"); sp.add_argument("product")
    for sp2 in (sp, (up := sub.add_parser("update"))):
        sp2.add_argument("--persona"); sp2.add_argument("--title", action="append")
        sp2.add_argument("--seniority", action="append"); sp2.add_argument("--evidence")
        sp2.add_argument("--evidence-url"); sp2.add_argument("--by"); sp2.add_argument("--reason")
    up.add_argument("mapping_id", type=int); up.add_argument("--clear-seniority", action="store_true")
    for o in up._actions:
        if o.dest in ("by", "reason", "evidence"):
            o.required = True
    return p


def main() -> None:
    con = connect(os.getenv("CHANAKYA_DB_PATH", str(ROOT / "data/chanakya.db")))
    sys.exit(cmd(sys.argv[1:], con))


if __name__ == "__main__":
    main()
