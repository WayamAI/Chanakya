"""Master CRM workbook: the 10 sheets of the product spec (13.1), read straight from the stored records.

Summary, Companies, Contacts, Opportunities, Signals, Research, Email Enrichment, Outreach, Sources, Activity.
Filters (all optional): company ids, accelerator (product id), origin (top_down / bottom_up / both), qualification
status. Every sheet has bold frozen headers, an autofilter and readable widths; the Summary records export time,
filters, row counts and the database file so the export can be checked against the database. `export_one` writes a
single named view (spec 13.2) as xlsx or csv.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from chanakya.storage.db import now

ORIGIN = ("(SELECT CASE WHEN COUNT(DISTINCT d.direction)>1 THEN 'both' ELSE MAX(d.direction) END FROM discovery_events d "
          "WHERE d.entity='company' AND d.entity_id=c.id)")


def _ensure(con):
    con.execute("CREATE TABLE IF NOT EXISTS account_analyses (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL "
                "REFERENCES companies(id), model TEXT NOT NULL, analysis_json TEXT NOT NULL, created_by TEXT, "
                "created_at TEXT NOT NULL)")


def _companies(con, f):
    _ensure(con)
    where, args = ["1=1"], []
    if f.get("company_ids"):
        where.append(f"c.id IN ({','.join('?' * len(f['company_ids']))})")
        args += list(f["company_ids"])
    if f.get("qualification_status"):
        where.append("COALESCE(c.qualification_status,'')=?")
        args.append(f["qualification_status"])
    if f.get("product_id"):
        where.append("EXISTS (SELECT 1 FROM opportunities o WHERE o.company_id=c.id AND o.product_id=?)")
        args.append(f["product_id"])
    if f.get("origin"):
        where.append(f"COALESCE({ORIGIN},'top_down')=?")
        args.append(f["origin"])
    return [r[0] for r in con.execute(f"SELECT c.id FROM companies c WHERE {' AND '.join(where)} ORDER BY c.id", args)]


J = "json_extract(c.data_json,'$.{k}')"
LIVE = "CASE WHEN s.provenance LIKE '%lakshya%' THEN 'historical' ELSE 'live' END"

SHEETS = {
    "Companies": f"""SELECT c.id "Company ID", c.name "Company name", COALESCE({J.format(k='website')},'https://'||c.domain)
        "Official website", c.domain "Domain", {J.format(k='industry')} "Industry", {J.format(k='country')} "Country",
        {J.format(k='locality')} "Geography", {J.format(k='size')} "Company size", {J.format(k='description')} "Description",
        (SELECT group_concat(DISTINCT source) FROM company_sources s WHERE s.company_id=c.id) "Discovery source",
        COALESCE({ORIGIN},'top_down') "Discovery direction",
        (SELECT group_concat(o.product_id||':'||o.score, '; ') FROM opportunities o WHERE o.company_id=c.id AND o.score>0)
        "Accelerator relevance", c.identity_status "Verification status",
        (SELECT MAX(o.score) FROM opportunities o WHERE o.company_id=c.id) "Qualification score",
        CASE WHEN (SELECT MAX(o.score) FROM opportunities o WHERE o.company_id=c.id)>=60 THEN 'High'
             WHEN (SELECT MAX(o.score) FROM opportunities o WHERE o.company_id=c.id)>=40 THEN 'Medium' ELSE 'Low' END "Priority",
        CASE WHEN EXISTS (SELECT 1 FROM account_analyses a WHERE a.company_id=c.id) THEN 'researched' ELSE 'not researched' END
        "Research status", (SELECT MAX(a.created_at) FROM account_analyses a WHERE a.company_id=c.id) "Last researched",
        c.first_seen "Date discovered" FROM companies c WHERE c.id IN ({{IDS}}) ORDER BY c.id""",
    "Contacts": """SELECT 'R'||r.id "Contact ID", p.id "Person ID", p.full_name "Full name", p.linkedin_url "LinkedIn URL", r.title "Current designation",
        c.name "Company", json_extract(r.role_match_json,'$.department') "Department",
        (SELECT json_extract(pr.analysis_json,'$.summary') FROM profile_research pr WHERE pr.person_id=p.id ORDER BY pr.id DESC
         LIMIT 1) "Professional background", COALESCE(r.seniority, CASE WHEN r.title LIKE '%Chief%' OR r.title LIKE '%CEO%' OR r.title LIKE '%CTO%' OR r.title LIKE '%CIO%' OR r.title LIKE '%COO%' OR r.title LIKE '%CFO%' THEN 'c_suite' WHEN r.title LIKE '%Director%' THEN 'director' WHEN r.title LIKE '%Head%' THEN 'head' WHEN r.title LIKE '%Chair%' OR r.title LIKE '%Board%' THEN 'board' ELSE 'other' END) "Decision-maker category",
        json_extract(r.role_match_json,'$.reason') "Role relevance", p.verification "Identity verification",
        (SELECT group_concat(DISTINCT ps.source) FROM person_sources ps WHERE ps.person_id=p.id) "Discovery source",
        p.first_seen "Discovery date", r.review_status "Approval status"
        FROM person_roles r JOIN people p ON p.id=r.person_id JOIN companies c ON c.id=r.company_id
        WHERE c.id IN ({IDS}) ORDER BY c.id, p.id""",
    "Opportunities": """SELECT o.id "Opportunity ID", c.name "Company", json_extract(o.explanation_json,'$.need') "Business requirement",
        json_extract(o.explanation_json,'$.unknowns') "Unknowns / problem gaps", o.product_id "Accelerator",
        o.need_fit "Need fit (40)", o.timing "Timing (20)", o.access "Access (20)", o.proof_fit "Proof (20)", o.score "Total score",
        CASE WHEN o.score>=60 THEN 'High' WHEN o.score>=40 THEN 'Medium' ELSE 'Low' END "Priority",
        json_extract(o.explanation_json,'$.confidence') "Confidence", json_extract(o.explanation_json,'$.facts') "Evidence",
        o.status "Qualification status",
        (SELECT h.status||' by '||h.reviewer FROM human_reviews h WHERE h.company_id=c.id ORDER BY h.id DESC LIMIT 1) "Human decision",
        CASE WHEN o.score>=60 THEN 'Contact decision-makers' WHEN o.score>=40 THEN 'Research further' ELSE 'Monitor' END "Next action",
        o.origin "Origin", o.computed_at "Computed at"
        FROM opportunities o JOIN companies c ON c.id=o.company_id WHERE o.company_id IN ({IDS}) ORDER BY o.score DESC""",
    "Signals": f"""SELECT s.id "Signal ID", COALESCE(c.name, s.company_name) "Company", s.type "Signal category",
        s.summary "Description", s.provenance "Source", s.source_url "Source URL", s.source_date "Publication date",
        s.captured_at "Retrieval date", s.urgency "Relevance",
        (SELECT group_concat(f.product_id) FROM signal_fits f WHERE f.signal_id=s.id AND f.score>=60) "Accelerator",
        s.verification_status "Verification status", {LIVE} "Historical/live"
        FROM signals s LEFT JOIN companies c ON c.id=s.company_id WHERE s.company_id IN ({{IDS}}) ORDER BY s.company_id, s.source_date DESC""",
    "Research": """SELECT 'A'||a.id "Research ID", c.name "Company", NULL "Person", 'company' "Research type",
        json_extract(a.analysis_json,'$.summary') "Research summary", a.analysis_json "Findings",
        NULL "Source URLs", json_extract(a.analysis_json,'$.facts') "Facts", json_extract(a.analysis_json,'$.inferences') "Inferences",
        json_extract(a.analysis_json,'$.unknowns') "Unknowns", a.model "Model used", a.created_at "Research date", 'model output' "Verification status"
        FROM account_analyses a JOIN companies c ON c.id=a.company_id WHERE a.company_id IN ({IDS})
        UNION ALL SELECT 'P'||pr.id, c.name, p.full_name, 'person', json_extract(pr.analysis_json,'$.summary'), pr.analysis_json,
        pr.profile_url, pr.facts_json, NULL, pr.dropped_json, pr.model, pr.created_at, pr.review_status
        FROM profile_research pr JOIN people p ON p.id=pr.person_id JOIN person_roles r ON r.person_id=p.id
        JOIN companies c ON c.id=r.company_id WHERE c.id IN ({IDS}) GROUP BY pr.id""",
    "Email Enrichment": """SELECT p.full_name "Contact", c.name "Company", (SELECT r.title FROM person_roles r WHERE r.person_id=p.id
        LIMIT 1) "Designation", e.email "Email address", COALESCE(e.source_url, e.method) "Email source", e.status "Verification status",
        e.method "Enrichment provider", e.created_at "Enrichment date", e.mx_json "MX status", e.review_status "Approval status"
        FROM email_records e JOIN people p ON p.id=e.person_id JOIN companies c ON c.id=e.company_id
        WHERE e.company_id IN ({IDS}) ORDER BY e.id""",
    "Outreach": """SELECT d.id "Draft ID", c.name "Company", p.full_name "Contact", r.title "Designation", d.to_address "Email recipient",
        json_extract(d.hooks_json,'$.product_id') "Accelerator", json_extract(d.hooks_json,'$.opportunity') "Opportunity",
        d.subject "Email subject", d.body "Complete email body", d.hooks_json "Personalisation context",
        json_extract(d.hooks_json,'$.evidence') "Supporting evidence", d.status "Draft status",
        (SELECT h.action FROM outreach_draft_history h WHERE h.draft_id=d.id ORDER BY h.id DESC LIMIT 1) "Review status",
        CASE WHEN d.status IN ('approved','ready','saved_to_outlook') THEN 'approved' WHEN d.status IN ('rejected','discarded')
             THEN 'rejected' ELSE 'pending' END "Approval status",
        CASE WHEN d.outlook_message_id IS NOT NULL THEN 'saved' ELSE 'not saved' END "Outlook status",
        d.outlook_message_id "Outlook draft ID", d.created_at "Created", d.updated_at "Modified"
        FROM outreach_drafts d JOIN people p ON p.id=d.person_id JOIN person_roles r ON r.person_id=p.id
        JOIN companies c ON c.id=r.company_id WHERE c.id IN ({IDS}) GROUP BY d.id ORDER BY d.id""",
    "Sources": """SELECT 'C'||s.id "Source ID", s.source "Source name", s.source_url "URL", c.name "Company/contact",
        s.tool "Source type", NULL "Publication date", s.fetched_at "Retrieval date", 'accepted' "Evidence status", NULL "Rejection reason"
        FROM company_sources s JOIN companies c ON c.id=s.company_id WHERE s.company_id IN ({IDS})
        UNION ALL SELECT 'S'||s.id, s.provenance, s.source_url, COALESCE(c.name,s.company_name), s.type, s.source_date, s.captured_at,
        s.verification_status, json_extract(s.verification_json,'$.reason')
        FROM signals s LEFT JOIN companies c ON c.id=s.company_id WHERE s.company_id IN ({IDS})""",
    "LinkedIn Prospects": """SELECT p.id "Person ID", c.name "Company", COALESCE(json_extract(c.data_json,'$.website'),
        c.domain) "Company website", json_extract(c.data_json,'$.industry') "Industry", json_extract(c.data_json,'$.locality')
        "Location", json_extract(c.data_json,'$.linkedin_url') "Company LinkedIn", p.full_name "Name", r.title "Designation",
        p.linkedin_url "LinkedIn profile",
        (SELECT e.email FROM email_records e WHERE e.person_id=p.id ORDER BY e.id DESC LIMIT 1) "Email",
        (SELECT e.status||' ('||e.method||')' FROM email_records e WHERE e.person_id=p.id ORDER BY e.id DESC LIMIT 1)
        "Email status", (SELECT json_extract(d.hooks_json,'$.accelerator') FROM outreach_drafts d WHERE d.person_id=p.id
        ORDER BY d.id DESC LIMIT 1) "Accelerator",
        (SELECT d.subject FROM outreach_drafts d WHERE d.person_id=p.id ORDER BY d.id DESC LIMIT 1) "Draft subject",
        (SELECT d.body FROM outreach_drafts d WHERE d.person_id=p.id ORDER BY d.id DESC LIMIT 1) "Draft body",
        (SELECT d.status FROM outreach_drafts d WHERE d.person_id=p.id ORDER BY d.id DESC LIMIT 1) "Draft status",
        (SELECT CASE WHEN d.outlook_message_id IS NOT NULL THEN 'saved' ELSE 'not saved' END FROM outreach_drafts d
         WHERE d.person_id=p.id ORDER BY d.id DESC LIMIT 1) "Outlook", MIN(ps.fetched_at) "Found on LinkedIn"
        FROM person_sources ps JOIN people p ON p.id=ps.person_id JOIN person_roles r ON r.person_id=p.id AND
        r.company_id=ps.company_id JOIN companies c ON c.id=ps.company_id WHERE ps.tool='visible-chrome' AND
        c.id IN ({IDS}) GROUP BY p.id, c.id ORDER BY c.name, p.full_name""",
    "Activity": """SELECT 'W'||e.id "Activity ID", e.workflow_id "Workflow ID", e.stage "Agent", e.message "Action", NULL "Company/person",
        e.at "Timestamp", e.level "Result", NULL "Reviewer", NULL "Decision", NULL "Reason",
        CASE WHEN e.level='error' THEN e.message END "Error" FROM workflow_events e
        UNION ALL SELECT 'R'||h.id, NULL, 'Human review', 'qualification', c.name, h.reviewed_at, h.status, h.reviewer, h.status, h.reason, NULL
        FROM human_reviews h JOIN companies c ON c.id=h.company_id
        UNION ALL SELECT 'D'||h.id, NULL, 'Outreach', h.action, NULL, h.at, h.to_status, h.reviewer, h.to_status, h.reason, NULL
        FROM outreach_draft_history h ORDER BY 6 DESC""",
}

# Separate exports (spec 13.2): name -> (base sheet, extra WHERE applied on the result columns)
VIEWS = {
    "companies": ("Companies", None), "contacts": ("Contacts", None), "opportunities": ("Opportunities", None),
    "signals": ("Signals", None), "research": ("Research", None), "email_enrichment": ("Email Enrichment", None),
    "outreach": ("Outreach", None), "sources": ("Sources", None), "audit": ("Activity", None),
    "linkedin_prospects": ("LinkedIn Prospects", None),
    "leaders": ("Contacts", "\"Decision-maker category\" <> 'other'"),
    "pending_approvals": ("Contacts", "COALESCE(\"Approval status\",'pending') NOT IN ('approved','rejected')"),
    "qualified": ("Opportunities", "\"Total score\">=40"),
    "top_down": ("Companies", "\"Discovery direction\"='top_down'"),
    "bottom_up": ("Companies", "\"Discovery direction\"='bottom_up'"),
    "combined": ("Companies", "\"Discovery direction\"='both'"),
}


def _rows(con, sql, ids, where=None):
    if "{IDS}" in sql:
        if not ids:
            ids = [-1]
        n = sql.count("{IDS}")
        sql, args = sql.replace("{IDS}", ",".join("?" * len(ids))), list(ids) * n
    else:
        args = []
    if where:
        sql = f"SELECT * FROM ({sql}) WHERE {where}"
    cur = con.execute(sql, args)
    return [d[0] for d in cur.description], [tuple(r) for r in cur.fetchall()]


def _safe(v):
    if v is None or isinstance(v, (int, float)):
        return v
    s = str(v)
    if s and s[0] in "=+-@":  # never let a stored value execute as a spreadsheet formula
        s = "'" + s
    return s[:32000]


def _sheet(wb, name, cols, rows):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    ws = wb.create_sheet(name)
    ws.append(cols)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3864")
    for r in rows:
        ws.append([_safe(v) for v in r])
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(cols, 1):
        width = max([len(str(col))] + [min(len(str(r[i - 1] or "")), 60) for r in rows[:200]])
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 62)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=len(str(c.value or "")) > 60)
    return ws


def _summary(con, ids, counts):
    q = lambda sql: con.execute(sql.replace("{IDS}", ",".join(map(str, ids or [-1])))).fetchone()[0]  # ids are ints
    dirs = {k: q(f"SELECT COUNT(*) FROM companies c WHERE c.id IN ({{IDS}}) AND COALESCE({ORIGIN},'top_down')='{k}'")
            for k in ("top_down", "bottom_up", "both")}
    d = lambda st: q(f"SELECT COUNT(*) FROM outreach_drafts WHERE status IN ({st})")
    return [("Total companies", counts["Companies"]), ("Total contacts", counts["Contacts"]),
            ("Total opportunities", counts["Opportunities"]), ("Total signals", counts["Signals"]),
            ("Top-down discoveries", dirs["top_down"]), ("Bottom-up discoveries", dirs["bottom_up"]),
            ("Both-direction discoveries", dirs["both"]),
            ("Qualified opportunities (score >= 40)",
             q("SELECT COUNT(*) FROM opportunities WHERE company_id IN ({IDS}) AND score>=40")),
            ("High-priority prospects (score >= 60)",
             q("SELECT COUNT(DISTINCT company_id) FROM opportunities WHERE company_id IN ({IDS}) AND score>=60")),
            ("Pending approvals (roles)", q("SELECT COUNT(*) FROM person_roles WHERE company_id IN ({IDS}) AND "
                                            "COALESCE(review_status,'pending') NOT IN ('approved','rejected')")),
            ("Drafts generated", d("SELECT status FROM outreach_drafts")),
            ("Drafts approved", d("'approved','ready','saved_to_outlook'")),
            ("Drafts saved to Outlook", q("SELECT COUNT(*) FROM outreach_drafts WHERE outlook_message_id IS NOT NULL"))]


def export(con, path: Path, filters: dict | None = None, db_file: str | None = None) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    f = filters or {}
    ids = _companies(con, f)
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    counts, checks = {}, []
    for name, sql in SHEETS.items():
        try:
            cols, rows = _rows(con, sql, ids)
        except Exception as e:  # noqa: BLE001 - a missing optional table is reported, not fatal
            cols, rows = ["error"], [(f"{type(e).__name__}: {e}",)]
        ids_col = [r[0] for r in rows]
        if len(ids_col) != len(set(ids_col)):
            checks.append(f"{name}: duplicate IDs")
        _sheet(wb, name, cols, rows)
        counts[name] = len(rows)
    if counts["Companies"] != len(ids):
        checks.append("Companies sheet rows differ from matched companies")
    summary.append(["Metric", "Value"])
    for c in summary[1]:
        c.font = Font(bold=True)
    for row in [*_summary(con, ids, counts), ("Last updated / exported at", now()), ("Database", db_file or ""),
                ("Filters", json.dumps(f)), *[(f"{k} rows", v) for k, v in counts.items()],
                ("Consistency checks", "; ".join(checks) or "passed")]:
        summary.append(list(row))
    summary.column_dimensions["A"].width, summary.column_dimensions["B"].width = 40, 60
    summary.freeze_panes = "A2"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return {"path": str(path), "companies": len(ids), "counts": counts, "filters": f, "checks": checks or ["passed"]}


def export_one(con, view: str, path: Path, fmt: str = "xlsx", filters: dict | None = None) -> dict:
    if view not in VIEWS:
        raise KeyError(view)
    base, where = VIEWS[view]
    cols, rows = _rows(con, SHEETS[base], _companies(con, filters or {}), where)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerows([[_safe(v) for v in r] for r in rows])
    else:
        from openpyxl import Workbook
        wb = Workbook()
        wb.remove(wb.active)
        _sheet(wb, view[:31], cols, rows)
        info = wb.create_sheet("Export info")
        for r in [("View", view), ("Exported at", now()), ("Filters", json.dumps(filters or {})), ("Rows", len(rows))]:
            info.append(list(r))
        wb.save(path)
    return {"path": str(path), "rows": len(rows)}
