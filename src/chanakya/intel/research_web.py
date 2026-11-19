"""Top-down evidence research on known companies from their own public websites, plus the intel refresh.

company_web_signals: fetches a few public pages of a company's own domain (robots.txt respected by the fetcher), and
stores only structured, dated evidence the site declares (schema.org JobPosting -> hiring, Article/NewsArticle/
BlogPosting/PressRelease -> news). Each signal keeps the page URL, the source date from the page, the capture date,
and a conservative rule-based accelerator fit (capped at 55, labelled 'needs review'). This is how evidence found
top-down attaches to an account that bottom-up discovered (and vice versa): the signal resolves to the SAME company.

refresh: verifies unverified signal sources (reachability + company mention + published date), recomputes gap themes,
and rescores the opportunities of the affected companies.
"""
from __future__ import annotations

from chanakya.intel import entities as E
from chanakya.intel import gaps as G
from chanakya.intel import scoring as SC
from chanakya.intel import signals as SG

PAGES = ("", "/careers", "/jobs", "/news", "/press", "/blog")
MAX_FEEDS = 2
MAX_ARTICLES = 5


def company_web_signals(con, company_id: int, fetcher, *, workflow_id=None, max_pages: int = 6) -> dict:
    c = con.execute("SELECT id, name, domain, identity_status FROM companies WHERE id=?", (company_id,)).fetchone()
    if c is None or c["identity_status"] != "resolved" or ":" in str(c["domain"]):
        return {"company_id": company_id, "skipped": "no resolved domain yet"}
    pages, found, seen_urls = [], [], set()
    feeds: list[str] = []
    articles: list[str] = []

    def get(url):
        p = fetcher(url)
        pages.append({"url": p.url, "status": p.status, "robots_allowed": p.robots_allowed, "error": p.error})
        return p if p.text and (p.status or 0) < 400 else None

    batches = []
    for path in PAGES[:max_pages]:
        p = get(f"https://{c['domain']}{path or '/'}")
        if p is None:
            continue
        batches.append((p, SG.extract_page_signals(p.text, p.url)))
        # Listing pages rarely declare dated items themselves: follow their feeds and article links (same site,
        # bounded: at most MAX_FEEDS feeds and MAX_ARTICLES article pages per company).
        feeds += [u for u in SG.find_feed_links(p.text, p.url) if u not in feeds]
        if path in ("/news", "/press", "/blog"):
            articles += [u for u in SG.find_article_links(p.text, p.url, c["domain"]) if u not in articles]
    for u in feeds[:MAX_FEEDS]:
        p = get(u)
        if p is not None:
            batches.append((p, SG.extract_feed_items(p.text, p.url, c["domain"])))
    for u in articles[:MAX_ARTICLES]:
        p = get(u)
        if p is not None:
            batches.append((p, SG.extract_page_signals(p.text, p.url)))
    for p, sigs in batches:
        for sig in sigs:
            if sig["source_url"] in seen_urls:
                continue
            seen_urls.add(sig["source_url"])
            text = sig["summary"] + " " + str(sig.get("evidence", {}).get("description", ""))
            sid, new = SG.store_signal(
                con, type=sig["type"], summary=sig["summary"], direction=E.TOP, provenance="company-website",
                company={"name": c["name"], "domain": c["domain"]}, source_url=sig["source_url"],
                source_date=sig.get("source_date"), source_date_basis="date declared on the source page"
                if sig.get("source_date") else "not stated on the page", fits=SG.rule_fit(con, text),
                fit_method="rule (keyword overlap, needs review)", workflow_id=workflow_id,
                verification_status="source_reachable")
            con.execute("UPDATE signals SET verification_json=? WHERE id=? AND verification_json IS NULL",
                        (SG.jdump({"evidence": sig.get("evidence"), "found_on": p.url}), sid))
            found.append({"signal_id": sid, "new": new, "type": sig["type"], "summary": sig["summary"]})
    con.commit()
    return {"company_id": company_id, "domain": c["domain"], "pages": pages, "signals": found}


def refresh(con, company_ids: list[int] | None, fetcher=None, *, verify_limit: int = 40) -> dict:
    verified = []
    if fetcher is not None:
        q = "SELECT id FROM signals WHERE source_url IS NOT NULL AND verification_status='unverified'"
        args: list = []
        if company_ids:
            q += f" AND company_id IN ({','.join('?' * len(company_ids))})"
            args = list(company_ids)
        for (sid,) in con.execute(q + " ORDER BY id LIMIT ?", (*args, verify_limit)).fetchall():
            verified.append({"signal_id": sid, **{k: v for k, v in SG.verify_signal(con, sid, fetcher).items()
                                                  if k in ("status", "http_status", "published_date_found")}})
    themes = G.recompute(con)
    ids = company_ids or [r[0] for r in con.execute("SELECT id FROM companies WHERE identity_status IS NOT 'merged'")]
    opps = [o for cid in ids for o in SC.rescore_company(con, cid)]
    return {"verified": verified, "gap_themes": themes, "opportunities": len(opps)}
