"""UK Companies House (official public register API): find a company's registration and its current officers.

Officers (directors, secretaries) are saved through the shared people dedupe with source "companies_house". Each role
starts as needs_manual_review, and the officer's register URL is kept as evidence. Resigned officers are skipped.
Only companies registered in the UK have records; public bodies like the FCA usually have none, and the answer says so.

Configuration: COMPANIES_HOUSE_API_KEY (free; register an application at developer.company-information.service.gov.uk,
REST key). The API allows 600 requests per 5 minutes.
"""
from __future__ import annotations

import os
import re

import httpx

BASE = "https://api.company-information.service.gov.uk"


class CHError(RuntimeError):
    pass


def configured() -> bool:
    return bool((os.getenv("COMPANIES_HOUSE_API_KEY") or "").strip())


def _get(path: str, params=None, transport=None) -> dict:
    if not configured():
        raise CHError("Companies House is not configured: add COMPANIES_HOUSE_API_KEY (free REST key) to .env")
    with httpx.Client(timeout=20, transport=transport, auth=(os.environ["COMPANIES_HOUSE_API_KEY"].strip(), "")) as c:
        r = c.get(f"{BASE}{path}", params=params)
    if r.status_code == 401:
        raise CHError("Companies House rejected the API key (401)")
    if r.status_code == 429:
        raise CHError("Companies House rate limit reached; try again in 5 minutes")
    if r.status_code == 404:
        return {}
    if r.status_code >= 400:
        raise CHError(f"Companies House HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def search(name: str, transport=None) -> list[dict]:
    d = _get("/search/companies", {"q": name, "items_per_page": 5}, transport)
    return [{"number": i.get("company_number"), "title": i.get("title"), "status": i.get("company_status"),
             "address": i.get("address_snippet"), "created": i.get("date_of_creation")} for i in d.get("items") or []]


def _person_name(raw: str) -> str:
    # register format "SURNAME, Forenames" -> "Forenames Surname"
    if "," in raw:
        last, first = raw.split(",", 1)
        raw = f"{first.strip()} {last.strip().title()}"
    return re.sub(r"\s+", " ", raw).strip()


def import_officers(con, company_id: int, number: str, *, by: str, transport=None) -> dict:
    from chanakya.bottomup.agent import approved_mappings
    from chanakya.intel import scoring as SC
    from chanakya.people.finder import upsert_person
    prof = _get(f"/company/{number}", transport=transport)
    if not prof:
        raise CHError(f"No Companies House record for company number {number}")
    d = _get(f"/company/{number}/officers", {"items_per_page": 100}, transport)
    saved = []
    for o in d.get("items") or []:
        if o.get("resigned_on") or o.get("officer_role") in ("corporate-secretary", "corporate-director"):
            continue
        name = _person_name(o.get("name") or "")
        title = (o.get("occupation") or o.get("officer_role") or "officer").replace("-", " ").title()
        if not name:
            continue
        url = f"https://find-and-update.company-information.service.gov.uk/company/{number}/officers"
        pid, created = upsert_person(con, {"full_name": name, "job_title": title, "officer_role": o.get("officer_role"),
                                           "appointed_on": o.get("appointed_on"), "evidence": url,
                                           "companies_house_number": number},
                                     company_id, mappings=approved_mappings(con), run_id=None, raw_response_id=None,
                                     tool="companies_house.officers", task_id=number, query=f"officers by {by}",
                                     source="companies_house")
        saved.append({"person_id": pid, "created": created, "name": name, "role": o.get("officer_role"),
                      "appointed_on": o.get("appointed_on")})
    SC.rescore_company(con, company_id)
    con.commit()
    return {"company": {"number": number, "name": prof.get("company_name"), "status": prof.get("company_status")},
            "officers": saved}
