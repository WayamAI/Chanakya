"""Platform and positioning rules from the Lakshya handover.

LinkedIn. The handover says "Never scrape LinkedIn (terms)" and Studio deliberately used a LinkedIn people-search link
per lead instead. LinkedIn's User Agreement prohibits software or scripts that scrape or copy the service. The
LinkedIn harness automates a logged-in browser, so it is DISABLED by default. Compliant route: Chanakya builds the
people-search link, a human opens it, and pastes what they found (manual import); public web pages and licensed data
(Clay) are used for everything else. Enabling the harness is a recorded business decision (settings.linkedin_automation).

Naming NDA. Wayam AI may be named in private outreach; it must not be named in public content before
settings.public_naming_allowed_from (handover: "about Jan 2027"). Verify with Vineet before any public post.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from chanakya.intel import settings as S


def linkedin_automation_enabled(con) -> bool:
    return S.get(con, "linkedin_automation") == "enabled"


def linkedin_people_search_link(role: str, company: str = "") -> str:
    q = " ".join(x for x in [(role or "").strip(), (company or "").strip()] if x)
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(q)


def public_naming_allowed(con, on: dt.date | None = None) -> bool:
    d = dt.date.fromisoformat(S.get(con, "public_naming_allowed_from"))
    return (on or dt.date.today()) >= d


def manual_route(con, queries: list[str], company: str = "") -> dict:
    return {"policy": "LinkedIn automation is disabled (LinkedIn terms; handover rule 'Never scrape LinkedIn').",
            "manual_steps": "Open each link, review the people yourself, then paste the relevant profiles into "
                            "Discovery > Import people (name, headline, profile URL, company, company domain).",
            "links": [{"query": q, "url": linkedin_people_search_link(q, company)} for q in queries]}


def linkedin_capabilities(con, *, harness_configured: bool, clay_mode: str | None) -> dict:
    """What Chanakya can and cannot do with LinkedIn data, by HOW the data is obtained. The UI shows this table, so
    nothing is presented as automated scraping when it is a human step, and nothing restricted looks available.

    class: automated            runs without a person and touches no LinkedIn page
           authorised_provider  data licensed from a provider (Clay), not fetched from LinkedIn by Chanakya
           human_assisted       a person opens LinkedIn in their own session and decides what to bring in
           restricted           technically present, switched off by policy (LinkedIn User Agreement)
           unavailable          not available to Wayam today
    """
    enabled = linkedin_automation_enabled(con)
    harness = ("enabled by a recorded decision" if enabled else "disabled by policy") if harness_configured \
        else "not installed"
    clay_ok = clay_mode == "live"
    clay_state = "available" if clay_ok else f"needs Clay connection ({clay_mode or 'never tested'})"
    rows = [
        {"capability": "LinkedIn people-search link per role and company", "class": "automated",
         "status": "available", "how": "Chanakya builds the search URL; it never opens it",
         "where": "Discover; account page (roles named in signals); bottom-up run"},
        {"capability": "Review search results and choose people", "class": "human_assisted", "status": "available",
         "how": "A person opens the link in their own LinkedIn session", "where": "Discover"},
        {"capability": "Import chosen profiles (name, headline, profile URL, company, domain)",
         "class": "human_assisted", "status": "available",
         "how": "Pasted rows; deduplicated by profile URL; stored as manual-import with the importer's name",
         "where": "Discover > Import"},
        {"capability": "Normalise, deduplicate and link imported people to the canonical company",
         "class": "automated", "status": "available",
         "how": "Shared entity resolver (domain, LinkedIn company URL, key, name); one person per profile URL",
         "where": "runs on every import"},
        {"capability": "Verify the company of an imported person", "class": "automated", "status": "available",
         "how": "Fetches the company's own homepage (robots.txt respected) and checks it names the organisation",
         "where": "runs on every import with a domain"},
        {"capability": "Verify current role and find decision-makers without LinkedIn", "class": "automated",
         "status": "available",
         "how": "Leadership-page agent reads the company's own board/leadership pages; name and title must be "
                "verbatim on the page", "where": "site_people stage; account page"},
        {"capability": "Relevance, accelerator match, company research, scoring, draft for imported people",
         "class": "automated", "status": "available",
         "how": "The normal pipeline runs on imported records (Sonnet relevance and match, website evidence, score)",
         "where": "bottom-up run after import"},
        {"capability": "People and contact search with LinkedIn profile URLs", "class": "authorised_provider",
         "status": clay_state, "how": "Clay search-contacts under Clay's data licence; exact query confirmed first",
         "where": "people stage; Review > Clay and spend"},
        {"capability": "Automated LinkedIn people search, profile and company page reading, profile research",
         "class": "restricted", "status": harness,
         "how": "Mriganka's browser harness drives a logged-in LinkedIn session. LinkedIn's User Agreement prohibits "
                "automated access, so it is off unless the business records a decision to enable it "
                "(setting linkedin_automation). No login wall, CAPTCHA or rate limit is ever bypassed.",
         "where": "bu_search, bu_profile, bu_company, resolve, research stages (skipped while off)"},
        {"capability": "LinkedIn official API for people search or profile data", "class": "unavailable",
         "status": "not available",
         "how": "LinkedIn offers people data only to approved partners (Sales Navigator / Talent / Marketing "
                "programmes). Wayam has no such access; nothing in Chanakya uses it.", "where": "-"},
        {"capability": "Messaging, connection requests, posting", "class": "unavailable", "status": "never",
         "how": "Not implemented and blocked in the harness client (NEVER_ALLOWED commands)", "where": "-"},
    ]
    return {"automation_setting": "enabled" if enabled else "disabled", "harness_configured": harness_configured,
            "policy": "LinkedIn's User Agreement prohibits automated access. Chanakya uses human-assisted search, "
                      "company websites and a licensed provider (Clay) instead.",
            "rows": rows}
