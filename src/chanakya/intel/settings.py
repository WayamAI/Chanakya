"""Configurable methodology values (Lakshya handover, Vineet Shukla, 4 Nov 2026), stored in the `settings` table.

Defaults are the documented values. Every change is stored with who changed it and when.
"""
from __future__ import annotations

import json

from chanakya.storage.db import now

# Eight signal types and initial weights (handover section 4; Lakshya artifact TYPES).
SIGNAL_TYPES = {
    "tender": {"label": "Tender or RFP", "weight": 1.0, "tells": "Budget exists and a deadline is set."},
    "conversation": {"label": "Client conversation", "weight": 0.9, "tells": "Someone asked you for it directly."},
    "hiring": {"label": "Hiring post", "weight": 0.8, "tells": "They plan to fund this. A new role is a budget line."},
    "regulation": {"label": "Regulation or standard", "weight": 0.7, "tells": "A rule with a date forces spend."},
    "filing": {"label": "Filing or earnings call", "weight": 0.7, "tells": "Leadership says in public what hurts."},
    "exec": {"label": "Executive move", "weight": 0.6, "tells": "A new CTO, CDO or plant head buys in the first 100 days."},
    "news": {"label": "Trigger news", "weight": 0.5, "tells": "Plant launch, recall, outage, breach, merger, funding."},
    "post": {"label": "Problem post", "weight": 0.4, "tells": "Someone states the pain in public. Weakest intent."},
}
DEFAULTS = {
    "signal_weights": {k: v["weight"] for k, v in SIGNAL_TYPES.items()},
    "recency_half_scale_days": 60,          # recency = exp(-days / 60)
    # The handover's artifact decayed from the CAPTURE date and listed that as a known issue; the user asked for the
    # SOURCE date. Default: source date, falling back to the capture date (flagged) when no source date is known.
    "recency_basis": "source_date",
    "gap_fit_threshold": 60,                # a gap = a real need where no accelerator scores 60+
    "presell_min_accounts": 3,              # Ready to pre-sell: >= 3 accounts ...
    "presell_min_signal_types": 2,          # ... and >= 2 distinct signal types
    "building_min_accounts": 2,             # Building evidence: >= 2 accounts
    "fit_count_min": 40,                    # fits below 40 do not count (artifact rule)
    "public_naming_allowed_from": "2027-01-01",  # naming NDA (handover: "about Jan 2027"); verify with Vineet
    "linkedin_automation": "disabled",      # handover: "Never scrape LinkedIn (terms)"; see compliance.py
    # Defaults every new run starts with (Settings > Run settings). A run's own params override them.
    "run_defaults": {"company_signal_limit": 5, "classify_limit": 5, "brief_limit": 2, "site_people_limit": 3,
                     "tender_days": 120, "job_days": 60, "auto_resolve_limit": 15},
}


def get(con, key: str):
    r = con.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else DEFAULTS[key]


def put(con, key: str, value, *, by: str) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key!r}")
    if not (by or "").strip():
        raise ValueError("a named person must change settings")
    con.execute("INSERT INTO settings(key,value_json,updated_by,updated_at) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE "
                "SET value_json=excluded.value_json, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (key, json.dumps(value), by, now()))
    con.commit()


def all_settings(con) -> dict:
    out = {}
    for k in DEFAULTS:
        r = con.execute("SELECT value_json, updated_by, updated_at FROM settings WHERE key=?", (k,)).fetchone()
        out[k] = {"value": json.loads(r[0]) if r else DEFAULTS[k], "default": DEFAULTS[k],
                  "updated_by": r[1] if r else None, "updated_at": r[2] if r else None}
    return out


def weight(con, signal_type: str) -> float:
    w = get(con, "signal_weights")
    return float(w.get(signal_type, w.get("post", 0.4)))
