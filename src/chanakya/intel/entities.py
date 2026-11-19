"""Entity resolution and discovery provenance: one canonical company per real organisation, whichever direction found it.

Resolution order for a company sighting (name, domain, LinkedIn company URL, key):
  1. domain (companies.domain or a domain alias)      -> deterministic identity
  2. LinkedIn company URL alias                        -> deterministic identity
  3. company key alias (e.g. Lakshya companyKey)       -> same source system identity
  4. normalised name alias                             -> strong but not certain
If nothing matches, a company is created. Without a known domain it is created as `unresolved:<slug>` with
identity_status='unresolved'; when a later sighting brings the real domain, the record is upgraded in place, or, if
another company already owns that domain, the two are MERGED (same domain = same company). Merges move every related
row (people roles, sources, signals, opportunities, emails, research, leads, reviews) and keep a snapshot in
company_merges; nothing is deleted without a trail. Name-only matches never merge two companies that both have
different real domains.

Every sighting writes a discovery_events row (entity, id, direction top_down|bottom_up, source, workflow, run), so the
origin of any company or person is `top_down`, `bottom_up` or `both`.
"""
from __future__ import annotations

import json
import re

from chanakya.storage.db import jdump, now

TOP, BOTTOM = "top_down", "bottom_up"
STOP = {"pvt", "private", "ltd", "limited", "inc", "llc", "llp", "co", "corp", "corporation", "the", "plc", "gmbh",
        "company", "group", "india"}


def norm_name(name: str | None) -> str:
    toks = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t and t not in STOP]
    return " ".join(toks)


_ACR_SKIP = STOP | {"of", "and", "for", "&", "in", "on", "at", "to"}


def acronym(name: str | None) -> str | None:
    """Initials of the significant words: "Financial Conduct Authority" -> "fca". None for one-word names."""
    toks = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t and t not in _ACR_SKIP]
    return "".join(t[0] for t in toks) if len(toks) >= 2 else None


def _stem(domain: str | None) -> str:
    d = norm_domain(domain) or ""
    return "" if d.startswith("unresolved:") else d.split(".")[0]


def _is_acr(name: str | None) -> str | None:
    t = re.sub(r"[^A-Za-z0-9]", "", name or "")
    return t.lower() if 2 <= len(t) <= 6 and t.isupper() and " " not in (name or "").strip() else None


def acronym_match(con, name: str | None, domain: str | None = None) -> int | None:
    """A company this name is the acronym (or expansion) of. Only accepted when the resolved record's own website
    domain starts with the acronym (fca.org.uk for FCA), so "FCA" never lands on, say, Fiat Chrysler by initials."""
    a = _is_acr(name)
    if a:
        for r in con.execute("SELECT id, name, domain FROM companies WHERE identity_status='resolved'"):
            if acronym(r["name"]) == a and _stem(r["domain"]) == a:
                return r["id"]
        return None
    a = acronym(name)
    if a and _stem(domain) == a:
        for r in con.execute("SELECT id, name, domain, identity_status FROM companies"):
            if _is_acr(r["name"]) == a and r["identity_status"] == "unresolved":
                return r["id"]
    return None


def duplicate_candidates(con) -> list[dict]:
    """Pairs that look like one company: same normalised name, or acronym <-> expansion. `safe` pairs (one side
    unresolved, and for acronyms the domain stem confirms it) can be merged automatically; the rest need a human."""
    rows = [dict(r) for r in con.execute("SELECT id, name, domain, identity_status FROM companies")]
    out, seen = [], set()
    for a in rows:
        for b in rows:
            if a["id"] >= b["id"] or (a["id"], b["id"]) in seen:
                continue
            why = None
            if norm_name(a["name"]) and norm_name(a["name"]) == norm_name(b["name"]):
                why = "same name"
            else:
                for x, y in ((a, b), (b, a)):
                    ac = _is_acr(x["name"])
                    if ac and acronym(y["name"]) == ac:
                        why = "acronym"
                        stem_ok = _stem(y["domain"]) == ac
                        break
            if not why:
                continue
            seen.add((a["id"], b["id"]))
            unres = [r for r in (a, b) if r["identity_status"] == "unresolved"]
            safe = len(unres) == 1 and (why == "same name" or stem_ok)
            keep = b if unres and unres[0] is a else a
            out.append({"keep": keep["id"], "drop": (b if keep is a else a)["id"], "names": [a["name"], b["name"]],
                        "domains": [a["domain"], b["domain"]], "reason": why, "safe": safe})
    return out


def auto_merge(con, by: str = "entity-resolver") -> list[dict]:
    """Merge every `safe` duplicate (source history, aliases, roles and signals move; a snapshot is kept)."""
    done = []
    for c in duplicate_candidates(con):
        if c["safe"] and con.execute("SELECT 1 FROM companies WHERE id=?", (c["drop"],)).fetchone():
            merge_companies(con, keep=c["keep"], drop=c["drop"], by=by,
                            reason=f"{c['reason']}: {c['names'][0]} = {c['names'][1]}")
            done.append(c)
    con.commit()
    return done


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "unknown"


def norm_domain(d: str | None) -> str | None:
    from chanakya.scout.agent import normalize_domain
    return normalize_domain(d) if d else None


def norm_linkedin_company(u: str | None) -> str | None:
    m = re.search(r"linkedin\.com/company/([A-Za-z0-9\-_%]+)", u or "", re.I)
    return m.group(1).lower() if m else None


def record_discovery(con, entity: str, entity_id: int, direction: str, source: str, *, workflow_id=None, run_id=None,
                     detail: dict | None = None) -> None:
    if direction not in (TOP, BOTTOM):
        raise ValueError(f"direction must be {TOP} or {BOTTOM}")
    con.execute("INSERT INTO discovery_events(entity,entity_id,direction,source,workflow_id,run_id,detail_json,at) "
                "VALUES(?,?,?,?,?,?,?,?)", (entity, entity_id, direction, source, workflow_id, run_id, jdump(detail),
                                            now()))


def origin(con, entity: str, entity_id: int) -> str | None:
    dirs = {r[0] for r in con.execute("SELECT DISTINCT direction FROM discovery_events WHERE entity=? AND entity_id=?",
                                      (entity, entity_id))}
    return "both" if dirs == {TOP, BOTTOM} else (next(iter(dirs)) if dirs else None)


SOURCE_LABELS = (("lakshya", "lakshya"), ("clay", "clay"), ("contracts-finder", "tenders"), ("tender", "tenders"),
                 ("job-board", "jobs"), ("company-website", "website"), ("manual-import", "manual"), ("linkedin-harness", "linkedin"),
                 ("wikidata", "wikidata"), ("human", "human"), ("edgar", "filings"))


def sources(con, entity: str, entity_id: int) -> list[str]:
    """Which source systems have seen this entity (lakshya, clay, tenders, website, manual, ...), from the discovery
    log. Complements `origin`, which only says the direction."""
    out: list[str] = []
    for (src,) in con.execute("SELECT DISTINCT source FROM discovery_events WHERE entity=? AND entity_id=?",
                              (entity, entity_id)):
        s = (src or "").lower()
        label = next((lab for needle, lab in SOURCE_LABELS if needle in s), "other")
        if label not in out:
            out.append(label)
    return sorted(out)


def add_alias(con, company_id: int, kind: str, raw: str | None, source: str) -> None:
    v = {"domain": norm_domain, "linkedin": norm_linkedin_company, "name": norm_name, "key": slug}[kind](raw)
    if v:
        con.execute("INSERT OR IGNORE INTO company_aliases(company_id,kind,value_norm,value_raw,source,created_at) "
                    "VALUES(?,?,?,?,?,?)", (company_id, kind, v, raw, source, now()))


def _by_alias(con, kind: str, value: str | None):
    if not value:
        return None
    r = con.execute("SELECT company_id FROM company_aliases WHERE kind=? AND value_norm=?", (kind, value)).fetchone()
    return r[0] if r else None


def find_company(con, *, name=None, domain=None, linkedin_url=None, key=None) -> tuple[int | None, str | None]:
    d = norm_domain(domain)
    if d:
        r = con.execute("SELECT id FROM companies WHERE domain=?", (d,)).fetchone()
        if r:
            return r[0], "domain"
        cid = _by_alias(con, "domain", d)
        if cid:
            return cid, "domain_alias"
    cid = _by_alias(con, "linkedin", norm_linkedin_company(linkedin_url))
    if cid:
        return cid, "linkedin"
    cid = _by_alias(con, "key", slug(key) if key else None)
    if cid:
        return cid, "key"
    n = norm_name(name)
    if n:
        cid = _by_alias(con, "name", n)
        if cid:
            return cid, "name"
        for r in con.execute("SELECT id, name FROM companies WHERE name IS NOT NULL"):
            if norm_name(r[1]) == n:
                return r[0], "name"
        cid = acronym_match(con, name, d)
        if cid:
            return cid, "acronym"
    return None, None


def resolve_company(con, *, name=None, domain=None, linkedin_url=None, key=None, direction: str, source: str,
                    workflow_id=None, run_id=None, detail: dict | None = None, record: dict | None = None) -> dict:
    """Find-or-create the canonical company, add every alias, upgrade/merge on a newly learned domain, and record the
    discovery. Returns {company_id, created, matched_by, merged_from}."""
    d = norm_domain(domain)
    cid, how = find_company(con, name=name, domain=d, linkedin_url=linkedin_url, key=key)
    created, merged = False, None
    if cid is None:
        t = now()
        rec = {k: v for k, v in (record or {}).items() if v not in (None, "", [])}
        rec.setdefault("name", name)
        cid = con.execute("INSERT INTO companies(domain,name,data_json,first_seen,last_seen,qualification_status,"
                          "qualification_source,identity_status,assessment_json) VALUES(?,?,?,?,?,?,?,?,?)",
                          (d or f"unresolved:{slug(key or name or 'unknown')}", name, json.dumps(rec), t, t,
                           "needs_manual_review", "system", "resolved" if d else "unresolved",
                           json.dumps({"qualification_status": "needs_manual_review", "qualification_source": "system",
                                       "review_reasons": ["created by entity resolution; a human must qualify it"]})
                          )).lastrowid
        created, how = True, "new"
    elif d:
        row = con.execute("SELECT domain, identity_status FROM companies WHERE id=?", (cid,)).fetchone()
        if row["identity_status"] == "unresolved" or str(row["domain"]).startswith("unresolved:"):
            owner = con.execute("SELECT id FROM companies WHERE domain=? AND id!=?", (d, cid)).fetchone()
            if owner:  # another record already owns the real domain: same company -> merge into the owner
                merge_companies(con, keep=owner[0], drop=cid, by="entity-resolver",
                                reason=f"same domain {d} learned for an unresolved record")
                merged, cid = cid, owner[0]
            else:
                con.execute("UPDATE companies SET domain=?, identity_status='resolved' WHERE id=?", (d, cid))
    if d and how in ("domain", "domain_alias"):
        # The same sighting may also match an UNRESOLVED record by key or name (e.g. "Lytx" known from a demand signal
        # before anyone knew lytx.com). It is the same company: merge it into the domain owner.
        for kind, val in (("key", slug(key) if key else None), ("name", norm_name(name))):
            other = _by_alias(con, kind, val) if val else None
            if other and other != cid:
                st = con.execute("SELECT identity_status FROM companies WHERE id=?", (other,)).fetchone()
                if st and st[0] == "unresolved":
                    merge_companies(con, keep=cid, drop=other, by="entity-resolver",
                                    reason=f"{kind} match to an unresolved record; domain {d} identifies the company")
                    merged = other
    if how == "acronym" and name and not _is_acr(name):  # the expansion becomes the display name; acronym stays an alias
        con.execute("UPDATE companies SET name=? WHERE id=?", (name, cid))
    if name and not con.execute("SELECT name FROM companies WHERE id=?", (cid,)).fetchone()[0]:
        con.execute("UPDATE companies SET name=? WHERE id=?", (name, cid))
    con.execute("UPDATE companies SET last_seen=? WHERE id=?", (now(), cid))
    for kind, raw in (("domain", d), ("linkedin", linkedin_url), ("key", key), ("name", name)):
        if raw:
            add_alias(con, cid, kind, raw, source)
    record_discovery(con, "company", cid, direction, source, workflow_id=workflow_id, run_id=run_id,
                     detail={**(detail or {}), "matched_by": how})
    return {"company_id": cid, "created": created, "matched_by": how, "merged_from": merged}


# Every table/column that points at companies(id).
_FK = [("company_sources", "company_id"), ("human_reviews", "company_id"), ("legacy_fit_archive", "company_id"),
       ("person_sources", "company_id"), ("linkedin_resolutions", "company_id"), ("email_records", "company_id"),
       ("prospects", "company_id"), ("signals", "company_id"), ("bottomup_leads", "company_id"),
       ("company_aliases", "company_id"), ("lakshya_records", "company_id")]


def merge_companies(con, *, keep: int, drop: int, by: str, reason: str) -> None:
    if keep == drop:
        return
    k = con.execute("SELECT * FROM companies WHERE id=?", (keep,)).fetchone()
    d = con.execute("SELECT * FROM companies WHERE id=?", (drop,)).fetchone()
    if k is None or d is None:
        raise ValueError("both companies must exist")
    if (not str(k["domain"]).startswith("unresolved:") and not str(d["domain"]).startswith("unresolved:")
            and k["domain"] != d["domain"] and by == "entity-resolver"):
        raise ValueError("automatic merges need the same domain; two different real domains need a human decision")
    snap = {"company": dict(d), "roles": [dict(r) for r in con.execute("SELECT * FROM person_roles WHERE company_id=?",
                                                                        (drop,))]}
    for t, col in _FK:
        con.execute(f"UPDATE OR IGNORE {t} SET {col}=? WHERE {col}=?", (keep, drop))
    # person_roles has UNIQUE(person_id, company_id, title): move what does not collide, keep the rest in the snapshot
    con.execute("UPDATE OR IGNORE person_roles SET company_id=? WHERE company_id=?", (keep, drop))
    # opportunities UNIQUE(company_id, product_id): keep the kept company's row; dropped rows are recomputed later
    con.execute("UPDATE OR IGNORE opportunities SET company_id=? WHERE company_id=?", (keep, drop))
    con.execute("UPDATE discovery_events SET entity_id=? WHERE entity='company' AND entity_id=?", (keep, drop))
    # Roles that collide (same person + same title already at the kept company) stay on the old row, which is then
    # kept as a 'merged' tombstone below instead of being deleted, so their review history is preserved.
    snap["left_roles"] = [dict(r) for r in con.execute("SELECT * FROM person_roles WHERE company_id=?", (drop,))]
    # Opportunities are derived data: colliding rows of the dropped company are removed and recomputed for the kept one.
    left_opps = [r[0] for r in con.execute("SELECT id FROM opportunities WHERE company_id=?", (drop,))]
    for oid in left_opps:
        con.execute("DELETE FROM opportunity_history WHERE opportunity_id=?", (oid,))
        con.execute("DELETE FROM opportunities WHERE id=?", (oid,))
    merged_data = {**json.loads(d["data_json"] or "{}"), **json.loads(k["data_json"] or "{}")}
    con.execute("UPDATE companies SET data_json=?, name=COALESCE(name, ?), first_seen=MIN(first_seen, ?) WHERE id=?",
                (json.dumps(merged_data), d["name"], d["first_seen"], keep))
    con.execute("INSERT INTO company_merges(kept_id,merged_id,reason,by,snapshot_json,at) VALUES(?,?,?,?,?,?)",
                (keep, drop, reason, by, jdump(snap), now()))
    if not snap["left_roles"]:
        con.execute("DELETE FROM companies WHERE id=?", (drop,))
    else:  # roles that could not move keep the old row alive, marked as merged, so nothing is lost
        con.execute("UPDATE companies SET identity_status='merged', domain=? WHERE id=?",
                    (f"merged:{drop}->{keep}", drop))
    add_alias(con, keep, "name", d["name"], f"merge from {drop}")


def company_identity(con, company_id: int) -> dict:
    c = con.execute("SELECT id, name, domain, identity_status FROM companies WHERE id=?", (company_id,)).fetchone()
    return {**dict(c), "origin": origin(con, "company", company_id),
            "aliases": [dict(a) for a in con.execute("SELECT kind, value_raw, source FROM company_aliases WHERE "
                                                     "company_id=? ORDER BY id", (company_id,))],
            "discoveries": [dict(e) for e in con.execute("SELECT direction, source, workflow_id, run_id, detail_json, at "
                                                         "FROM discovery_events WHERE entity='company' AND entity_id=? "
                                                         "ORDER BY id", (company_id,))]}


def backfill_discovery(con) -> dict:
    """Records created before discovery_events existed get their direction from their stored provenance
    (company_sources.source, person_sources.source, bottomup_leads). Idempotent: only entities without events."""
    n = {"companies": 0, "people": 0}
    for r in con.execute("SELECT DISTINCT cs.company_id, cs.source, cs.tool, cs.run_id FROM company_sources cs WHERE NOT "
                         "EXISTS (SELECT 1 FROM discovery_events d WHERE d.entity='company' AND d.entity_id="
                         "cs.company_id)").fetchall():
        d = BOTTOM if r["source"] in ("linkedin-harness", "manual-import") else TOP
        record_discovery(con, "company", r["company_id"], d, f"{r['source']}:{r['tool']} (backfill)", run_id=r["run_id"])
        n["companies"] += 1
    for r in con.execute("SELECT DISTINCT ps.person_id, ps.source, ps.run_id FROM person_sources ps WHERE NOT EXISTS "
                         "(SELECT 1 FROM discovery_events d WHERE d.entity='person' AND d.entity_id=ps.person_id)").fetchall():
        d = BOTTOM if r["source"] in ("linkedin-harness", "manual-import") else TOP
        record_discovery(con, "person", r["person_id"], d, f"{r['source']} (backfill)", run_id=r["run_id"])
        n["people"] += 1
    for r in con.execute("SELECT id, name, company_id, company_linkedin_url, company_domain, company_observed FROM "
                         "bottomup_leads WHERE company_id IS NOT NULL").fetchall():
        add_alias(con, r["company_id"], "linkedin", r["company_linkedin_url"], "bottomup lead (backfill)")
        add_alias(con, r["company_id"], "name", r["company_observed"], "bottomup lead (backfill)")
        add_alias(con, r["company_id"], "domain", r["company_domain"], "bottomup lead (backfill)")
    for r in con.execute("SELECT id, name, domain FROM companies WHERE domain NOT LIKE '%:%'").fetchall():
        add_alias(con, r["id"], "domain", r["domain"], "companies (backfill)")
        add_alias(con, r["id"], "name", r["name"], "companies (backfill)")
    con.commit()
    return n


def correct_domain(con, company_id: int, new_domain: str, *, by: str, reason: str) -> dict:
    """A human fixes a WRONG domain on a resolved account (e.g. a Wikidata suggestion that named a different company).

    Evidence fetched from the old domain's website is rejected with the reason (kept for audit, never deleted), the
    old domain alias is removed, and the account takes the new domain, merging into the record that already owns it.
    The caller rescores. Returns {"company_id": kept, "old_domain", "new_domain", "rejected_signals": [...]}.
    """
    from urllib.parse import urlparse

    from chanakya.intel.signals import review_signal
    if not (by or "").strip() or not (reason or "").strip():
        raise ValueError("reviewer and reason are required")
    c = con.execute("SELECT name, domain FROM companies WHERE id=?", (company_id,)).fetchone()
    if c is None:
        raise ValueError("unknown account")
    new = norm_domain(new_domain)
    if not new:
        raise ValueError("a valid domain is required")
    old = c["domain"] if c["domain"] and ":" not in c["domain"] else None
    if new == old:
        raise ValueError("that is already the account's domain")
    rejected = []
    if old:
        for s in con.execute("SELECT id, source_url FROM signals WHERE company_id=? AND provenance='company-website' "
                             "AND verification_status IS NOT 'rejected'", (company_id,)).fetchall():
            host = (urlparse(s["source_url"] or "").hostname or "").lower()
            if host == old or host.endswith("." + old):
                review_signal(con, s["id"], "rejected", by=by,
                              reason=f"domain corrected {old} -> {new}: evidence came from the wrong company's site")
                rejected.append(s["id"])
        con.execute("DELETE FROM company_aliases WHERE company_id=? AND kind='domain' AND value_norm=?",
                    (company_id, old))
    owner = con.execute("SELECT id FROM companies WHERE domain=? AND id!=?", (new, company_id)).fetchone()
    if owner:
        con.execute("UPDATE companies SET domain=? WHERE id=?", (f"unresolved:corrected-{company_id}", company_id))
        merge_companies(con, keep=owner[0], drop=company_id, by=by, reason=f"human domain correction: {reason}")
        kept = owner[0]
    else:
        con.execute("UPDATE companies SET domain=?, identity_status='resolved' WHERE id=?", (new, company_id))
        add_alias(con, company_id, "domain", new, f"human-correction:{by}")
        kept = company_id
    record_discovery(con, "company", kept, TOP, f"human-domain-correction:{by}",
                     detail={"reason": reason, "old_domain": old, "new_domain": new, "rejected_signals": rejected})
    con.commit()
    return {"company_id": kept, "old_domain": old, "new_domain": new, "rejected_signals": rejected}
