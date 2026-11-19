"""Lakshya handover intelligence: full ingestion of the 4 Nov 2026 package, not only the signal seed.

Sources (all read-only, paths under the handover folder, see `locate`):
  03_Research/lakshya_seed_all.json      38 signals, 27 accounts, 3 themes  -> signals / companies (intel.signals)
  03_Research/Research_Findings.xlsx     gap themes (need, reuse, verdict, next step), competitors
  02_Deliverables/Setu_leads.xlsx        17 Setu leads (need, why now, whom, by when, angle, risk, analyst email),
                                         deep dives (NHSBSA, Lytx) with named contacts, Setu parts

Everything except signals lands in `lakshya_records` (kind, external_id, company_id, product_id, title, body, data,
source_url, source_file, source_ref, as_of, content hash). Rules:
  * idempotent: external_id is stable (`lakshya:<kind>:<key>`); a re-import updates changed rows, never duplicates;
  * provenance: file, sheet and row are kept; the handover date is the `as_of` date. Records are HISTORICAL analyst
    research: `context` labels them with their age, and "by when" dates in the past are flagged, never shown as live;
  * entity resolution: a lead or deep dive is attached to the canonical company (shared resolver). Market segments
    (no single organisation) get no company;
  * named contacts from the deep dives become people with role `needs_manual_review` and source `lakshya-handover`
    (the handover itself says "names may be out of date; verify"). Nothing is approved automatically;
  * no email address is ever taken or guessed from the handover for a person.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

from chanakya.intel import entities as E
from chanakya.storage.db import jdump, now

PROVENANCE = "lakshya-handover-research-2026-11-04"
PEOPLE_SOURCE = "lakshya-handover"
AS_OF = "2026-11-04"
SETU = "PRD-SETU"
FILES = {"seed": "03_Research/lakshya_seed_all.json", "research": "03_Research/Research_Findings.xlsx",
         "setu": "02_Deliverables/Setu_leads.xlsx"}
KINDS = ("lead", "deep_dive", "competitor", "setu_part", "gap_theme", "account")
_ROLE_WORDS = {"chief", "head", "director", "officer", "vp", "cdo", "cio", "cfo", "cto", "ceo", "the", "bi",
               "engineer", "others", "commercial", "manager", "lead", "team", "route", "president", "of", "and",
               "data", "services", "partnerships", "alliances", "practice"}


def ensure(con) -> None:
    """Tables are part of the main schema (storage/db.py); nothing to do for a connection opened with connect()."""


def locate(path: str | os.PathLike | None = None) -> Path | None:
    """The handover root (the folder holding 03_Research/). Accepts the root, any parent that contains it, or the
    seed file itself. Falls back to LAKSHYA_DIR, then the folder of LAKSHYA_SEED."""
    cands = [path, os.getenv("LAKSHYA_DIR"), os.getenv("LAKSHYA_SEED")]
    for c in cands:
        if not c:
            continue
        p = Path(c)
        if p.is_file():
            p = p.parent.parent
        if (p / FILES["seed"]).exists():
            return p
        if p.is_dir():
            for hit in sorted(p.rglob("lakshya_seed_all.json")):
                if hit.parent.name == "03_Research":
                    return hit.parent.parent
    return None


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _txt(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.date().isoformat() if isinstance(v, dt.datetime) else v.isoformat()
    s = str(v).strip()
    return s or None


def _link(cell) -> str | None:
    h = getattr(cell, "hyperlink", None)
    t = getattr(h, "target", None) if h else None
    if t:
        return t
    v = _txt(cell.value)
    return v if v and v.lower().startswith("http") else None


def _table(ws, header_first: str):
    """Rows below the header row whose first cell equals `header_first`: yields (row_number, {header: cell})."""
    hdr, hrow = None, None
    for i, row in enumerate(ws.iter_rows(), 1):
        first = _txt(row[0].value)
        if hdr is None:
            if first == header_first:
                hdr, hrow = [_txt(c.value) for c in row], i
            continue
        if not any(c.value is not None for c in row):
            continue
        yield i, {h: c for h, c in zip(hdr, row) if h}


def _upsert(con, stats: dict, *, kind: str, key: str, title: str, body: str | None, data: dict, source_file: str,
            source_ref: str, source_url: str | None = None, company_id: int | None = None,
            product_id: str | None = None) -> int:
    ext = f"lakshya:{kind}:{E.slug(key)}"
    sha = _sha({"t": title, "b": body, "d": data, "u": source_url, "c": company_id, "p": product_id})
    row = con.execute("SELECT id, content_sha256 FROM lakshya_records WHERE external_id=?", (ext,)).fetchone()
    t = now()
    if row is None:
        rid = con.execute(
            "INSERT INTO lakshya_records(kind,external_id,company_id,product_id,title,body,data_json,source_url,"
            "source_file,source_ref,as_of,content_sha256,imported_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, ext, company_id, product_id, title, body, jdump(data), source_url, source_file, source_ref, AS_OF,
             sha, t, t)).lastrowid
        stats[kind]["created"] += 1
        return rid
    if row["content_sha256"] != sha:
        con.execute("UPDATE lakshya_records SET company_id=?, product_id=?, title=?, body=?, data_json=?, source_url=?,"
                    " source_file=?, source_ref=?, content_sha256=?, updated_at=? WHERE id=?",
                    (company_id, product_id, title, body, jdump(data), source_url, source_file, source_ref, sha, t,
                     row["id"]))
        stats[kind]["updated"] += 1
    else:
        stats[kind]["unchanged"] += 1
    return row["id"]


def _company(con, name: str, *, create: bool, workflow_id=None, detail: dict | None = None) -> int | None:
    cid, _ = E.find_company(con, name=name, key=E.slug(name))
    if cid is None and not create:
        return None
    return E.resolve_company(con, name=name, key=E.slug(name), direction=E.TOP, source=PROVENANCE,
                             workflow_id=workflow_id, detail=detail)["company_id"]


def _is_person_name(s: str) -> bool:
    toks = s.replace(".", " ").split()
    return 2 <= len(toks) <= 3 and all(t[:1].isupper() and t.isalpha() for t in toks) and not (
        {t.lower() for t in toks} & _ROLE_WORDS)


def _first_sentence(s: str | None) -> str:
    first = re.split(r"(?<=[a-z\)])\.\s", (s or "").strip(), maxsplit=1)[0].rstrip(".").strip()
    return re.split(r"\s+(?:at|from|based in)\s", first, maxsplit=1)[0].strip()


def _add_person(con, stats: dict, company_id: int, name: str, title: str, *, note: str, source_url: str | None,
                source_ref: str, run_id: int | None) -> None:
    from chanakya.bottomup.agent import approved_mappings
    from chanakya.people.finder import upsert_person
    before = con.execute("SELECT COUNT(*) FROM person_sources p JOIN people x ON x.id=p.person_id WHERE p.source=? AND "
                         "p.company_id=? AND x.full_name=? AND p.query=?", (PEOPLE_SOURCE, company_id, name,
                                                                         source_ref)).fetchone()[0]
    if before:  # this exact handover row was already imported: keep one provenance row per source row
        stats["people"]["unchanged"] += 1
        return
    rec = {"full_name": name, "job_title": title, "source_url": source_url, "lakshya_note": note, "as_of": AS_OF,
           "caveat": "Name from the Lakshya handover (public pages as of 4 Nov 2026); may be out of date. Verify the "
                     "role on a current public page before approving."}
    _, created = upsert_person(con, rec, company_id, mappings=approved_mappings(con), run_id=run_id,
                               raw_response_id=None, tool="lakshya-deep-dive", task_id=None, query=source_ref,
                               source=PEOPLE_SOURCE)
    stats["people"]["created" if created else "matched_existing"] += 1


def import_all(con, root: str | os.PathLike | None = None, *, by: str | None = None, workflow_id=None) -> dict:
    """Ingest the whole handover. Returns counts per kind plus a validation block comparing with the source."""
    import openpyxl

    from chanakya.intel import signals as SG
    from chanakya.storage.db import finish_run, start_run
    ensure(con)
    base = locate(root)
    if base is None:
        raise FileNotFoundError("Lakshya handover folder not found (give a path, or set LAKSHYA_DIR / LAKSHYA_SEED)")
    stats: dict = {k: {"created": 0, "updated": 0, "unchanged": 0} for k in KINDS}
    stats["people"] = {"created": 0, "matched_existing": 0, "unchanged": 0}
    files, problems = {}, []
    run_id = start_run(con, "lakshya_import", {"root": str(base), "workflow_id": workflow_id})

    # 1. signals, accounts and themes (the seed)
    seed_path = base / FILES["seed"]
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    files["seed"] = {"path": str(seed_path), "sha256": hashlib.sha256(seed_path.read_bytes()).hexdigest()}
    sig = SG.import_lakshya_seed(con, seed_path, workflow_id=workflow_id)
    for key, a in seed.get("accounts", {}).items():
        cid, _ = E.find_company(con, name=a["name"], key=key)
        _upsert(con, stats, kind="account", key=key, title=a["name"], body=a.get("notes") or None,
                data={"stage": a.get("stage"), "contacts": a.get("contacts") or [], "key": key},
                source_file=FILES["seed"], source_ref=f"accounts.{key}", company_id=cid)

    # 2. research workbook: gap themes and competitors
    rp = base / FILES["research"]
    if rp.exists():
        files["research"] = {"path": str(rp), "sha256": hashlib.sha256(rp.read_bytes()).hexdigest()}
        wb = openpyxl.load_workbook(rp, data_only=True)
        for n, r in _table(wb["Gap themes"], "Gap theme"):
            name = _txt(r["Gap theme"].value)
            if not name or name.lower().startswith("verdict rule"):
                continue
            _upsert(con, stats, kind="gap_theme", key=name, title=name, body=_txt(r["What buyers need"].value),
                    data={"reuse_idea": _txt(r["Reuse idea"].value), "named_companies": _txt(r["Named companies"].value),
                          "kinds_of_signal": _txt(r["Kinds of signal"].value), "verdict": _txt(r["Verdict"].value),
                          "next_step": _txt(r["Next step"].value)},
                    source_file=FILES["research"], source_ref=f"Gap themes!{n}")
        for n, r in _table(wb["Competitors"], "Competitor"):
            name = _txt(r["Competitor"].value)
            if not name:
                continue
            area = _txt(r["Area"].value) or ""
            _upsert(con, stats, kind="competitor", key=f"{area}-{name}", title=name, body=_txt(r["Offer"].value),
                    data={"area": area, "vendor_claim_unverified": _txt(r["Vendor-stated claim (unverified)"].value),
                          "how_to_answer": _txt(r["How to answer"].value)},
                    source_file=FILES["research"], source_ref=f"Competitors!{n}", source_url=_link(r["Source"]),
                    product_id=SETU if area.lower() == "setu" else None)
    else:
        problems.append(f"missing {FILES['research']}")

    # 3. Setu leads workbook: leads, deep dives, Setu parts
    sp = base / FILES["setu"]
    if sp.exists():
        files["setu"] = {"path": str(sp), "sha256": hashlib.sha256(sp.read_bytes()).hexdigest()}
        wb = openpyxl.load_workbook(sp, data_only=True)
        for n, r in _table(wb["Setu leads"], "#"):
            lead = _txt(r["Lead"].value)
            if not lead or not _txt(r["#"].value) or not str(_txt(r["#"].value)).isdigit():
                continue
            ltype = _txt(r["Lead type"].value) or ""
            segment = "segment" in ltype.lower()
            cid = None if segment else _company(con, lead, create=True, workflow_id=workflow_id,
                                                detail={"lakshya_lead": int(_txt(r["#"].value)), "lead_type": ltype})
            g = lambda h: _txt(r[h].value) if h in r else None  # noqa: E731
            _upsert(con, stats, kind="lead", key=f"setu-{int(g('#')):02d}", title=lead, body=g("What is needed"),
                    data={"number": int(g("#")), "priority": g("Priority"), "lead_type": ltype,
                          "why_now": g("Why now (evidence)"), "reach_first": g("Whom to reach out: first"),
                          "reach_second": g("Whom to reach out: second"), "act_by": g("By when to reach out"),
                          "why_that_date": g("Why that date"), "setu_part": g("What part of Setu could interest them"),
                          "opening_line": g("Opening line (draft, your voice)"), "proof_to_bring": g("Proof to bring"),
                          "warm_path": g("Warm path to check"), "risk": g("Risk or caveat"),
                          "next_step": g("Next step"), "status": g("Status"), "owner": g("Owner"),
                          "email_subject": g("Email subject"), "email_body": g("Email (copy and paste)"),
                          "accelerator_url": g("Accelerator URL")},
                    source_file=FILES["setu"], source_ref=f"Setu leads!{n}", source_url=_link(r["Source"]),
                    company_id=cid, product_id=SETU)
        for sheet in [s for s in wb.sheetnames if s.startswith("Deep dive ")]:
            ws = wb[sheet]
            head = _txt(ws.cell(1, 1).value) or sheet
            cname = head.split(":", 1)[1].strip() if ":" in head else sheet.replace("Deep dive ", "")
            cid = _company(con, cname, create=True, workflow_id=workflow_id, detail={"lakshya_deep_dive": sheet})
            for n, r in _table(ws, "Section"):
                section, item, detail = _txt(r["Section"].value), _txt(r["Item"].value), _txt(r["Detail"].value)
                if not (section and item):
                    continue
                url = _link(r["Source"]) if "Source" in r else None
                _upsert(con, stats, kind="deep_dive", key=f"{cname}-{n}", title=f"{section}: {item}", body=detail,
                        data={"section": section, "item": item}, source_file=FILES["setu"],
                        source_ref=f"{sheet}!{n}", source_url=url, company_id=cid, product_id=SETU)
                if section.lower().startswith("whom to reach"):
                    nm = re.sub(r"^\d+\.\s*", "", item).strip()
                    if _is_person_name(nm):
                        _add_person(con, stats, cid, nm, _first_sentence(detail), note=detail or "", source_url=url,
                                    source_ref=f"{sheet}!{n}", run_id=run_id)
                    elif nm.lower().startswith("others"):
                        for m in re.finditer(r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,2}) \(([^)]+)\)", detail or ""):
                            if _is_person_name(m.group(1)):
                                _add_person(con, stats, cid, m.group(1), m.group(2), note=detail or "",
                                            source_url=url, source_ref=f"{sheet}!{n}:{E.slug(m.group(1))}",
                                            run_id=run_id)
        for n, r in _table(wb["Setu parts"], "Part"):
            part = _txt(r["Part"].value)
            if not part or part.lower().startswith("source:"):
                continue
            _upsert(con, stats, kind="setu_part", key=part, title=part, body=_txt(r["What it does"].value),
                    data={"who_cares": _txt(r["Who cares most"].value),
                          "proof_today": _txt(r["Proof you can show today"].value),
                          "proof_gap": _txt(r["Proof gap to close"].value),
                          "site_status": _txt(r["Status on your site"].value)},
                    source_file=FILES["setu"], source_ref=f"Setu parts!{n}", product_id=SETU)
    else:
        problems.append(f"missing {FILES['setu']}")

    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
    validation = {
        "source": {"signals": len(seed.get("signals", {})), "accounts": len(seed.get("accounts", {})),
                   "themes": len(seed.get("themes", {}))},
        "database": {"signals": q("SELECT COUNT(*) FROM signals WHERE provenance=?", PROVENANCE),
                     "signals_distinct_external_ids": q("SELECT COUNT(DISTINCT external_id) FROM signals WHERE "
                                                        "provenance=?", PROVENANCE),
                     "accounts_records": q("SELECT COUNT(*) FROM lakshya_records WHERE kind='account'"),
                     "accounts_linked_to_company": q("SELECT COUNT(*) FROM lakshya_records WHERE kind='account' AND "
                                                     "company_id IS NOT NULL"),
                     "signals_mapped_to_accelerator": q(
                         "SELECT COUNT(DISTINCT s.id) FROM signals s JOIN signal_fits f ON f.signal_id=s.id JOIN "
                         "products p ON p.id=f.product_id WHERE s.provenance=?", PROVENANCE),
                     "signals_with_source_url": q("SELECT COUNT(*) FROM signals WHERE provenance=? AND source_url IS "
                                                  "NOT NULL AND source_url!=''", PROVENANCE),
                     **{k: q("SELECT COUNT(*) FROM lakshya_records WHERE kind=?", k) for k in KINDS if k != "account"}},
        "problems": problems}
    validation["ok"] = (validation["database"]["signals"] == validation["source"]["signals"]
                        == validation["database"]["signals_distinct_external_ids"]
                        and validation["database"]["accounts_records"] == validation["source"]["accounts"]
                        and not problems)
    result = {"root": str(base), "signals": sig, "records": stats, "validation": validation}
    con.execute("INSERT INTO lakshya_imports(root,files_json,result_json,by,at) VALUES(?,?,?,?,?)",
                (str(base), jdump(files), jdump(result), by, now()))
    finish_run(con, run_id, "done", {"validation_ok": validation["ok"], "records": stats})
    con.commit()
    return result


# ---------------- read side: CRM panels, search, agent context ----------------
def _age_days() -> int:
    return (dt.date.today() - dt.date.fromisoformat(AS_OF)).days


def _row(r) -> dict:
    d = dict(r)
    d["data"] = json.loads(d.pop("data_json") or "{}")
    d["age_days"] = _age_days()
    d["historical"] = True
    act = d["data"].get("act_by")
    if act:
        try:
            d["act_by_passed"] = dt.date.fromisoformat(act[:10]) < dt.date.today()
        except ValueError:
            pass
    return d


def for_company(con, company_id: int) -> list[dict]:
    ensure(con)
    return [_row(r) for r in con.execute("SELECT * FROM lakshya_records WHERE company_id=? ORDER BY CASE kind WHEN "
                                         "'lead' THEN 0 WHEN 'account' THEN 1 ELSE 2 END, id", (company_id,))]


def for_product(con, product_id: str) -> list[dict]:
    ensure(con)
    return [_row(r) for r in con.execute("SELECT * FROM lakshya_records WHERE product_id=? AND kind IN "
                                         "('setu_part','competitor') ORDER BY kind, id", (product_id,))]


def search(con, q: str = "", *, kind: str | None = None, limit: int = 100) -> list[dict]:
    """Search every Lakshya record AND the Lakshya signals by text (title, body, data)."""
    ensure(con)
    like = f"%{q.lower()}%"
    out = []
    sql = ("SELECT r.*, c.name company_name FROM lakshya_records r LEFT JOIN companies c ON c.id=r.company_id WHERE "
           "(lower(r.title) LIKE ? OR lower(COALESCE(r.body,'')) LIKE ? OR lower(r.data_json) LIKE ?)")
    args: list = [like, like, like]
    if kind and kind != "signal":
        sql += " AND r.kind=?"
        args.append(kind)
    if kind != "signal":
        out = [_row(r) for r in con.execute(sql + " ORDER BY r.kind, r.id LIMIT ?", (*args, limit))]
    if kind in (None, "", "signal"):
        for s in con.execute("SELECT id, external_id, company_id, company_name, type, summary, source_url, industry, "
                             "region, captured_at, verification_status FROM signals WHERE provenance=? AND "
                             "(lower(summary) LIKE ? OR lower(COALESCE(company_name,'')) LIKE ? OR "
                             "lower(COALESCE(industry,'')) LIKE ?) ORDER BY id LIMIT ?",
                             (PROVENANCE, like, like, like, limit)):
            out.append({"kind": "signal", "id": s["id"], "external_id": s["external_id"],
                        "company_id": s["company_id"], "company_name": s["company_name"],
                        "title": f"{s['type']}: {s['company_name'] or 'market'}", "body": s["summary"],
                        "source_url": s["source_url"], "as_of": AS_OF, "age_days": _age_days(), "historical": True,
                        "data": {"industry": s["industry"], "region": s["region"],
                                 "verification_status": s["verification_status"]}})
    return out


def summary(con) -> dict:
    ensure(con)
    last = con.execute("SELECT * FROM lakshya_imports ORDER BY id DESC LIMIT 1").fetchone()
    return {"as_of": AS_OF, "age_days": _age_days(),
            "counts": {**{r[0]: r[1] for r in con.execute("SELECT kind, COUNT(*) FROM lakshya_records GROUP BY kind")},
                       "signal": con.execute("SELECT COUNT(*) FROM signals WHERE provenance=?",
                                             (PROVENANCE,)).fetchone()[0]},
            "people": con.execute("SELECT COUNT(DISTINCT person_id) FROM person_sources WHERE source=?",
                                  (PEOPLE_SOURCE,)).fetchone()[0],
            "last_import": ({"at": last["at"], "by": last["by"], "root": last["root"],
                             "files": json.loads(last["files_json"]),
                             "validation": json.loads(last["result_json"]).get("validation")} if last else None),
            "configured_root": str(locate() or "") or None}


def context(con, company_id: int, product_id: str | None = None, *, max_chars: int = 2600) -> str:
    """Compact text for model prompts (qualification brief, draft): what the Lakshya analysts recorded for this account,
    each line labelled HISTORICAL with its date. Empty string when there is nothing."""
    recs = for_company(con, company_id)
    if not recs:
        return ""
    lines = [f"Lakshya analyst research as of {AS_OF} ({_age_days()} days old; historical context, not current "
             "buying intent; verify before relying on it):"]
    for r in recs:
        d = r["data"]
        if r["kind"] == "account":
            lines.append(f"- [L{r['id']}] Lakshya stage then: {d.get('stage')}")
        elif r["kind"] == "lead" and (product_id in (None, r["product_id"])):
            lines.append(f"- [L{r['id']}] Lead (priority {d.get('priority')}, {d.get('lead_type')}). Need: {r['body']} "
                         f"Why then: {d.get('why_now')} Part of Setu: {d.get('setu_part')} Proof to bring: "
                         f"{d.get('proof_to_bring')} Risk: {d.get('risk')}"
                         + (f" (act-by date {d.get('act_by')} has passed)" if r.get("act_by_passed") else ""))
        elif r["kind"] == "deep_dive":
            lines.append(f"- [L{r['id']}] {r['title']}: {r['body'] or ''}")
    text = "\n".join(lines)
    return text[:max_chars]
