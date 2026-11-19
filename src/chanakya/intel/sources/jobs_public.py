"""Hiring signals from free public job-board APIs (no login, no key, no scraping): companies that are hiring for a
need a Wayam accelerator covers. This is a DISCOVERY source: the employer becomes a top-down company (identity
unresolved until a domain is confirmed) with a dated hiring signal and the posting URL.

Feeds (one HTTP request each per sweep; phrases are matched locally, so the feeds are never queried per keyword):
  arbeitnow  https://www.arbeitnow.com/api/job-board-api      (free public API; asks for a link back: the posting URL
                                                               is stored and shown as the source)
  remotive   https://remotive.com/api/remote-jobs             (free public API; jobs must not be republished to job
                                                               boards: Chanakya stores a one-line summary and the
                                                               link for internal research only)

A posting is kept only if an exact need phrase appears in its title or description. A phrase in the title scores
higher than one in the description. Scores stay below the 60 "strong fit" line: a hiring post is a lead to check,
not proof of a buying decision.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from typing import Callable

FEEDS = {"arbeitnow": "https://www.arbeitnow.com/api/job-board-api?page={page}",
         "remotive": "https://remotive.com/api/remote-jobs"}
UA = "Chanakya-research/1.0 (Wayam AI; hiring signals from public job APIs)"

# Product -> exact phrases that name the NEED in a job posting (not just a tool name: "Power BI" alone would match
# every analyst role). Override per run with params.job_keywords.
DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "PRD-SETU": ["Tableau to Power BI", "Power BI migration", "migration to Power BI", "migrate to Power BI",
                 "BI migration", "dashboard migration", "Cognos", "BusinessObjects", "Business Objects",
                 "MicroStrategy", "OBIEE", "Qlik to Power BI", "report migration"],
    "PRD-VEDHA": ["penetration testing", "penetration tester", "red team"],
    "PRD-PARIKSHA": ["test automation framework", "QA automation"],
    "PRD-BODHAM": ["text-to-SQL", "natural language to SQL", "semantic layer"],
    "PRD-SUTRADHAR": ["AIOps", "IT operations automation", "incident automation"],
    "PRD-VAKYAM": ["conversation intelligence", "speech analytics"],
    "PRD-ARJUNA": ["video analytics"],
}


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _get(url: str):
    import httpx
    last: Exception | None = None
    for wait in (0, 2, 5):
        if wait:
            import time
            time.sleep(wait)
        try:
            r = httpx.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=60,
                          follow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r.json()
        except httpx.TransportError as e:
            last = e
    raise last or RuntimeError("unreachable")


def fetch(feed: str, *, get: Callable | None = None, pages: int = 2) -> list[dict]:
    """Normalised postings: {feed, id, title, company, location, url, date, text}."""
    get = get or _get
    out: list[dict] = []
    if feed == "arbeitnow":
        for page in range(1, pages + 1):
            d = get(FEEDS[feed].format(page=page))
            for j in d.get("data") or []:
                ts = j.get("created_at")
                date = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date().isoformat() if ts else None
                out.append({"feed": feed, "id": j.get("slug"), "title": _clean(j.get("title")),
                            "company": _clean(j.get("company_name")), "location": _clean(j.get("location")),
                            "url": j.get("url"), "date": date, "text": _clean(j.get("description"))})
            if not (d.get("links") or {}).get("next"):
                break
    elif feed == "remotive":
        d = get(FEEDS[feed])
        for j in d.get("jobs") or []:
            out.append({"feed": feed, "id": str(j.get("id")), "title": _clean(j.get("title")),
                        "company": _clean(j.get("company_name")),
                        "location": _clean(j.get("candidate_required_location")), "url": j.get("url"),
                        "date": (j.get("publication_date") or "")[:10] or None,
                        "text": _clean(j.get("description"))})
    else:
        raise ValueError(f"unknown job feed {feed!r}")
    return [j for j in out if j["id"] and j["company"] and j["title"] and j["url"]]


def match(job: dict, keywords: dict[str, list[str]]) -> list[dict]:
    """Every product whose need phrase appears in the posting, best phrase first (title beats description)."""
    title, text = job["title"].lower(), job["text"].lower()
    hits = []
    for pid, words in keywords.items():
        best = None
        for w in words:
            pat = r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])"
            if re.search(pat, title):
                best = {"product_id": pid, "phrase": w, "where": "title", "score": 50}
                break
            if best is None and re.search(pat, text):
                best = {"product_id": pid, "phrase": w, "where": "description", "score": 38}
        if best:
            hits.append(best)
    return sorted(hits, key=lambda h: -h["score"])


def sweep(con, *, keywords: dict[str, list[str]] | None = None, feeds: tuple[str, ...] = ("arbeitnow", "remotive"),
          since: dt.date | None = None, get: Callable | None = None, workflow_id=None, limit: int = 40) -> dict:
    """Fetch each feed once, keep postings that name a need, store them as dated hiring signals. Idempotent by
    posting id. Feed errors are reported per feed, never hidden; one failing feed does not stop the other."""
    from chanakya.intel import entities as E
    from chanakya.intel.signals import store_signal
    since = since or (dt.date.today() - dt.timedelta(days=60))
    products = {r[0] for r in con.execute("SELECT id FROM products")}
    kws = {p: w for p, w in (keywords or DEFAULT_KEYWORDS).items() if p in products}
    res = {"feeds": [], "since": since.isoformat(), "created": 0, "updated": 0, "errors": [], "signals": []}
    for feed in feeds:
        try:
            jobs = fetch(feed, get=get)
        except Exception as e:  # noqa: BLE001 - reported as a feed error
            res["errors"].append(f"{feed}: {type(e).__name__}: {str(e)[:150]}")
            res["feeds"].append({"feed": feed, "fetched": 0, "matched": 0, "error": True})
            continue
        matched = 0
        for j in jobs:
            if j["date"] and j["date"] < since.isoformat():
                continue
            hits = match(j, kws)
            if not hits or matched >= limit:
                continue
            matched += 1
            top = hits[0]
            summary = (f"HIRING: {j['title']} at {j['company']}" + (f" ({j['location']})" if j["location"] else "")
                       + f"; the posting names '{top['phrase']}' in its {top['where']}")
            sid, created = store_signal(
                con, type="hiring", summary=summary[:600], direction=E.TOP, provenance=f"job-board:{feed}",
                company={"name": j["company"]}, external_id=f"job:{feed}:{j['id']}", source_url=j["url"],
                source_date=j["date"], source_date_basis=f"{feed} posting date" if j["date"] else "not stated",
                roles=[j["title"]], fit_method="rule (hiring phrase, needs review)", workflow_id=workflow_id,
                fits=[{"product_id": h["product_id"], "score": h["score"],
                       "why": f"posting names '{h['phrase']}' in its {h['where']}"} for h in hits])
            res["created" if created else "updated"] += 1
            res["signals"].append({"signal_id": sid, "created": created, "url": j["url"], "company": j["company"],
                                   "phrase": top["phrase"], "product_id": top["product_id"], "date": j["date"]})
        res["feeds"].append({"feed": feed, "fetched": len(jobs), "matched": matched})
    con.commit()
    return res
