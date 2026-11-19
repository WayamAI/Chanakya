"""Gap intelligence (Lakshya "What should I build").

A gap is a real need where no accelerator scores 60 or above (settings.gap_fit_threshold). Signals flagged as a gap
theme (analyst or model) and not noise feed a theme. Verdict (artifact verdict()):
  Ready to pre-sell  : >= 3 distinct accounts AND >= 2 distinct signal types
  Building evidence  : >= 2 distinct accounts
  Early signal       : otherwise
The verdict is computed; the decision (watching / pre-selling / building / parked) is a separate human field. One weak
match for one prospect never creates a gap: a theme needs the signal flagged as a gap AND best fit below the threshold.
"""
from __future__ import annotations

import json

from chanakya.intel import settings as S
from chanakya.intel.entities import slug
from chanakya.storage.db import jdump, now


def verdict(con, accounts: int, types: int) -> str:
    if accounts >= S.get(con, "presell_min_accounts") and types >= S.get(con, "presell_min_signal_types"):
        return "ready_to_presell"
    if accounts >= S.get(con, "building_min_accounts"):
        return "building_evidence"
    return "early_signal"


def recompute(con) -> list[dict]:
    thr = S.get(con, "gap_fit_threshold")
    themes: dict[str, dict] = {}
    for s in con.execute("SELECT * FROM signals WHERE is_gap=1 AND noise=0 AND gap_theme IS NOT NULL "
                         "AND verification_status IS NOT 'rejected'"):
        best = con.execute("SELECT MAX(score) FROM signal_fits WHERE signal_id=?", (s["id"],)).fetchone()[0] or 0
        if best >= thr:
            continue  # an accelerator already covers it: not a gap
        t = themes.setdefault(slug(s["gap_theme"]), {"name": s["gap_theme"], "needs": [], "reuse": [], "companies": set(),
                                                     "types": set(), "signals": [], "best": 0})
        if s["company_id"]:  # market-level signals (no named account) support the theme but are not accounts
            t["companies"].add(s["company_id"])
        t["types"].add(s["type"])
        t["signals"].append(s["id"])
        t["best"] = max(t["best"], best)
        if s["gap_need"]:
            t["needs"].append(s["gap_need"])
        if s["reuse"]:
            t["reuse"].append(s["reuse"])
    out = []
    for key, t in themes.items():
        v = verdict(con, len(t["companies"]), len(t["types"]))
        con.execute("INSERT INTO gap_themes(key,name,need,reuse_json,verdict,accounts,signal_types,best_fit,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name, need=excluded.need, "
                    "reuse_json=excluded.reuse_json, verdict=excluded.verdict, accounts=excluded.accounts, "
                    "signal_types=excluded.signal_types, best_fit=excluded.best_fit, updated_at=excluded.updated_at",
                    (key, t["name"], (t["needs"] or [None])[0], jdump(sorted(set(t["reuse"]))), v, len(t["companies"]),
                     len(t["types"]), t["best"], now()))
        tid = con.execute("SELECT id FROM gap_themes WHERE key=?", (key,)).fetchone()[0]
        for sid in t["signals"]:
            con.execute("INSERT OR IGNORE INTO gap_evidence(theme_id,signal_id) VALUES(?,?)", (tid, sid))
        out.append({"key": key, "name": t["name"], "verdict": v, "accounts": len(t["companies"]),
                    "signal_types": sorted(t["types"]), "best_fit": t["best"]})
    con.commit()
    return sorted(out, key=lambda x: (x["verdict"] != "ready_to_presell", -x["accounts"]))


def decide(con, theme_id: int, status: str, *, by: str, reason: str) -> None:
    if status not in ("watching", "pre_selling", "building", "parked"):
        raise ValueError("status must be watching, pre_selling, building or parked")
    if not (by or "").strip() or not (reason or "").strip():
        raise ValueError("reviewer and reason are required")
    con.execute("UPDATE gap_themes SET decision_status=?, decision_by=?, decision_reason=?, updated_at=? WHERE id=?",
                (status, by, reason, now(), theme_id))
    con.commit()


def themes(con) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM gap_themes ORDER BY (verdict='ready_to_presell') DESC, accounts DESC"):
        d = dict(r)
        d["reuse"] = json.loads(d.pop("reuse_json") or "[]")
        d["evidence"] = [dict(x) for x in con.execute(
            "SELECT s.id, s.type, s.company_name, s.company_id, s.summary, s.source_url, s.source_date, "
            "s.verification_status FROM gap_evidence g JOIN signals s ON s.id=g.signal_id WHERE g.theme_id=?", (r["id"],))]
        out.append(d)
    return out
