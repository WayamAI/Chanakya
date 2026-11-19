"""Leadership-page agent: named executives from a company's OWN public website (no LinkedIn, no login, robots respected
by the fetcher). Replaces manual import for the common case where an organisation publishes its board or leadership.

Steps per company (resolved domain only):
  1. fetch the homepage, collect same-site links whose URL or text names leadership/board/team/management pages,
     plus a few conventional paths; fetch at most `max_pages`;
  2. a model (route 'extraction', Haiku by default) lists {name, title} pairs found in the page text;
  3. a pair is kept only if BOTH the name and the title occur verbatim in that page's text (no invented people);
  4. kept pairs become people + roles with review_status 'needs_manual_review' (a human still approves each role),
     source 'company-website' with the page URL.
Without a model, nothing is extracted (reported as such; no rule-based guessing of names).
"""
from __future__ import annotations

import html as H
import re
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field

from chanakya.storage.db import now

SOURCE = "company-website"
WORDS = ("leadership", "board", "our-board", "management", "executive", "our-team", "team", "governance",
         "who-we-are", "about", "our-people", "directors", "trustees", "what-we-do", "our-organisation",
         "our-organization", "corporate-information", "committee", "leaders")
PATHS = ("/about", "/about-us", "/leadership", "/our-leadership", "/team", "/our-team", "/board", "/management")

SYSTEM = ("You extract people who hold named roles at the organisation from one web page of its own site. "
          "List only people explicitly named on the page with their role title AS WRITTEN on the page. "
          "Do not infer, translate, or add people who are not on the page. Exclude authors, testimonials, customers, "
          "suppliers, partners and anyone quoted from another organisation. For each person give the organisation the "
          "page says they work for. If none, return an empty list.")


class _Person(BaseModel):
    name: str
    title: str
    organisation: str = Field("", description="the organisation this person works for, as the page states it")


class _People(BaseModel):
    people: list[_Person] = Field(default_factory=list)


def page_text(html: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", H.unescape(s)).strip()


def leadership_links(html: str, base: str, domain: str, limit: int = 6) -> list[str]:
    out = []
    for href, text in re.findall(r'(?is)<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', html or ""):
        u = urljoin(base, H.unescape(href))
        host = urlparse(u).netloc.lower()
        if not (host == domain or host.endswith("." + domain)) or not u.startswith("http"):
            continue
        hay = (urlparse(u).path + " " + page_text(text)).lower().replace(" ", "-")
        if any(w in hay for w in WORDS) and u.split("?")[0] not in out:
            out.append(u.split("?")[0])
    # Rank BEFORE cutting: board and executive links often sit in the footer, after a dozen generic "about" links.
    return sorted(out, key=_rank)[:limit]


STRONG = ("board", "leadership", "executive", "executives", "directors", "trustees", "team", "management-team",
          "senior-management", "governance", "committee", "exco", "leaders")
HUBS = ("about", "who-we-are", "what-we-do", "our-organisation", "our-organization", "corporate-information")


def _key(u: str | None) -> str:
    x = urlparse(u or "")
    return (x.netloc.lower().removeprefix("www.") + x.path.rstrip("/").lower()) or "/"


HUB_ORDER = {"who-we-are": 0, "our-people": 0, "our-organisation": 1, "our-organization": 1,
             "corporate-information": 1, "about": 2, "what-we-do": 3}


def _rank(u: str) -> tuple:
    """Sort key, lowest first: (0, depth) a leadership/board page; (1, hub order, depth) a hub that usually links to
    one, "who we are" before "about" before "what we do", shallow before deep; (2, depth) anything else. A long slug
    (a news article whose title happens to contain 'management') is never ranked as a leadership page."""
    segs = [x for x in urlparse(u).path.lower().split("/") if x]
    short = [x for x in segs if x.count("-") <= 3]
    if any(any(w == x or x.startswith(w + "-") or x.endswith("-" + w) or w in x.split("-") for w in STRONG)
           for x in short):
        return (0, 0, len(segs))
    hubs = [o for h, o in HUB_ORDER.items() if short and h in short[-1]]  # the LAST segment decides what a page is
    if hubs:
        return (1, min(hubs), len(segs))
    return (1, 4, len(segs)) if any(any(h in x for h in HUBS) for x in short) else (2, 0, len(segs))


_STOP = {"the", "and", "of", "for", "ltd", "limited", "plc", "inc", "group", "nhs", "trust", "council", "services"}


def _same_org(org: str, title: str, company: str, domain: str) -> bool:
    """False when the page says the person works somewhere else (e.g. a supplier's CEO quoted in a press release)."""
    mine = set(re.findall(r"[a-z0-9]{3,}", f"{company} {domain.split('.')[0]}".lower())) - _STOP
    acronym = "".join(w[0] for w in re.findall(r"[A-Za-z]+", company) if w.lower() not in {"of", "and", "the"}).lower()
    mine |= {acronym}

    def other(text):
        toks = set(re.findall(r"[a-z0-9]{3,}", (text or "").lower())) - _STOP
        return bool(toks) and not (toks & mine)
    if org.strip() and other(org):
        return False
    m = re.search(r"\b(?:at|of)\s+([A-Z][\w&.\- ]{2,})$", title or "")
    return not (m and other(m.group(1)))


def _verbatim(needle: str, text: str) -> bool:
    n = re.sub(r"\s+", " ", needle).strip().lower()
    return len(n) >= 3 and n in text.lower()


def find_people(con, company_id: int, fetcher, *, llm, max_pages: int = 8, workflow_id=None) -> dict:
    from chanakya.llm.structured import ask_structured
    from chanakya.people.finder import upsert_person
    c = con.execute("SELECT id, name, domain, identity_status FROM companies WHERE id=?", (company_id,)).fetchone()
    if c is None or c["identity_status"] != "resolved" or ":" in str(c["domain"]):
        return {"company_id": company_id, "skipped": "no resolved domain"}
    if llm is None:
        return {"company_id": company_id, "skipped": "no model for route 'extraction'; no names extracted"}
    dom = c["domain"]
    pages, found, kept, rejected = [], [], [], []
    home = fetcher(f"https://{dom}/")
    pages.append({"url": home.url, "status": home.status, "robots_allowed": home.robots_allowed, "error": home.error})
    # Crawl at most two levels from the homepage (board pages often sit under About us), strongest pages first,
    # every URL fetched once (requested and final URL both count as seen).
    queue: list[str] = []
    seen_urls = {_key(home.url or f"https://{dom}/"), _key(f"https://{dom}/")}

    def enqueue(urls):
        for u in urls:
            if _key(u) not in seen_urls and _key(u) not in {_key(q) for q in queue}:
                queue.append(u)
        queue.sort(key=_rank)
    if home.text:
        enqueue(leadership_links(home.text, home.url or f"https://{dom}/", dom, limit=12))
    enqueue([f"https://{dom}{p}" for p in PATHS])
    seen = 0
    while queue and seen < max_pages and len(pages) < max_pages * 3:
        u = queue.pop(0)
        seen_urls.add(_key(u))
        p = fetcher(u)
        pages.append({"url": p.url, "status": p.status, "robots_allowed": p.robots_allowed, "error": p.error})
        if not p.text or (p.status or 0) >= 400 or (_key(p.url) in seen_urls and _key(p.url) != _key(u)):
            continue
        seen_urls.add(_key(p.url))
        seen += 1
        enqueue(leadership_links(p.text, p.url, dom, limit=12))
        txt = page_text(p.text)[:24000]
        out = ask_structured(llm, _People, system=SYSTEM, agent="website_people", purpose="extraction", con=con,
                             user=f"Organisation: {c['name']} ({dom})\nPage: {p.url}\n\n{txt}")
        for x in out.people:
            item = {"name": x.name.strip(), "title": x.title.strip(), "url": p.url}
            found.append(item)
            item["organisation"] = (x.organisation or "").strip()
            if not (_verbatim(item["name"], txt) and _verbatim(item["title"], txt)):
                rejected.append(item | {"why": "name or title not verbatim on the page"})
            elif not _same_org(item["organisation"], item["title"], c["name"], dom):
                rejected.append(item | {"why": "the page says this person works for another organisation"})
            else:
                kept.append(item)
    seen_keys, people = set(), []
    run_id = None
    if kept:
        from chanakya.storage.db import start_run
        run_id = start_run(con, "website_people", {"company_id": company_id, "workflow_id": workflow_id})
    for k in kept:
        key = (k["name"].lower(), k["title"].lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rec = {"full_name": k["name"], "job_title": k["title"], "source_url": k["url"], "extracted_by": "website_people"}
        pid, created = upsert_person(con, rec, company_id, mappings=_mappings(con), run_id=run_id, raw_response_id=None,
                                     tool="website-leadership-page", task_id=None, query=k["url"], source=SOURCE)
        people.append({"person_id": pid, "created": created, **k})
    if run_id:
        from chanakya.storage.db import finish_run
        finish_run(con, run_id, "done", {"company_id": company_id, "kept": len(people)})
    con.commit()
    return {"company_id": company_id, "domain": dom, "run_id": run_id, "pages": pages, "extracted": len(found), "kept": len(people),
            "rejected": rejected, "people": people, "at": now()}


def _mappings(con):
    from chanakya.bottomup.agent import approved_mappings
    return approved_mappings(con)
