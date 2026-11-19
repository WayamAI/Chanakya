"""Lakshya scoring: account heat, accelerator ladder (artifact formulas) and the 100-point opportunity model (Studio).

Opportunity score = need fit (0-40) + timing (0-20) + access (0-20) + proof fit (0-20). Studio asked a model for these;
here every component is computed from stored evidence by a documented rule, so nothing can be inflated by wording:

  need_fit  = 0.4 x best accelerator fit among the company's non-noise signals (fit 0-100, method recorded).
              If there is no signal fit but a grounded research/bottom-up match exists: 15 (marked as inference).
  timing    = 20 x recency of the freshest signal that fits this accelerator (exp(-days/60), source date basis).
              Undated signals (no source date, capture-date fallback) get half: Studio's rubric says "old or undated
              signals score low". The basis is always listed in the explanation.
  access    = approved role at the company 8 (+4 if more than one) + human-approved LinkedIn 4
              + approved email 4 (valid) / 2 (risky or unverified) + budget signal 4 (explicit) / 2 (likely); max 20.
              With no people yet, owner titles named in signals give 2.
  proof_fit = 0 if the accelerator has no registered proof; 10 if it has registered proof; 20 if a registered proof's
              tool/industry terms also appear in this company's signals. Forbidden claims never count.

Every opportunity stores facts (signal ids, URLs, dates), inferences and unknowns, plus a history row on change.
"""
from __future__ import annotations

import json
import re

from chanakya.intel import entities as E
from chanakya.intel import settings as S
from chanakya.intel.signals import recency, signal_strength
from chanakya.storage.db import jdump, now


def company_signals(con, company_id: int, include_noise=False) -> list:
    # Human-rejected signals are kept for the audit trail but never count as evidence.
    return con.execute("SELECT * FROM signals WHERE company_id=? AND verification_status IS NOT 'rejected'" +
                       ("" if include_noise else " AND noise=0") + " ORDER BY id", (company_id,)).fetchall()


def account_heat(con, company_id: int) -> dict:
    """Artifact: score += weight x recency x (best/100 or 0.2); heat = score + max urgency x 0.06."""
    score, urg, types = 0.0, 0, set()
    for s in company_signals(con, company_id):
        best = con.execute("SELECT MAX(score) FROM signal_fits WHERE signal_id=?", (s["id"],)).fetchone()[0] or 0
        st = signal_strength(con, s)
        score += st["strength"] * ((best / 100) or 0.2)
        urg = max(urg, s["urgency"] or 0)
        types.add(s["type"])
    return {"heat": round(score + urg * 0.06, 4), "signals": len(company_signals(con, company_id)),
            "signal_types": sorted(types), "max_urgency": urg}


def accelerator_ladder(con) -> list[dict]:
    """Artifact: per accelerator, sum of weight x (fit/100) x recency over fits of 40+ (noise excluded)."""
    floor = S.get(con, "fit_count_min")
    agg: dict[str, dict] = {}
    for r in con.execute("SELECT f.product_id, f.score, s.* FROM signal_fits f JOIN signals s ON s.id=f.signal_id "
                         "WHERE s.noise=0 AND s.verification_status IS NOT 'rejected' AND f.score>=?", (floor,)):
        a = agg.setdefault(r["product_id"], {"product_id": r["product_id"], "score": 0.0, "signals": 0, "companies": set()})
        a["score"] += signal_strength(con, r)["strength"] * r["score"] / 100
        a["signals"] += 1
        if r["company_id"]:
            a["companies"].add(r["company_id"])
    out = [{**a, "score": round(a["score"], 4), "companies": len(a["companies"])} for a in agg.values()]
    return sorted(out, key=lambda a: -a["score"])


# Words every BI-migration claim and need share; they say nothing about WHICH platform the proof is for.
_GENERIC_PROOF_WORDS = {"estate", "dashboards", "dashboard", "migrated", "migration", "migrations", "migrate",
                        "retired", "weeks", "savings", "licence", "license", "auto", "utility", "platform"}


def _proof_terms(claim: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", claim.lower())) - _GENERIC_PROOF_WORDS


def score_opportunity(con, company_id: int, product_id: str) -> dict:
    facts, inferences, unknowns = [], [], []
    sigs = company_signals(con, company_id)
    fitted = []
    for s in sigs:
        f = con.execute("SELECT score, why, method FROM signal_fits WHERE signal_id=? AND product_id=? ORDER BY score "
                        "DESC LIMIT 1", (s["id"], product_id)).fetchone()
        if f:
            fitted.append((s, f))
    # need fit
    if fitted:
        s_best, f_best = max(fitted, key=lambda x: x[1]["score"])
        need = round(0.4 * f_best["score"])
        facts.append({"component": "need_fit", "signal_id": s_best["id"], "type": s_best["type"],
                      "url": s_best["source_url"], "fit": f_best["score"], "method": f_best["method"],
                      "why": f_best["why"]})
    else:
        need = 0
        grounded = con.execute("SELECT 1 FROM profile_research x JOIN person_roles r ON r.person_id=x.person_id WHERE "
                               "r.company_id=? AND x.matches_json LIKE ?", (company_id, f'%"{product_id}"%')).fetchone()
        lead = con.execute("SELECT 1 FROM bottomup_leads WHERE company_id=? AND matches_json LIKE ?",
                           (company_id, f'%"{product_id}"%')).fetchone()
        if grounded or lead:
            need = 15
            inferences.append({"component": "need_fit", "note": "no demand signal; a grounded profile/company match "
                                                                "suggests relevance (15/40)"})
        else:
            unknowns.append("no demand signal or grounded match for this accelerator")
    # timing
    if fitted:
        recs = [(recency(con, s["source_date"], s["captured_at"]), s) for s, _ in fitted]
        rec, s_fresh = max(recs, key=lambda x: x[0]["r"])
        timing = round(20 * rec["r"])
        if rec["basis"] != "source_date":  # Studio rubric: "Old or undated signals score low" -> halved
            timing = round(timing / 2)
            unknowns.append(f"signal #{s_fresh['id']} has no source date; timing halved (basis {rec['basis']})")
        facts.append({"component": "timing", "signal_id": s_fresh["id"], "days": rec["days"], "basis": rec["basis"]})
    else:
        timing = 0
    # access
    roles = con.execute("SELECT COUNT(*) FROM person_roles WHERE company_id=? AND review_status='approved'",
                        (company_id,)).fetchone()[0]
    access, parts = 0, []
    if roles:
        access += 8 + (4 if roles > 1 else 0)
        parts.append(f"{roles} approved role(s)")
        li = con.execute("SELECT COUNT(*) FROM people p JOIN person_roles r ON r.person_id=p.id WHERE r.company_id=? AND "
                         "r.review_status='approved' AND p.linkedin_url IS NOT NULL", (company_id,)).fetchone()[0]
        if li:
            access += 4
            parts.append("LinkedIn known")
        em = con.execute("SELECT status FROM email_records e JOIN person_roles r ON r.person_id=e.person_id WHERE "
                         "r.company_id=? AND e.review_status='approved' ORDER BY (status='valid') DESC LIMIT 1",
                         (company_id,)).fetchone()
        if em:
            access += 4 if em[0] == "valid" else 2
            parts.append(f"approved email ({em[0]})")
    else:
        titles = {t for s in sigs for t in json.loads(s["roles_json"] or "[]")}
        if titles:
            access += 2
            parts.append("owner titles named in signals: " + ", ".join(sorted(titles)[:4]))
            unknowns.append("no named, human-approved person yet")
    budget = max((2 if s["budget"] == "likely" else 4 if s["budget"] == "explicit" else 0) for s in sigs) if sigs else 0
    if budget:
        access += budget
        parts.append(f"budget signal ({'explicit' if budget == 4 else 'likely'})")
    access = min(20, access)
    facts.append({"component": "access", "parts": parts})
    # proof fit
    proofs = [r[0] for r in con.execute("SELECT claim FROM accelerator_proof WHERE product_id=? AND allowed=1",
                                        (product_id,))]
    if not proofs:
        proof = 0
        unknowns.append("accelerator has no registered proof point (outreach must not claim results)")
    else:
        text = " ".join((s["summary"] or "") + " " + (s["industry"] or "") for s in sigs).lower()
        matched = [p for p in proofs if _proof_terms(p) & set(re.findall(r"[a-z]{4,}", text))]
        proof = 20 if matched else 10
        from chanakya.intel.proof import NO_PROOF_TOOLS
        unproven = sorted({t for t in NO_PROOF_TOOLS if t in text})
        if unproven and not matched:
            unknowns.append(f"need names {', '.join(unproven)}; Wayam has no published proof for that platform "
                            "(registered proof covers a different platform)")
        facts.append({"component": "proof_fit", "registered": proofs, "matching_company_evidence": matched})
    total = need + timing + access + proof
    expl = {"components": {"need_fit": need, "timing": timing, "access": access, "proof_fit": proof},
            "facts": facts, "inferences": inferences, "unknowns": unknowns,
            "rule": "need 0.4 x best fit; timing 20 x recency; access from approved people/contact/budget; proof from "
                    "the proof registry (see intel/scoring.py)"}
    org = E.origin(con, "company", company_id)
    old = con.execute("SELECT id, score FROM opportunities WHERE company_id=? AND product_id=?",
                      (company_id, product_id)).fetchone()
    t = now()
    if old is None:
        oid = con.execute("INSERT INTO opportunities(company_id,product_id,need_fit,timing,access,proof_fit,score,"
                          "explanation_json,origin,computed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (company_id, product_id, need, timing, access, proof, total, jdump(expl), org, t)).lastrowid
        con.execute("INSERT INTO opportunity_history(opportunity_id,score,components_json,reason,at) VALUES(?,?,?,?,?)",
                    (oid, total, jdump(expl["components"]), "created", t))
    else:
        oid = old["id"]
        con.execute("UPDATE opportunities SET need_fit=?, timing=?, access=?, proof_fit=?, score=?, explanation_json=?, "
                    "origin=?, computed_at=? WHERE id=?", (need, timing, access, proof, total, jdump(expl), org, t, oid))
        if old["score"] != total:
            con.execute("INSERT INTO opportunity_history(opportunity_id,score,components_json,reason,at) "
                        "VALUES(?,?,?,?,?)", (oid, total, jdump(expl["components"]), f"rescored {old['score']} -> {total}", t))
    return {"opportunity_id": oid, "score": total, **expl, "origin": org}


def candidate_products(con, company_id: int) -> set[str]:
    pids = {r[0] for r in con.execute("SELECT DISTINCT f.product_id FROM signal_fits f JOIN signals s ON s.id=f.signal_id "
                                      "WHERE s.company_id=? AND s.noise=0", (company_id,))}
    for col in ("SELECT matches_json FROM bottomup_leads WHERE company_id=? AND matches_json IS NOT NULL",):
        for r in con.execute(col, (company_id,)):
            pids |= {m.get("product_id") for m in json.loads(r[0]).get("matches", []) if m.get("product_id")}
    r = con.execute("SELECT i.product_id FROM companies c JOIN icps i ON i.id=c.icp_id WHERE c.id=?", (company_id,)).fetchone()
    if r and r[0]:
        pids.add(r[0])
    return {p for p in pids if p}


def rescore_company(con, company_id: int) -> list[dict]:
    out = [score_opportunity(con, company_id, p) for p in sorted(candidate_products(con, company_id))]
    con.commit()
    return out


def rescore_all(con) -> dict:
    ids = [r[0] for r in con.execute("SELECT id FROM companies WHERE identity_status IS NOT 'merged'")]
    n = sum(len(rescore_company(con, i)) for i in ids)
    return {"companies": len(ids), "opportunities": n}
