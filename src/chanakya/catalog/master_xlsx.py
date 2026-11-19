"""Import Wayam_Accelerators_Master.xlsx (the sales team's master sheet) into the catalogue.

Sheets: Master (one row per accelerator), Decks, one sheet per accelerator (client and target use cases),
Managed offerings, Entry offers. Idempotent: re-importing the same or a newer file updates rows in place.

* products: existing accelerators are updated (name, area/category, tagline, problem, what it does, modules,
  integrations, headline metrics, links); new ones are added. Accelerators missing from the sheet are kept.
* target industries come from the industries of the accelerator's client use cases (labelled as derived).
* buyer roles come from the decks' "Who it is for" for decks featuring the accelerator (labelled as derived).
* client-project metrics are stored as use cases, i.e. CANDIDATE proof. They are not written to accelerator_proof:
  outreach may only use claims a human approved.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from chanakya.storage.db import now

ALIASES = {"parikshan": "PRD-PARIKSHA", "wayam eye": "PRD-WAYAM-EYE"}
TABLES = """
CREATE TABLE IF NOT EXISTS catalogue_use_cases (id INTEGER PRIMARY KEY, product_id TEXT NOT NULL, kind TEXT NOT NULL,
  title TEXT NOT NULL, industry TEXT, function TEXT, background TEXT, challenge TEXT, steps TEXT, outcomes TEXT,
  metrics TEXT, evidence TEXT, url TEXT, source_sha256 TEXT, imported_at TEXT NOT NULL, UNIQUE(product_id, title));
CREATE TABLE IF NOT EXISTS catalogue_decks (id INTEGER PRIMARY KEY, title TEXT NOT NULL UNIQUE, grp TEXT, audience TEXT,
  summary TEXT, slides TEXT, accelerators TEXT, cover_line TEXT, imported_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalogue_offers (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
  offer_type TEXT, what_it_is TEXT, scope TEXT, duration TEXT, outcome TEXT, accelerators TEXT, buyer TEXT,
  follow_on TEXT, commercials TEXT, imported_at TEXT NOT NULL, UNIQUE(kind, name));
CREATE TABLE IF NOT EXISTS catalogue_imports (id INTEGER PRIMARY KEY, source_path TEXT, source_sha256 TEXT,
  summary_json TEXT, imported_at TEXT NOT NULL);
"""


def _s(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _pid(con, name: str) -> str:
    key = name.strip().lower()
    if key in ALIASES:
        return ALIASES[key]
    row = con.execute("SELECT id FROM products WHERE lower(name)=?", (key,)).fetchone()
    return row[0] if row else "PRD-" + re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")


def _lines(v) -> list[str]:
    return [re.sub(r"^[-•\s]+", "", x).strip() for x in str(v or "").splitlines() if x.strip()]


def _header_row(ws, first: str, max_row: int = 15) -> int | None:
    for i, r in enumerate(ws.iter_rows(min_row=1, max_row=max_row, values_only=True), 1):
        if any(_s(x) == first for x in r):
            return i
    return None


def _use_cases(ws):
    """Client use cases under the first header; target use cases after the 'Target use cases' heading."""
    kind, out, header = "client", [], None
    for r in ws.iter_rows(min_row=1, values_only=True):
        cells = [_s(x) for x in r]
        joined = " ".join(c for c in cells if c)
        if joined.lower().startswith("target use cases"):
            kind = "target"
            continue
        if cells[:2] == ["#", "Use case"]:
            header = cells
            continue
        if header and len(cells) > 1 and cells[1] and cells[0] and re.match(r"^T?\d+$", cells[0]):
            row = dict(zip(header, cells))
            if kind == "target":  # columns: Use case, Buyer, Pain it removes, Readiness today, Entry offer
                out.append({"kind": kind, "title": row.get("Use case"), "industry": None,
                            "function": row.get("Buyer"), "background": None, "challenge": row.get("Pain it removes"),
                            "steps": None, "outcomes": row.get("Entry offer"), "metrics": None,
                            "evidence": "Target: " + (row.get("Readiness today") or "not yet delivered"), "url": None})
                continue
            out.append({"kind": kind, "title": row.get("Use case"), "industry": row.get("Industry"),
                        "function": row.get("Function"), "background": row.get("Client background"),
                        "challenge": row.get("Challenge"), "steps": row.get("How it works (steps)"),
                        "outcomes": row.get("Outcomes"), "metrics": row.get("Key metrics"),
                        "evidence": row.get("Evidence"), "url": row.get("Read more on wayam.ai")})
    return out


def import_master(con, path: str | Path, by: str = "import") -> dict:
    from openpyxl import load_workbook
    path = Path(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    con.executescript(TABLES)
    wb = load_workbook(path, data_only=True)
    t = now()
    rep = {"accelerators_updated": [], "accelerators_added": [], "use_cases": 0, "decks": 0, "offers": 0,
           "kept_not_in_sheet": []}

    # decks first: buyer roles per accelerator are derived from them
    decks = []
    if "Decks" in wb.sheetnames:
        ws = wb["Decks"]
        h = _header_row(ws, "Deck")
        hdr = [_s(x) for x in next(ws.iter_rows(min_row=h, max_row=h, values_only=True))]
        for r in ws.iter_rows(min_row=h + 1, values_only=True):
            d = dict(zip(hdr, [_s(x) for x in r]))
            if not d.get("Deck") or not (d.get("#") or "").isdigit():
                continue
            decks.append(d)
            con.execute("INSERT INTO catalogue_decks(title,grp,audience,summary,slides,accelerators,cover_line,imported_at) "
                        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(title) DO UPDATE SET grp=excluded.grp, audience=excluded.audience, "
                        "summary=excluded.summary, slides=excluded.slides, accelerators=excluded.accelerators, "
                        "cover_line=excluded.cover_line, imported_at=excluded.imported_at",
                        (d["Deck"], d.get("Group"), d.get("Who it is for"), d.get("Summary"), d.get("Slides"),
                         d.get("Accelerators featured"), d.get("Cover line"), t))
            rep["decks"] += 1

    ws = wb["Master"]
    h = _header_row(ws, "Accelerator")
    hdr = [_s(x) for x in next(ws.iter_rows(min_row=h, max_row=h, values_only=True))]
    seen = set()
    for r in ws.iter_rows(min_row=h + 1, values_only=True):
        m = dict(zip(hdr, [_s(x) for x in r]))
        name = m.get("Accelerator")
        if not name or not (m.get("#") or "").isdigit():
            continue
        pid = _pid(con, name)
        seen.add(pid)
        ucs = _use_cases(wb[name]) if name in wb.sheetnames else []
        for u in ucs:
            if not u["title"]:
                continue
            con.execute("INSERT INTO catalogue_use_cases(product_id,kind,title,industry,function,background,challenge,steps,"
                        "outcomes,metrics,evidence,url,source_sha256,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(product_id,title) DO UPDATE SET kind=excluded.kind, industry=excluded.industry, "
                        "function=excluded.function, background=excluded.background, challenge=excluded.challenge, "
                        "steps=excluded.steps, outcomes=excluded.outcomes, metrics=excluded.metrics, "
                        "evidence=excluded.evidence, url=excluded.url, source_sha256=excluded.source_sha256, "
                        "imported_at=excluded.imported_at",
                        (pid, u["kind"], u["title"], u["industry"], u["function"], u["background"], u["challenge"],
                         u["steps"], u["outcomes"], u["metrics"], u["evidence"], u["url"], sha, t))
            rep["use_cases"] += 1
        industries = sorted({u["industry"] for u in ucs if u["industry"]})
        if not industries:  # no client projects yet: industries of the industry decks that feature it
            industries = sorted({re.sub(r"^Wayam AI for (the )?", "", d["Deck"]) for d in decks
                                 if d.get("Group") == "Industry" and name.lower() in (d.get("Accelerators featured") or "").lower()})
        buyers = sorted({re.sub(r"\s*\(.*?\)", "", b).strip() for d in decks
                         if name.lower() in (d.get("Accelerators featured") or "").lower()
                         for b in re.split(r"[;,]|\balso\b", d.get("Who it is for") or "") if b.strip()}
                        | {u["function"] for u in ucs if u["kind"] == "target" and u["function"]})
        sheet = {k: m.get(k) for k in hdr if k}
        fields = dict(
            name=name, status="active", summary=m.get("What it does"),
            problems_solved_json=json.dumps([m["Problem it solves"]] if m.get("Problem it solves") else []),
            target_industries_json=json.dumps(industries),
            target_industries_note="derived from the industries of this accelerator's use cases in the master sheet",
            integrations_json=json.dumps([x.strip() for x in (m.get("Integrations") or "").split(",") if x.strip()]),
            differentiators_json=json.dumps(_lines(m.get("Modules"))),
            evidence_json=json.dumps({"headline_metrics": m.get("Headline metrics"), "client_use_cases":
                                      sum(1 for u in ucs if u["kind"] == "client"),
                                      "note": "candidate proof from the master sheet; approve before outreach uses it"}))
        row = con.execute("SELECT raw_json, buyer_roles_json FROM products WHERE id=?", (pid,)).fetchone()
        if row:
            raw = json.loads(row["raw_json"] or "{}") if row["raw_json"] else {}
            raw["master_sheet"] = sheet
            br = json.loads(row["buyer_roles_json"] or "null") or buyers
            con.execute("UPDATE products SET name=?, status=?, summary=?, problems_solved_json=?, target_industries_json=?, "
                        "target_industries_note=?, integrations_json=?, differentiators_json=?, evidence_json=?, "
                        "buyer_roles_json=?, raw_json=?, catalogue_version=?, imported_at=? WHERE id=?",
                        (*fields.values(), json.dumps(br), json.dumps(raw), "master-sheet-2026-11-10", t, pid))
            rep["accelerators_updated"].append(name)
        else:
            con.execute("INSERT INTO products(id,name,status,summary,problems_solved_json,target_industries_json,"
                        "target_industries_note,integrations_json,differentiators_json,evidence_json,buyer_roles_json,"
                        "verification,source_path,source_sha256,catalogue_version,raw_json,imported_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, *fields.values(), json.dumps(buyers), "imported from the master sheet (sales team)",
                         str(path.name), sha, "master-sheet-2026-11-10", json.dumps({"master_sheet": sheet}), t))
            rep["accelerators_added"].append(name)
    rep["kept_not_in_sheet"] = [r[0] for r in con.execute("SELECT name FROM products") if
                                _pid(con, r[0]) not in seen]

    for sheet_name, kind, first in (("Managed offerings", "managed", "Offering"), ("Entry offers", "entry", "Entry offer")):
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        h = _header_row(ws, first)
        if not h:
            continue
        hdr = [_s(x) for x in next(ws.iter_rows(min_row=h, max_row=h, values_only=True))]
        for r in ws.iter_rows(min_row=h + 1, values_only=True):
            d = dict(zip(hdr, [_s(x) for x in r]))
            nm = d.get(first)
            if not nm or nm == first:
                continue
            con.execute("INSERT INTO catalogue_offers(kind,name,offer_type,what_it_is,scope,duration,outcome,accelerators,"
                        "buyer,follow_on,commercials,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,name) "
                        "DO UPDATE SET offer_type=excluded.offer_type, what_it_is=excluded.what_it_is, scope=excluded.scope, "
                        "duration=excluded.duration, outcome=excluded.outcome, accelerators=excluded.accelerators, "
                        "buyer=excluded.buyer, follow_on=excluded.follow_on, commercials=excluded.commercials, "
                        "imported_at=excluded.imported_at",
                        (kind, nm, d.get("Type") or d.get("Family"), d.get("What it is"), d.get("Scope") or
                         d.get("Scope options"), d.get("Typical duration"), d.get("You leave with"),
                         d.get("Accelerators"), d.get("Typical buyer"), d.get("Follow-on"), d.get("Commercials"), t))
            rep["offers"] += 1
    con.execute("INSERT INTO catalogue_imports(source_path,source_sha256,summary_json,imported_at) VALUES(?,?,?,?)",
                (str(path.name), sha, json.dumps(rep), t))
    con.commit()
    return rep


def use_cases(con, product_id: str, kind: str | None = None) -> list[dict]:
    con.executescript(TABLES)
    q = "SELECT * FROM catalogue_use_cases WHERE product_id=?" + (" AND kind=?" if kind else "") + " ORDER BY id"
    return [dict(r) for r in con.execute(q, (product_id, kind) if kind else (product_id,))]
