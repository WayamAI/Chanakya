"""Claude web research for companies found on LinkedIn: official website, CEO, CFO, one-line summary, with sources.

Runs for all companies of a run in parallel (thread pool), so leaders are known before the browser visits each
company; the browser then only fills what Claude could not source. Uses the Anthropic SDK with the server-side web
search tool. Claude must give a source URL for every person; anything without one is dropped. Model: the project's
research route (CHANAKYA_MODEL_SONNET, default claude-sonnet-5-5).
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

SYSTEM = ("You research companies for B2B outreach. Use web search. Return ONLY one JSON object, no prose:\n"
          '{"website": "official domain or null", "summary": "one sentence on what the company does",\n'
          ' "ceo": {"name": "...", "title": "exact current title", "linkedin_url": "https://www.linkedin.com/in/... or null",'
          ' "source_url": "page that states it"} or null,\n'
          ' "cfo": {same fields} or null, "notes": "short caveats"}\n'
          "Rules: CURRENT office holders only, confirmed on the company's own site, a filing, or reputable news within "
          "the last 2 years. If a source is older or unclear, use null. Never guess a name or a LinkedIn URL. "
          "'ceo' may be a Managing Director or President if that is the top executive; 'cfo' may be a Finance Director.\n"
          "If an ACCELERATOR is given, also score the company against it with this rubric and add to the JSON:\n"
          ' "fit": {"industry": 0-40, "industry_why": "...", "problem": 0-40, "problem_why": "which catalogue problem this '
          'company plausibly has and why", "size_region": 0-20, "size_region_why": "..."},\n'
          ' "buying": {"signals": 0-50, "signals_found": [{"what": "hiring/funding/transformation/new leader/regulation/'
          'expansion...", "date": "YYYY-MM or null", "source_url": "..."}], "recency": 0-25, "recency_why": "..."}\n'
          "Score conservatively: no evidence means a low score. Signals need a source URL; recency 25 = within 6 months, "
          "12 = 6-18 months, 0 = older or none.")


def _client():
    import anthropic
    return anthropic.Anthropic(max_retries=2, timeout=120.0)


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def score(out: dict) -> dict:
    """Totals computed here (not by the model) so the percentages always equal the visible parts."""
    f, b = out.get("fit") or {}, out.get("buying") or {}
    clip = lambda v, hi: max(0, min(hi, int(v))) if isinstance(v, (int, float)) else 0  # noqa: E731
    sig = [x for x in (b.get("signals_found") or []) if isinstance(x, dict) and str(x.get("source_url", "")).startswith("http")]
    signals = clip(b.get("signals"), 50) if sig else 0  # no sourced signal, no signal points
    reach = (10 if out.get("website") else 0) + (8 if out.get("ceo") else 0) + (7 if out.get("cfo") else 0)
    match = clip(f.get("industry"), 40) + clip(f.get("problem"), 40) + clip(f.get("size_region"), 20)
    buy = signals + (clip(b.get("recency"), 25) if sig else 0) + reach
    out["match_percent"], out["buy_percent"] = match, buy
    out["match_why"] = (f"Industry {clip(f.get('industry'), 40)}/40: {f.get('industry_why') or '-'} | Problem "
                        f"{clip(f.get('problem'), 40)}/40: {f.get('problem_why') or '-'} | Size/region "
                        f"{clip(f.get('size_region'), 20)}/20: {f.get('size_region_why') or '-'}")
    out["buy_why"] = (f"Signals {signals}/50: " + ("; ".join(f"{x.get('what')} ({x.get('date') or 'undated'})" for x in sig)
                                                  or "none with a source") +
                      f" | Recency {clip(b.get('recency'), 25) if sig else 0}/25: {b.get('recency_why') or '-'}"
                      f" | Reachability {reach}/25: website {'yes' if out.get('website') else 'no'}, CEO "
                      f"{'found' if out.get('ceo') else 'not found'}, CFO {'found' if out.get('cfo') else 'not found'}")
    out["buy_sources"] = [x.get("source_url") for x in sig]
    return out


def extras_prompt(extras: dict | None) -> str:
    """Optional run settings: extra buyer roles to map, a location the run is limited to, and Wayam's authority
    content to compare each company against. Each adds one JSON key to the reply."""
    if not extras:
        return ""
    out = []
    if roles := [r for r in (extras.get("roles") or []) if isinstance(r, str) and r.strip()]:
        out.append("BUYER ROLES: also find the CURRENT person in each of these roles (same sourcing rules as CEO/CFO; "
                   "the closest real title is fine, e.g. 'Head of O&M' for 'fleet O&M manager'). Add \"buyers\": "
                   "[{\"role\": role exactly as given, \"name\": \"...\", \"title\": \"exact current title\", "
                   "\"linkedin_url\": \"https://www.linkedin.com/in/... or null\", \"source_url\": \"page that states "
                   "it\"}]. Leave out any role you cannot source. Roles: " + json.dumps(roles, ensure_ascii=False))
    if geo := (extras.get("geography") or "").strip():
        out.append(f"LOCATION: this search is limited to {geo}. Add \"location\": {{\"hq\": \"city, country\", "
                   f"\"in_geography\": true or false, \"sites\": \"plants/offices/projects in {geo}, if any\", "
                   f"\"source_url\": \"...\"}}. in_geography is true only if the HQ or a real operating site or project "
                   f"is in {geo}. Use {geo} when scoring size_region.")
    if band := _band(extras):
        out.append(f"REVENUE: this search targets companies with annual revenue between ₹{band[0]:,.0f} Cr and "
                   f"₹{band[1]:,.0f} Cr. Add \"revenue\": {{\"inr_cr\": latest annual revenue (operations, "
                   "consolidated if published) in INR crore as a number, or null if no source states it, "
                   "\"fiscal_year\": \"FY2025\", \"source_url\": \"annual report, filing or credible press page that "
                   "states it\"}. Convert USD at the rate of that year if only USD is published. Never estimate.")
    fit = ("Add \"content_fit\": {\"best\": the exact title of the piece most relevant to this company, \"score\": "
           "0-10 (how directly it speaks to a problem this company has, with evidence), \"why\": \"which problem, and "
           "the evidence\"}.")
    raw = extras.get("authority_content")
    if isinstance(raw, dict):  # per accelerator (master-sheet use cases): pick only from the best match's pieces
        by_acc = {str(k): [c for c in v if isinstance(c, str) and c.strip()] for k, v in raw.items()
                  if isinstance(v, list)}
        if by_acc := {k: v for k, v in by_acc.items() if v}:
            out.append("AUTHORITY CONTENT: Wayam has these pieces of content (client use cases), per accelerator: "
                       + json.dumps(by_acc, ensure_ascii=False) + ". Choose only from the list of the accelerator that "
                       "is this company's best match. " + fit)
    elif content := [c for c in (raw or []) if isinstance(c, str) and c.strip()]:
        out.append("AUTHORITY CONTENT: Wayam has these pieces of content for this accelerator: "
                   + json.dumps(content, ensure_ascii=False) + ". " + fit)
    return ("\n\n" + "\n".join(out)) if out else ""


def _band(extras: dict | None) -> tuple[float, float] | None:
    r = (extras or {}).get("revenue") or {}
    lo, hi = r.get("min_cr"), r.get("max_cr")
    if isinstance(lo, (int, float)) or isinstance(hi, (int, float)):
        return float(lo or 0), float(hi if isinstance(hi, (int, float)) else float("inf"))
    return None


def apply_extras(out: dict, extras: dict | None = None) -> dict:
    """Keep only sourced buyers; flatten location, revenue and content fit for scoring, export and filtering.
    The revenue band is decided here from a sourced figure, never from the model's own judgement."""
    buyers = []
    for b in out.get("buyers") or []:
        if isinstance(b, dict) and b.get("name") and str(b.get("source_url") or "").startswith("http"):
            if b.get("linkedin_url") and "linkedin.com/in/" not in b["linkedin_url"]:
                b["linkedin_url"] = None
            buyers.append(b)
    out["buyers"] = buyers
    loc = out.get("location") if isinstance(out.get("location"), dict) else {}
    out["location"] = loc
    out["hq"] = loc.get("hq")
    out["in_geography"] = loc.get("in_geography") if isinstance(loc.get("in_geography"), bool) else None
    cf = out.get("content_fit") if isinstance(out.get("content_fit"), dict) else {}
    s = cf.get("score")
    out["content_score"] = max(0, min(10, int(s))) if isinstance(s, (int, float)) else None
    out["content_best"], out["content_why"] = cf.get("best"), cf.get("why")
    if band := _band(extras):
        rv = out.get("revenue") if isinstance(out.get("revenue"), dict) else {}
        cr, src = rv.get("inr_cr"), str(rv.get("source_url") or "")
        sourced = isinstance(cr, (int, float)) and not isinstance(cr, bool) and src.startswith("http")
        out["revenue_cr"] = cr if sourced else None
        out["revenue_fy"], out["revenue_source"] = rv.get("fiscal_year"), src or None
        out["in_revenue_band"] = (band[0] <= cr <= band[1]) if sourced else None
    return out


EVENTS = ("You find industry events for B2B business development. Use web search. Return ONLY JSON: {\"events\": "
          "[{\"name\": \"...\", \"date\": \"YYYY-MM-DD or YYYY-MM\", \"city\": \"...\", \"country\": \"...\", "
          "\"organiser\": \"...\", \"kind\": \"conference / forum / awards / webinar / specialist press\", "
          "\"url\": \"official event page\", \"audience\": \"who attends\", \"why\": \"why the buyers of this "
          "accelerator will be there\"}]}. Only UPCOMING events (today or later, within 12 months), each with its "
          "official page. Specialist press (magazines, newsletters) may have no date. Never invent an event.")


def find_events(accelerator: dict, roles: list[str] | None = None, geography: str | None = None,
                sources: list[str] | None = None, client=None) -> list[dict]:
    """Relevant upcoming events and specialist press for one accelerator's buyers (a second output of a run)."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return []
    import datetime as _dt
    ask = (f"Today: {_dt.date.today().isoformat()}\nACCELERATOR: " + json.dumps(
        {k: accelerator.get(k) for k in ("name", "summary", "problems_solved", "target_industries")},
        ensure_ascii=False)[:3000] + f"\nBUYERS: {', '.join(roles or accelerator.get('buyer_roles') or [])}"
           + (f"\nLOCATION: events in or for {geography} first, then major international ones." if geography else "")
           + (f"\nSOURCES to check first: {'; '.join(sources)}" if sources else "")
           + "\nList up to 12.")
    try:
        out = _ask(EVENTS, ask, client, uses=8, max_tokens=8000)  # 12 events need ~4k tokens; 2k truncated them
    except Exception:  # noqa: BLE001 - events are optional; the run continues without them
        return []
    return [e for e in (out.get("events") or []) if isinstance(e, dict) and e.get("name")
            and str(e.get("url") or "").startswith("http")]


MULTI = ("\nSEVERAL ACCELERATORS are given. Instead of one \"fit\", return \"fits\": a list with one entry per "
         "accelerator that plausibly fits (at most 3, best first): {\"accelerator\": exact name, \"industry\": 0-40, "
         "\"industry_why\", \"problem\": 0-40, \"problem_why\", \"size_region\": 0-20, \"size_region_why\"}. "
         "\"buying\" stays one object for the company.")


def score_multi(out: dict, names: list[str]) -> dict:
    """Per-accelerator match totals; the best one becomes the company's match."""
    fits = [f for f in (out.get("fits") or []) if isinstance(f, dict) and f.get("accelerator") in names]
    scored = []
    for f in fits:
        one = score({**out, "fit": f})
        scored.append({"accelerator": f["accelerator"], "match_percent": one["match_percent"], "match_why": one["match_why"]})
    scored.sort(key=lambda x: -x["match_percent"])
    base = score({**out, "fit": fits[0] if fits else {}})
    base["matches"] = scored
    if scored:
        base["best_accelerator"], base["match_percent"], base["match_why"] = (scored[0]["accelerator"],
                                                                           scored[0]["match_percent"], scored[0]["match_why"])
    return base


def research_company(company: dict, client=None, accelerator: dict | list | None = None,
                     extras: dict | None = None) -> dict:
    """With an accelerator, a reply without scores is retried once; if it still has none, the company is marked
    not scored (match/buy None) instead of 0%."""
    out = _research_once(company, client, accelerator, extras)
    if accelerator and not out.get("error") and not _has_fit(out):
        out = _research_once(company, client, accelerator, extras)
    if accelerator and not _has_fit(out):
        out["match_percent"] = out["buy_percent"] = None
        out["match_why"] = out["buy_why"] = "Not scored: the research reply had no scores" + (
            f" ({out['error']})" if out.get("error") else "")
    return out


def _has_fit(out: dict) -> bool:
    return bool(out.get("fit") or out.get("fits"))


def _research_once(company: dict, client=None, accelerator: dict | list | None = None,
                   extras: dict | None = None) -> dict:
    """company: {title, url (LinkedIn), lines}. Returns the JSON above plus 'error' on failure."""
    client = client or _client()
    model = os.getenv("CHANAKYA_MODEL_SONNET", "claude-sonnet-5-5")
    ask = (f"Company: {company['title']}\nLinkedIn page: {company.get('url')}\n"
           f"LinkedIn shows: {' | '.join((company.get('lines') or [])[:3])}\n"
           "Find its official website, current CEO and current CFO.")
    multi = isinstance(accelerator, list) and len(accelerator) > 1
    if isinstance(accelerator, list) and len(accelerator) == 1:
        accelerator = accelerator[0]
    if multi:
        brief = [{k: a.get(k) for k in ("name", "summary", "problems_solved", "target_industries")} for a in accelerator]
        ask += "\n\nACCELERATORS (score the company against each that fits):\n" + json.dumps(brief, ensure_ascii=False)[:12000] + MULTI
    elif accelerator:
        ask += "\n\nACCELERATOR (score the company against this):\n" + json.dumps(accelerator, ensure_ascii=False)[:4000]
    ask += extras_prompt(extras)
    try:
        r = client.messages.create(
            model=model, max_tokens=4000, system=SYSTEM,
            output_config={"effort": "low"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}],
            messages=[{"role": "user", "content": ask}])
        if r.stop_reason == "refusal":
            return {"error": "model declined"}
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        out = _json(text)
        for k in ("ceo", "cfo"):  # a person without a source is not kept
            p = out.get(k)
            if not isinstance(p, dict) or not p.get("name") or not str(p.get("source_url") or "").startswith("http"):
                out[k] = None
            elif p.get("linkedin_url") and "linkedin.com/in/" not in p["linkedin_url"]:
                p["linkedin_url"] = None
        out["model"] = model
        if extras:
            apply_extras(out, extras)
        if multi:
            return score_multi(out, [a["name"] for a in accelerator])
        return score(out) if accelerator else out
    except Exception as e:  # noqa: BLE001 - one company's failure never stops the run
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def research_all(companies: list[dict], workers: int = 6, accelerator: dict | list | None = None,
                 extras: dict | None = None) -> list[dict]:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return [{"error": "ANTHROPIC_API_KEY not set"} for _ in companies]
    client = _client()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda c: research_company(c, client, accelerator, extras), companies))


XRAY = ("Find the public LinkedIn profile of one person using web search (like a Google X-ray search: "
        "site:linkedin.com/in name company). Return ONLY JSON: {\"linkedin_url\": \"https://www.linkedin.com/in/...\" "
        "or null, \"evidence\": \"the search result title/snippet that matches name AND company\"}. Only return a URL "
        "whose result names this person at this company. Never guess.")

WEB_EMAIL = ("Find an email address for one person that is PUBLISHED on the open web (company press release, speaker "
             "page, GitHub, conference programme, official filing). Return ONLY JSON: {\"email\": \"...\" or null, "
             "\"source_url\": \"page where it appears verbatim\"}. Never construct or guess an address; if you cannot see "
             "it written on a page, return null.")


def _ask(system: str, user: str, client=None, uses: int = 3, max_tokens: int = 2000) -> dict:
    client = client or _client()
    r = client.messages.create(
        model=os.getenv("CHANAKYA_MODEL_SONNET", "claude-sonnet-5-5"), max_tokens=max_tokens, system=system,
        output_config={"effort": "low"},
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": uses}],
        messages=[{"role": "user", "content": user}])
    if r.stop_reason == "refusal":
        return {}
    return _json("".join(b.text for b in r.content if getattr(b, "type", "") == "text"))


def xray_linkedin(name: str, company: str, client=None) -> str | None:
    """Agent 04 step 2 (Search X-ray, no login): public LinkedIn URL from web search results."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        u = (_ask(XRAY, f"Person: {name}\nCompany: {company}", client).get("linkedin_url") or "").split("?")[0]
        return u if re.match(r"^https://([a-z]+\.)?linkedin\.com/in/[^/]+/?$", u) else None
    except Exception:  # noqa: BLE001
        return None


def web_email(name: str, company: str, domain: str | None, client=None) -> dict:
    """Agent 05 step 3 (Open web search): an address published verbatim on a public page, with its source."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {}
    try:
        out = _ask(WEB_EMAIL, f"Person: {name}\nCompany: {company}\nCompany domain: {domain or 'unknown'}", client)
    except Exception:  # noqa: BLE001
        return {}
    e, src = (out.get("email") or "").strip().lower(), out.get("source_url") or ""
    if not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", e) or not src.startswith("http"):
        return {}
    return {"email": e, "source_url": src}
