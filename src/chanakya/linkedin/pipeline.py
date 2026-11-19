"""After a live LinkedIn accelerator search: CRM records -> leaders -> emails -> personalised drafts -> Outlook Drafts.

Runs outside the browser thread. Per company found (and its official website read from its LinkedIn About page):
1. the company enters the shared CRM (entity resolution; origin bottom_up, source linkedin_live);
2. leaders found on LinkedIn (CEO, CFO and the planned buyer titles) whose current role names the company become
   people + roles with their LinkedIn URL;
3. Email Hunter: published addresses on the company's own site first, then the domain's proven pattern; DNS MX is
   checked. Nothing is invented and no mailbox is probed: each address carries its status (verified on site /
   inferred / not found);
4. Claude (route outreach_draft) writes one personalised draft per person from the accelerator's approved catalogue
   entry and the facts seen on LinkedIn; proof/voice checks run; drafts with problems stay in Chanakya as
   'needs_editing';
5. clean drafts are saved to Outlook Drafts (never sent) for the user to review and send themselves. In Outlook test
   mode (CHANAKYA_OUTLOOK_TEST_MODE, default on) the recipient is left blank unless allowlisted.
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from chanakya.storage.db import jdump, now

LEADER_TITLES = ["CEO", "CFO"]


class DraftOut(BaseModel):
    subject: str = Field(description="under 70 characters, specific to the company")
    body: str = Field(description="90-160 words, plain text, ends with the signature given")
    personalisation: list[str] = Field(description="the facts from the input used to personalise it")


def _toks(s: str) -> set[str]:
    stop = {"the", "and", "of", "ltd", "limited", "inc", "plc", "llc", "group", "company", "co", "pvt", "private", "&"}
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in stop and len(t) > 2}


GENERIC = {"manufacturing", "medical", "device", "devices", "technology", "technologies", "solutions", "services",
           "industries", "industry", "international", "global", "systems", "food", "production", "foods", "india",
           "health", "healthcare", "engineering", "products", "consulting", "digital", "data", "software", "finance",
           "financial", "bank", "insurance", "logistics", "energy", "retail", "pharma", "pharmaceutical", "aerospace"}
NOT_A_COMPANY = re.compile(r"\b(women in|association|community|network|council|society|forum|federation|institute|"
                           r"magazine|news|jobs|careers|group of|alliance|club|chapter|summit|conference|expo|show|"
                           r"awards|professionals|hub|official .* page)\b", re.I)


def clean_company_name(name: str) -> str:
    """LinkedIn sometimes labels a card 'Page by Nexent Bank' or 'Nexent Bank Showcase': keep the company name."""
    n = re.sub(r"^\s*(showcase\s+)?page\s+by\s+", "", name or "", flags=re.I)
    n = re.sub(r"\s+showcase(\s+page)?\s*$", "", n, flags=re.I)
    return n.strip() or name


def real_company(c: dict, query: str = "") -> bool:
    """Drop LinkedIn pages that are communities, events or industry labels rather than companies."""
    name = c.get("title") or ""
    if NOT_A_COMPANY.search(name):
        return False
    nt = _toks(name)
    return bool(nt - GENERIC - _toks(query))  # at least one distinctive word beyond the industry words


def works_at(person: dict, company: str) -> bool:
    """The card's CURRENT role names the company by its distinctive words (not industry words). Protects against
    same-title people elsewhere."""
    text = person.get("current") or " ".join((person.get("lines") or [])[:1])
    ct = _toks(company) - GENERIC
    if not ct:
        return False
    return len(ct & _toks(text)) >= min(2, len(ct))


def add_company(con, c: dict, product_id: str) -> int:
    from chanakya.intel import entities as E
    r = E.resolve_company(con, name=c["title"], domain=c.get("domain"), linkedin_url=c["url"], direction=E.BOTTOM,
                          source="linkedin_live", detail={"product_id": product_id, "lines": c.get("lines"),
                                                          "query": c.get("query")})
    cid = r["company_id"]
    row = con.execute("SELECT data_json FROM companies WHERE id=?", (cid,)).fetchone()
    data = json.loads(row["data_json"] or "{}") if row else {}
    lines = c.get("lines") or []
    data.setdefault("industry", lines[0] if lines else None)
    data.setdefault("locality", lines[1] if len(lines) > 1 else None)
    data.setdefault("linkedin_url", c["url"])
    if c.get("website"):
        data.setdefault("website", c["website"])
    if c.get("about"):
        data.setdefault("description", c["about"][:1500])
    if c.get("hq"):
        data["hq"] = c["hq"]
    if c.get("location"):
        data["location"] = c["location"]
    if c.get("match_percent") is not None:
        data["linkedin_fit"] = {k: c.get(k) for k in ("match_percent", "match_why", "buy_percent", "buy_why",
                                                     "buy_sources", "matches", "accelerator")} | {"product_id": product_id}
    con.execute("UPDATE companies SET data_json=? WHERE id=?", (jdump(data), cid))
    con.execute("INSERT INTO company_sources(company_id,source,tool,source_url,query,raw_json,fetched_at) "
                "VALUES(?,?,?,?,?,?,?)", (cid, "linkedin_live", "visible-chrome", c["url"], c.get("query"), jdump(c),
                                         now()))
    con.commit()
    return cid


TITLE_RE = {"CEO": re.compile(r"\b(ceo|chief executive|managing director|founder & ceo|group ceo)\b", re.I),
            "CFO": re.compile(r"\b(cfo|chief financial|finance director|group cfo)\b", re.I)}
OTHER_ORG = re.compile(r"\b(?:at|of|@)\s+([A-Z][\w&.\- ]{2,})")


def leader_at(person: dict, company: str, kw: str) -> tuple[bool, bool]:
    """From the company's People tab: keep if the headline has the CEO/CFO title AND names this company (verified) or
    names no other organisation (kept, employer not confirmed). 'CEO of <other org>' is dropped."""
    head = next((l for l in person.get("lines") or [] if TITLE_RE[kw].search(l)), "")
    if not head:
        return False, False
    ct = _toks(company) - GENERIC
    if ct and ct & _toks(head):
        return True, True
    # employer not named: keep only a bare title ("Group CFO"), never one that names another organisation
    first = re.split(r"\s[|·•]\s|\s-\s", head)[0]
    if re.search(r",|\s(at|of|for)\s|@", first, re.I) or len(first) > 45:
        return False, False
    return True, False


def add_person(con, p: dict, company_id: int, query: str, verified: bool | None = True) -> int:
    from chanakya.people.finder import upsert_person
    title = p.get("current") or next((l for l in p.get("lines") or [] if TITLE_RE["CEO"].search(l) or
                                      TITLE_RE["CFO"].search(l)), None) or (p.get("lines") or [None])[0]
    if title and "|" in title:  # long headlines: keep the part that holds the leadership title
        parts = [x.strip() for x in title.split("|")]
        title = next((x for x in parts if TITLE_RE["CEO"].search(x) or TITLE_RE["CFO"].search(x)), parts[0])
    if title and " at " in title.lower():
        title = title[:title.lower().rfind(" at ")].strip()
    pid, _ = upsert_person(con, {"full_name": p["title"], "linkedin_url": p.get("url"), "title": title,
                                 "source_url": p.get("source_url")}, company_id,
                           mappings=[], run_id=None, raw_response_id=None, tool="visible-chrome", task_id=None,
                           query=query, source="linkedin-harness")
    con.execute("UPDATE people SET verification=? WHERE id=? AND (verification IS NULL OR verification LIKE 'linkedin:%')",
                ("linkedin: current role names company" if verified else
                 "linkedin: on company People tab, employer not named in headline - confirm before sending", pid))
    con.commit()
    return pid


LEADER_RE = re.compile(r"\b(ceo|cfo|chief executive|chief financial|managing director|group md)\b", re.I)


def is_leader(con, person_id: int) -> bool:
    """Drafts go to the CEO / CFO of each company found (the user's choice)."""
    return any(LEADER_RE.search(r[0] or "") for r in con.execute("SELECT title FROM person_roles WHERE person_id=?",
                                                                  (person_id,)))


def xray_missing_linkedin(con, person_id: int) -> str | None:
    """Agent 04 fallback: leaders without a LinkedIn URL get a public-profile lookup by web search (no login)."""
    from chanakya.linkedin.claude_research import xray_linkedin
    r = con.execute("SELECT p.full_name, p.linkedin_url, c.name FROM people p JOIN person_roles pr ON pr.person_id=p.id "
                    "JOIN companies c ON c.id=pr.company_id WHERE p.id=? LIMIT 1", (person_id,)).fetchone()
    if r is None or r["linkedin_url"]:
        return None
    u = xray_linkedin(r["full_name"], r["name"])
    if u and not con.execute("SELECT 1 FROM people WHERE linkedin_url=? AND id<>?", (u, person_id)).fetchone():
        con.execute("UPDATE people SET linkedin_url=? WHERE id=?", (u, person_id))
        con.commit()
        return u
    return None


def find_email(con, person_id: int, fetcher=None, mx_lookup=None) -> dict:
    from chanakya.emailhunter import agent as EH
    from chanakya.emailhunter.checks import check_mx, default_fetcher
    r = con.execute("SELECT p.full_name, c.id cid, c.domain FROM people p JOIN person_roles r ON r.person_id=p.id "
                    "JOIN companies c ON c.id=r.company_id WHERE p.id=? ORDER BY r.id DESC LIMIT 1", (person_id,)).fetchone()
    if r is None or not r["domain"] or "." not in (r["domain"] or ""):
        return {"email": None, "status": "not_found", "reason": "company website unknown"}
    have = con.execute("SELECT email, status FROM email_records WHERE person_id=? ORDER BY id DESC LIMIT 1",
                       (person_id,)).fetchone()
    if have:
        return {"email": have["email"], "status": have["status"], "reused": True}
    cand = EH.EmailCandidate(person_id, r["full_name"], r["cid"], r["domain"])
    fetcher = fetcher or default_fetcher()
    site = EH.scrape_domain(con, r["domain"], fetcher, None)
    mx = (mx_lookup or check_mx)(r["domain"])
    inf = EH.infer_pattern(r["domain"], site, EH.known_names(con, r["cid"], person_id))
    f = EH.classify(cand, site, inf, mx)
    if f.status in ("unverified", "not_found") or not f.email:
        # Open web search (Claude + web search): an address published verbatim on a public page beats a pattern guess
        from chanakya.linkedin.claude_research import web_email
        con.commit()  # the web search can take a minute: release the write lock first
        w = web_email(r["full_name"], con.execute("SELECT name FROM companies WHERE id=?", (r["cid"],)).fetchone()[0],
                      r["domain"])
        if w.get("email"):
            on_dom = w["email"].endswith("@" + r["domain"]) or w["email"].endswith("." + r["domain"])
            f = EH.Finding(w["email"], "open_web", "valid" if on_dom and mx.status != "none" else "risky",
                           0.85 if on_dom else 0.5, source_url=w["source_url"],
                           evidence=[{"published_at": w["source_url"], "on_company_domain": on_dom}])
    EH.store_finding(con, cand, f, mx, None)
    con.commit()
    return {"email": f.email, "status": f.status, "method": f.method}


def _prospect(con, person_id: int, company_id: int) -> int:
    row = con.execute("SELECT id FROM prospects WHERE person_id=?", (person_id,)).fetchone()
    if row:
        return row["id"]
    return con.execute("INSERT INTO prospects(person_id,company_id,record_json,score,tier,issues_json,conflicts_json,"
                       "qa_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (person_id, company_id, jdump({"source": "linkedin_live"}), 0, "unscored", "[]", "[]",
                        "not_run", now())).lastrowid


def best_product(con, person_id: int) -> str | None:
    """The accelerator that matched this person's company best in a LinkedIn run (multi-accelerator runs)."""
    for r in con.execute("SELECT c.data_json FROM person_roles r JOIN companies c ON c.id=r.company_id WHERE r.person_id=? "
                         "ORDER BY r.id DESC", (person_id,)):
        pid = (json.loads(r[0] or "{}").get("linkedin_fit") or {}).get("product_id")
        if pid:
            return pid
    return None


def write_draft(con, person_id: int, product_id: str, llm, sender: str) -> dict:
    from chanakya.linkedin.planner import catalogue
    from chanakya.llm.structured import ask_structured
    from chanakya.outreach import drafts as D
    existing = con.execute("SELECT id, status FROM outreach_drafts WHERE person_id=? AND status NOT IN "
                           "('discarded','rejected') ORDER BY id DESC LIMIT 1", (person_id,)).fetchone()
    if existing:
        if existing["status"] == "failed":  # only the earlier Outlook save failed; the draft itself is fine
            from chanakya.outreach import drafts as D
            D._change(con, existing["id"], "generated", by="linkedin_outreach",
                      reason="draft is fine; earlier Outlook save failed, ready to save again")
            return {"draft_id": existing["id"], "status": "generated", "reused": True}
        return {"draft_id": existing["id"], "status": existing["status"], "reused": True}
    p = con.execute("SELECT p.full_name, p.linkedin_url, r.title, c.id cid, c.name company, c.domain, c.data_json "
                    "FROM people p JOIN person_roles r ON r.person_id=p.id JOIN companies c ON c.id=r.company_id "
                    "WHERE p.id=? ORDER BY r.id DESC LIMIT 1", (person_id,)).fetchone()
    cat = catalogue(con, product_id)
    proof = [dict(x) for x in con.execute("SELECT * FROM accelerator_proof WHERE product_id=?", (product_id,))] \
        if con.execute("SELECT name FROM sqlite_master WHERE name='accelerator_proof'").fetchone() else []
    email = con.execute("SELECT email, status FROM email_records WHERE person_id=? ORDER BY id DESC LIMIT 1",
                        (person_id,)).fetchone()
    facts = {"person": {"name": p["full_name"], "title": p["title"], "linkedin": p["linkedin_url"]},
             "company": {"name": p["company"], "website": p["domain"], **{k: v for k, v in json.loads(
                 p["data_json"] or "{}").items() if k in ("industry", "locality", "description", "size")}},
             "accelerator": {k: cat[k] for k in ("name", "summary", "problems_solved", "needs")},
             "approved_proof_points": proof}
    con.commit()  # no write lock held while the model writes the draft
    out = ask_structured(
        llm, DraftOut, agent="linkedin_outreach", purpose="personalised outreach draft", con=con,
        system="You write a first outreach email from Wayam to a senior leader. Use ONLY the facts given. Personalise "
               "with their role and what the company does; connect ONE problem the accelerator solves to their "
               "situation as a question, not an assertion about their business. No performance numbers, client names "
               "or results unless they are in approved_proof_points. No flattery, no 'I hope this finds you well'. "
               "British English, plain and direct, one clear ask (a 20-minute conversation). Sign off exactly with: "
               + sender,
        user=json.dumps(facts, ensure_ascii=False))
    t = now()
    pr = _prospect(con, person_id, p["cid"])
    did = con.execute("INSERT INTO outreach_drafts(prospect_id,person_id,channel,to_address,subject,body,hooks_json,"
                      "generator,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (pr, person_id, "email", email["email"] if email else None, out.subject, out.body,
                       jdump({"product_id": product_id, "accelerator": cat["name"], "evidence": out.personalisation,
                              "email_status": email["status"] if email else "not_found", "source": "linkedin_live"}),
                      f"model:{getattr(llm, 'model', '')}", "generated", t, t)).lastrowid
    con.execute("INSERT INTO outreach_draft_history(draft_id,action,from_status,to_status,subject,body,reviewer,reason,"
                "at) VALUES(?,?,?,?,?,?,?,?,?)", (did, "generated", None, "generated", out.subject, out.body,
                                                  "linkedin_outreach", "personalised from LinkedIn + catalogue", t))
    con.commit()
    bad = D._quality(con, did)
    if bad:
        D._change(con, did, "needs_editing", by="quality check", reason="; ".join(bad)[:500])
        return {"draft_id": did, "status": "needs_editing", "issues": bad}
    return {"draft_id": did, "status": "generated"}


def save_to_outlook(con, draft_id: int, by: str, session_factory=None, user_id: int | None = None) -> dict:
    """Same route as the app's Save to Outlook button: Chanakya's Entra app (the user's own connection) when it is
    configured, else the Outlook MCP server."""
    import os
    from chanakya.outreach import graph_oauth as G
    from chanakya.outreach import outlook as O
    prov = (os.getenv("OUTLOOK_PROVIDER") or ("graph" if G.configured() else "mcp")).lower()
    if session_factory is None and prov == "graph":
        uid = user_id
        if uid is None:
            row = con.execute("SELECT user_id FROM outlook_connections ORDER BY rowid DESC LIMIT 1").fetchone() \
                if con.execute("SELECT name FROM sqlite_master WHERE name='outlook_connections'").fetchone() else None
            uid = row[0] if row else None
        if uid is None:
            return {"saved": False, "error": "Outlook is not connected: Settings > Outlook > Connect Microsoft 365, "
                                             "then press 'Save pending drafts to Outlook'"}
        session_factory = lambda: G.GraphSession(con, uid)  # noqa: E731
    try:
        kw = {"session_factory": session_factory} if session_factory else {}
        r = O.create_outlook_draft(con, draft_id, by=by, allow_unreviewed=True, **kw)
        return {"saved": True, "verified": r.get("verified_in_drafts"), "recipient": r.get("recipient")}
    except Exception as e:  # noqa: BLE001 - reported per draft; the run continues
        msg = str(e)[:300] or type(e).__name__
        if "HTTP 500" in msg or "HTTP 502" in msg:
            msg += (" | Microsoft could not reach this mailbox (Graph 5xx on every mail call). Check the account's "
                    "Exchange Online licence / that the mailbox opens in outlook.office.com, then retry.")
        return {"saved": False, "error": msg}
