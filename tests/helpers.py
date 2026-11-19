"""Shared offline test helpers. Everything here is SYNTHETIC (fake people, .example domains); nothing touches
LinkedIn, Clay, Ollama, DNS or the web.

Harness payloads are shaped like the real harness functions (linkedin_harness.py at af334b8): op_search ->
{kind, query, search_url, results:[{url,title,lines,new}], new_count}; op_profile -> {url,name,top,sections,
recent_post_urls}; op_company -> {url,title,text}; op_research -> see research/agent.py docstring;
status -> {url, logged_in}. Each is wrapped like the CLI's last stdout line: JSON: {"ok","data","saved"}.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from chanakya.emailhunter.checks import MXResult, Page
from chanakya.people.finder import upsert_person
from chanakya.people.role_review import review_role
from chanakya.resolver.harness_client import CompletedRun, HarnessClient
from chanakya.scout.agent import upsert_company
from chanakya.scout.qualification import record_human_decision
from chanakya.storage.db import now

PROFILE_URL = "https://www.linkedin.com/in/synthetic-asha-001"
POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000001/"


def seed_product(con, pid="PRD-TEST", name="Test Accelerator",
                 summary="Automates audit evidence collection for finance teams and cuts reconciliation time."):
    con.execute("INSERT INTO products(id,name,status,summary,needs_json,buyer_roles_json,verification,source_path,"
                "source_sha256,raw_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (pid, name, "active", summary, json.dumps([{"quote": "cuts reconciliation time", "kind": "capability"}]),
                 json.dumps(["CFO"]), "unverified", "test.yaml", "x", "{}", now()))
    icp = con.execute("INSERT INTO icps(version,icp_json,source_shas,created_at,content_sha256,product_id) "
                      "VALUES(1,'{}','{}',?,?,?)", (now(), "sha-" + pid, pid)).lastrowid
    con.execute("INSERT INTO persona_role_mappings(icp_id,product_id,persona,persona_source,titles_json,seniorities_json,"
                "status,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (icp, pid, "Finance leader", "human", json.dumps(["Chief Financial Officer", "Head of Finance"]),
                 json.dumps(["C-suite"]), "approved", "[]", now()))
    con.commit()
    return icp


def seed_company(con, domain="acme.example", name="Acme Analytics Pvt Ltd", icp_id=None, qualify=True):
    cid, _ = upsert_company(con, {"name": name, "domain": domain}, icp_id=icp_id, fit={"score": 1}, tool="t",
                            task_id=None, query="q", provider="clay", domain=domain)
    con.commit()
    if qualify:
        record_human_decision(con, domain, "qualified", "tester", "fits ICP")
        con.commit()
    return cid


def seed_person(con, cid, name="Asha Testperson", title="Chief Financial Officer", linkedin=None, approve=True,
                task_id=None, extra=None):
    rec = {"full_name": name, "job_title": title, **({"linkedin_url": linkedin} if linkedin else {}), **(extra or {})}
    pid, _ = upsert_person(con, rec, cid, mappings=[], run_id=None, raw_response_id=None, tool="search-contacts",
                           task_id=task_id, query="q")
    con.commit()
    role = con.execute("SELECT id FROM person_roles WHERE person_id=?", (pid,)).fetchone()[0]
    if approve:
        review_role(con, role, "approved", reviewer="tester", reason="right persona")
    return pid


def ok(data, saved="outputs/x.json"):
    return {"ok": True, "data": data, "saved": saved}


def research_data(url=PROFILE_URL):
    return {"profile_url": url + "/", "name": "Asha Testperson",
            "headline_block": ["Asha Testperson", "Chief Financial Officer at Acme Analytics", "Pune, India"],
            "about": ["Leading finance transformation and audit readiness across 4 business units."],
            "education": ["Example University"], "pages_loaded": 4, "clicks": ["experience", "activity"],
            "experience": ["Chief Financial Officer", "Acme Analytics Pvt Ltd · Full-time", "Jan 2021 - Present",
                           "Responsible for statutory audit, treasury and month-end close."],
            "window_days": 90, "in_window": {"posts": 1, "comments": 0},
            "posts": [{"type": "post", "age": "2w", "approx_date": "2026-10-20", "exact_date": "2026-10-21T09:00:00",
                       "text": "Month-end close still takes us 9 days. Reconciliation is our biggest bottleneck.",
                       "reactions": 12, "comments": 3, "reposts": 0, "url": POST_URL, "source_page": url + "/",
                       "in_window": True}],
            "comments": [], "counts": {"posts": 1, "comments": 0}, "evidence_screenshots": [],
            "collected_at": "2026-11-06T10:00:00", "note": "Read only."}


def people_search_data():
    return {"kind": "people", "query": "Head of Finance", "search_url": "https://www.linkedin.com/search/results/people/",
            "results": [
                {"url": "https://www.linkedin.com/in/synthetic-ravi-002/", "title": "Ravi Example",
                 "lines": ["Head of Finance at Beta Foods", "Mumbai"], "new": True},
                {"url": "https://www.linkedin.com/in/synthetic-meera-003/", "title": "Meera Sample",
                 "lines": ["Graphic designer", "Delhi"], "new": True}], "new_count": 2}


def profile_data():
    return {"url": "https://www.linkedin.com/in/synthetic-ravi-002/", "name": "Ravi Example",
            "top": ["Ravi Example", "Head of Finance at Beta Foods"],
            "sections": {"Ravi Example": ["Head of Finance at Beta Foods"],
                         "Experience": ["Head of Finance", "Beta Foods Pvt Ltd · Full-time", "2022 - Present"]},
            "recent_post_urls": []}


def company_search_data():
    return {"kind": "companies", "query": "Beta Foods Pvt Ltd", "search_url": "x", "new_count": 1,
            "results": [{"url": "https://www.linkedin.com/company/beta-foods/", "title": "Beta Foods",
                         "lines": ["Food & Beverages", "Mumbai"], "new": True}]}


def company_about_data():
    return {"url": "https://www.linkedin.com/company/beta-foods/about/", "title": "Beta Foods | About",
            "text": "Overview\nBeta Foods makes packaged snacks.\nWebsite\nhttps://www.betafoods.example\nIndustry\nFood"}


class FakeHarness:
    """Runner replaying responses per command; `responses[cmd]` is a dict, a list (consumed in order) or callable."""

    def __init__(self, responses: dict, logged_in=True):
        self.responses = {"status": ok({"url": "https://www.linkedin.com/feed/", "logged_in": logged_in}),
                          **responses}
        self.calls: list[list[str]] = []
        self.active = 0

    def __call__(self, argv, cwd, timeout):
        self.active += 1
        assert self.active == 1, "harness calls overlapped"
        try:
            self.calls.append(argv)
            r = self.responses[argv[3]]
            if callable(r):
                r = r(argv)
            elif isinstance(r, list):
                r = r.pop(0)
            if isinstance(r, Exception):
                raise r
            return CompletedRun(0 if r["ok"] else 1, "log line\nJSON: " + json.dumps(r), "")
        finally:
            self.active -= 1


def harness(tmp_path, runner, **kw) -> HarnessClient:
    (tmp_path / "linkedin_harness.py").write_text("# fake", encoding="utf-8")
    return HarnessClient(tmp_path, runner=runner, **kw)


class FakeSite:
    def __init__(self, pages: dict[str, str]):
        self.pages, self.calls = pages, []

    def __call__(self, url):
        self.calls.append(url)
        return Page(url, 200 if url in self.pages else 404, self.pages.get(url, ""), True)


def mx_ok(domain):
    return MXResult("ok", ["mx." + domain], "fake")


def mx_none(domain):
    return MXResult("none", [], "fake")


class SeqLLM:
    """Fake LLM: returns canned JSON strings/dicts in order; records messages."""

    def __init__(self, *responses):
        self.r, self.calls = list(responses), []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        c = self.r.pop(0)
        c = c if isinstance(c, str) else json.dumps(c)
        return SimpleNamespace(content=c, message={"role": "assistant", "content": c}, model="qwen-test",
                               prompt_tokens=10, output_tokens=5)
