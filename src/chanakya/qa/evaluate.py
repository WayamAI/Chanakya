"""Output-quality evaluation of the shared intelligence layer (the harness's "is it useful and correct" half).

bench/run_bench.py measures whether a MODEL calls tools correctly. This module measures whether the PLATFORM's
persisted outputs are trustworthy, deterministically and without an LLM judge:

  evidence      every signal carries source URL, source date (or a stated basis), capture time, summary, company
  provenance    every record says where it came from; historical seed, live and test-fixture records are counted apart
  verification  automatic checks never claim "verified"; distribution of verification states
  dedupe        no two companies share a domain / normalised name; no two people share an identity key
  traceability  every opportunity explanation cites signals that exist and belong to that company
  consistency   components are in range, sum to the score, and rescoring an in-memory copy gives the same result
  claims        no draft makes a forbidden proof claim; no draft is addressed to an email a human has not approved
  handoffs      workflow stages: done / waiting / failed, with the error text of failures
  convergence   accounts and people discovered by top-down, bottom-up or both

Read-only: the database passed in is never written (consistency rescoring runs on a memory copy).
Run: python scripts/chanakya.py evaluate   (or python -m chanakya.qa.evaluate PATH)
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter

SEED_PREFIXES = ("lakshya-handover",)
FIXTURE_MARKERS = ("simulated", "fixture", "e2e", "test")


def origin_class(provenance: str | None) -> str:
    p = (provenance or "").lower()
    if not p:
        return "unknown"
    if p.startswith(SEED_PREFIXES):
        return "historical_seed"
    if any(m in p for m in FIXTURE_MARKERS):
        return "test_fixture"
    return "live"


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _check(name: str, ok: bool, detail) -> dict:
    return {"check": name, "pass": bool(ok), "detail": detail}


def evaluate(con: sqlite3.Connection, *, rescore: bool = True) -> dict:
    con.row_factory = sqlite3.Row
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    checks: list[dict] = []

    # ---- evidence completeness + provenance
    sigs = q("SELECT * FROM signals")
    n = len(sigs)
    fields = {
        "source_url": sum(1 for s in sigs if s["source_url"]),
        "source_date_or_basis": sum(1 for s in sigs if s["source_date"] or s["source_date_basis"]),
        "source_date": sum(1 for s in sigs if s["source_date"]),
        "captured_at": sum(1 for s in sigs if s["captured_at"]),
        "summary": sum(1 for s in sigs if (s["summary"] or "").strip()),
        "company_or_market": sum(1 for s in sigs if s["company_id"] or s["company_name"]),
        "provenance": sum(1 for s in sigs if s["provenance"]),
    }
    evidence = {k: {"n": v, "pct": _pct(v, n)} for k, v in fields.items()}
    by_origin = Counter(origin_class(s["provenance"]) for s in sigs)
    checks.append(_check("every signal has a source URL", fields["source_url"] == n, evidence["source_url"]))
    checks.append(_check("every signal states its source date or why it has none",
                         fields["source_date_or_basis"] == n, evidence["source_date_or_basis"]))
    checks.append(_check("every signal has provenance", fields["provenance"] == n, evidence["provenance"]))

    # ---- verification honesty
    ver = Counter(s["verification_status"] or "unverified" for s in sigs)
    def human_verified(s) -> bool:
        reviews = json.loads(s["verification_json"] or "{}").get("human_reviews", [])
        return any(r.get("status") == "verified" and r.get("by") for r in reviews)
    auto_verified = [s["id"] for s in sigs if s["verification_status"] == "verified" and not human_verified(s)]
    checks.append(_check("no signal marked verified without a human reviewer", not auto_verified,
                         {"offending_signal_ids": auto_verified}))

    # ---- dedupe
    dom_dupes = q("SELECT domain, COUNT(*) n FROM companies WHERE domain IS NOT NULL AND domain NOT LIKE 'unresolved:%' "
                  "GROUP BY domain HAVING n>1")
    from chanakya.intel.entities import norm_name
    names = Counter(norm_name(r["name"]) for r in q("SELECT name FROM companies") if r["name"])
    name_dupes = {k: v for k, v in names.items() if v > 1 and k}
    ppl_dupes = q("SELECT dedupe_key, COUNT(*) n FROM people GROUP BY dedupe_key HAVING n>1")
    checks.append(_check("no duplicate company domains", not dom_dupes, [dict(r) for r in dom_dupes]))
    checks.append(_check("no duplicate normalised company names", not name_dupes, name_dupes))
    checks.append(_check("no duplicate people identities", not ppl_dupes, [dict(r) for r in ppl_dupes]))

    # ---- traceability + consistency of opportunities
    opps = q("SELECT * FROM opportunities")
    sig_company = {s["id"]: s["company_id"] for s in sigs}
    untraceable, inconsistent = [], []
    for o in opps:
        comps = [o["need_fit"], o["timing"], o["access"], o["proof_fit"]]
        bounds = [40, 20, 20, 20]
        if sum(comps) != o["score"] or any(c is None or c < 0 or c > b for c, b in zip(comps, bounds)):
            inconsistent.append(o["id"])
        expl = json.loads(o["explanation_json"] or "{}")
        cited = {int(x) for x in re.findall(r"signal #(\d+)", json.dumps(expl))}
        if not expl.get("facts") or any(sig_company.get(c) != o["company_id"] for c in cited):
            untraceable.append(o["id"])
    checks.append(_check("opportunity components in range and sum to the score", not inconsistent,
                         {"offending": inconsistent, "of": len(opps)}))
    checks.append(_check("every opportunity explanation has facts citing its own company's signals", not untraceable,
                         {"offending": untraceable, "of": len(opps)}))

    drift = []
    if rescore and opps:
        from chanakya.intel import scoring as SC
        mem = sqlite3.connect(":memory:")
        # iterdump, not backup(): backup() blocks forever if `con` holds an open write transaction.
        mem.executescript("\n".join(con.iterdump()))
        mem.row_factory = sqlite3.Row
        for o in opps:
            r = SC.score_opportunity(mem, o["company_id"], o["product_id"])
            again = SC.score_opportunity(mem, o["company_id"], o["product_id"])
            if r["score"] != again["score"]:
                drift.append({"opportunity": o["id"], "first": r["score"], "second": again["score"]})
        mem.close()
    checks.append(_check("rescoring is deterministic", not drift, {"drift": drift, "rescored": rescore}))

    # ---- claims and contact safety in drafts
    from chanakya.intel.proof import claim_violations
    bad_claims, unapproved_to = [], []
    approved = {(r["email"] or "").lower() for r in q("SELECT email FROM email_records WHERE review_status='approved'")}
    drafts = q("SELECT id, to_address, subject, body, status FROM outreach_drafts")
    for d in drafts:
        if claim_violations(f"{d['subject']} {d['body']}", "PRD-SETU"):
            bad_claims.append(d["id"])
        if d["to_address"] and d["to_address"].lower() not in approved:
            unapproved_to.append(d["id"])
    sent = [d["id"] for d in drafts if (d["status"] or "") == "sent"]
    checks.append(_check("no draft makes a forbidden proof claim", not bad_claims, {"offending": bad_claims}))
    checks.append(_check("no draft addressed to an email a human has not approved", not unapproved_to,
                         {"offending": unapproved_to}))
    checks.append(_check("no draft was sent by the system", not sent, {"offending": sent}))

    # ---- entity identity: company-website evidence must come from the account's own (current) domain
    from urllib.parse import urlparse
    doms = {r["id"]: (r["domain"] or "") for r in q("SELECT id, domain FROM companies")}
    foreign = []
    for s in sigs:
        if s["provenance"] != "company-website" or s["verification_status"] == "rejected":
            continue
        host = (urlparse(s["source_url"] or "").hostname or "").lower().removeprefix("www.")
        dom = doms.get(s["company_id"], "").lower().removeprefix("www.")
        if not dom or not (host == dom or host.endswith("." + dom)):
            foreign.append({"signal": s["id"], "host": host, "account_domain": dom})
    checks.append(_check("company-website evidence comes from the account's own domain", not foreign,
                         {"offending": foreign}))

    # ---- rejected evidence is never cited by an opportunity
    rejected = {s["id"] for s in sigs if s["verification_status"] == "rejected"}
    cites_rejected = []
    for o in opps:
        cited = {int(x) for x in re.findall(r"signal #(\d+)", o["explanation_json"] or "")}
        if cited & rejected:
            cites_rejected.append({"opportunity": o["id"], "rejected_signals": sorted(cited & rejected)})
    checks.append(_check("no opportunity cites a human-rejected signal", not cites_rejected, cites_rejected))

    # ---- live evidence keeps publication date and retrieval date apart
    live = [s for s in sigs if origin_class(s["provenance"]) == "live"]
    no_retrieval = [s["id"] for s in live if not s["captured_at"]]
    checks.append(_check("every live signal has a retrieval time (captured_at) separate from its source date",
                         not no_retrieval, {"offending": no_retrieval, "live_signals": len(live),
                                            "with_source_date": sum(1 for s in live if s["source_date"])}))

    # ---- Outlook copies are drafts, and the content catalogue is valid
    cols = {r[1] for r in con.execute("PRAGMA table_info(outreach_drafts)")}
    in_outlook = q("SELECT id, status FROM outreach_drafts WHERE outlook_message_id IS NOT NULL") \
        if "outlook_message_id" in cols else []
    checks.append(_check("drafts copied to Outlook were reviewed by a human and not sent",
                         all(r["status"] in ("edited", "ready") for r in in_outlook),
                         {"in_outlook": len(in_outlook), "offending": [r["id"] for r in in_outlook
                                                                       if r["status"] not in ("edited", "ready")]}))
    from chanakya.intel import content as CT
    try:
        CT.load()
        content_issue = None
    except CT.ContentError as e:
        content_issue = str(e)
    checks.append(_check("Wayam content catalogue is valid (voice rules, no unregistered proof)", content_issue is None,
                         {"path": str(CT.path()), "issue": content_issue}))

    # ---- handoffs
    stages =Counter(r["status"] for r in q("SELECT status FROM workflow_stages"))
    failures = [dict(r) for r in q("SELECT workflow_id, stage, attempts, error FROM workflow_stages WHERE status='failed'")]
    checks.append(_check("no failed workflow stage", not failures, failures))

    # ---- convergence
    conv = {}
    for entity in ("company", "person"):
        rows = q("SELECT entity_id, GROUP_CONCAT(DISTINCT direction) d FROM discovery_events WHERE entity=? "
                 "GROUP BY entity_id", entity)
        c = Counter("both" if ("top_down" in r["d"] and "bottom_up" in r["d"]) else r["d"] for r in rows)
        conv[entity] = dict(c)

    passed = sum(1 for c in checks if c["pass"])
    return {
        "summary": {"checks": len(checks), "passed": passed, "failed": len(checks) - passed},
        "counts": {"signals": n, "signals_by_origin": dict(by_origin), "companies": len(names),
                   "people": q("SELECT COUNT(*) n FROM people")[0]["n"], "opportunities": len(opps),
                   "drafts": len(drafts)},
        "evidence_completeness": evidence,
        "verification": dict(ver),
        "workflow_stages": dict(stages),
        "convergence": conv,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else "data/chanakya.db"
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    r = evaluate(con)
    print(json.dumps(r, indent=2, default=str))
    return 0 if r["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
