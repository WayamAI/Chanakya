"""UK public-procurement signals from Contracts Finder (official open API, no login, no key; robots.txt absent).

API: POST https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json
The API's keyword search is loose (it returns unrelated notices), so every notice is kept only if the exact keyword
phrase appears in its title or description. Each notice becomes a signal with its notice URL, the published date as
the source date, and the buyer as a top-down company (identity unresolved until a domain is found).

Labelling (never presents old notices as live opportunities):
  Open                -> type 'tender'  ("OPEN tender, deadline ...")
  Awarded / Closed    -> type 'news'    ("AWARDED to <supplier>, contract ends ..."): evidence of the platform in use
                                          and a renewal date, not an open buying opportunity
  Withdrawn / other   -> skipped
"""
from __future__ import annotations

import datetime as dt
import html
import re
from typing import Callable

API = "https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json"
NOTICE = "https://www.contractsfinder.service.gov.uk/Notice/{id}"
PROVENANCE = "contracts-finder-api"
UA = "Chanakya-research/1.0 (Wayam AI; public procurement notices)"

# Product -> exact phrases that name the need. Override per run with params.tender_keywords.
DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "PRD-SETU": ["Power BI", "Tableau", "OBIEE", "Oracle Analytics", "Qlik", "Cognos", "BusinessObjects",
                 "MicroStrategy", "business intelligence"],
    "PRD-VEDHA": ["penetration testing", "IT health check"],
    "PRD-ARJUNA": ["video analytics", "CCTV analytics"],
    "PRD-VAKYAM": ["contact centre", "call recording"],
    "PRD-BODHAM": ["self-service analytics", "natural language query"],
    "PRD-PARIKSHA": ["test automation", "software testing"],
}


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def _date(s: str | None) -> str | None:
    return s[:10] if s and re.match(r"\d{4}-\d{2}-\d{2}", s) else None


def search(keyword: str, *, since: dt.date, post: Callable | None = None, size: int = 100) -> list[dict]:
    """Notices published since `since` whose title or description contains the exact phrase."""
    if post is None:
        import httpx

        def post(url, body):
            r = httpx.post(url, json=body, headers={"User-Agent": UA}, timeout=60)
            r.raise_for_status()
            return r.json()
    d = post(API, {"searchCriteria": {"keyword": keyword, "publishedFrom": f"{since.isoformat()}T00:00:00"},
                   "size": size})
    k = keyword.lower()
    out = []
    for n in d.get("noticeList") or []:
        it = n["item"]
        if k in f"{_clean(it.get('title'))} {_clean(it.get('description'))}".lower():
            out.append(it)
    return out


def to_signal(it: dict, product_id: str, keyword: str) -> dict | None:
    st = it.get("noticeStatus") or ""
    title, buyer = _clean(it.get("title")), _clean(it.get("organisationName"))
    if not buyer or st not in ("Open", "Awarded", "Closed"):
        return None
    val = it.get("awardedValue") or it.get("valueHigh") or it.get("valueLow")
    money = f", value £{int(val):,}" if val else ""
    if st == "Open":
        typ = "tender"
        summary = f"OPEN tender: {title} ({buyer}){money}; deadline {_date(it.get('deadlineDate')) or 'not stated'}"
    else:
        typ = "news"
        sup = _clean(it.get("awardedSupplier")) or "supplier not stated"
        summary = (f"{st.upper()} contract (not an open opportunity): {title} ({buyer}), awarded to {sup}{money}; "
                   f"contract ends {_date(it.get('end')) or 'not stated'}")
    return {"type": typ, "summary": summary[:600], "external_id": f"cf:{it['id']}",
            "source_url": NOTICE.format(id=it["id"]), "source_date": _date(it.get("publishedDate")),
            "source_date_basis": "Contracts Finder publishedDate", "company": {"name": buyer},
            "region": "UK", "budget": money.lstrip(", ") or None,
            "fits": [{"product_id": product_id, "score": 50 if typ == "tender" else 35,
                      "why": f"notice names '{keyword}' ({st.lower()})"}],
            "status": st, "end": _date(it.get("end"))}


def sweep(con, *, keywords: dict[str, list[str]] | None = None, since: dt.date | None = None, post=None,
          workflow_id=None, limit_per_keyword: int = 20) -> dict:
    """Fetch, filter and store. Returns counts plus every source URL and any error (errors are reported, not hidden)."""
    from chanakya.intel import entities as E
    from chanakya.intel.signals import store_signal
    since = since or (dt.date.today() - dt.timedelta(days=120))
    kws = keywords or DEFAULT_KEYWORDS
    products = {r[0] for r in con.execute("SELECT id FROM products")}
    res = {"source": API, "since": since.isoformat(), "queries": [], "created": 0, "updated": 0, "skipped": 0,
           "errors": [], "signals": []}
    for pid, words in kws.items():
        if pid not in products:
            continue
        for w in words:
            try:
                items = search(w, since=since, post=post)
            except Exception as e:  # noqa: BLE001 - network/API failure is reported as such
                res["errors"].append(f"{w}: {type(e).__name__}: {str(e)[:150]}")
                continue
            res["queries"].append({"product_id": pid, "keyword": w, "matched": len(items)})
            for it in items[:limit_per_keyword]:
                s = to_signal(it, pid, w)
                if s is None:
                    res["skipped"] += 1
                    continue
                meta = {k: s.pop(k) for k in ("status", "end")}
                sid, created = store_signal(con, direction=E.TOP, provenance=PROVENANCE, fit_method="rule (tender keyword)",
                                            workflow_id=workflow_id, **s)
                res["created" if created else "updated"] += 1
                res["signals"].append({"signal_id": sid, "created": created, "url": s["source_url"],
                                       "buyer": s["company"]["name"], "status": meta["status"], "type": s["type"]})
    con.commit()
    return res
