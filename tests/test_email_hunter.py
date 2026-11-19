"""Agent 5 (Email Hunter) - offline. Website, DNS and Clay are fakes; all data is SYNTHETIC."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from chanakya.emailhunter import agent as E
from chanakya.emailhunter import checks as K
from chanakya.emailhunter.clay import PaidEmailGateway, PaidEmailLookup, SpendApproval, find_email_for
from chanakya.scout.agent import SpendBlocked
from tests.helpers import FakeSite, mx_none, mx_ok, seed_company, seed_person

D = "acme.example"


# ---------- unit ----------
def test_syntax_and_name_parts():
    assert K.valid_syntax("asha.t@acme.example") and not K.valid_syntax("bad@") and not K.valid_syntax("a b@x.io")
    assert K.name_parts("Dr. Asha  Testperson, CA") == ("asha", "testperson")
    assert K.name_parts("José Núñez") == ("jose", "nunez")
    assert K.apply_pattern("flast", "Asha Testperson", D) == "atestperson@acme.example"
    assert K.patterns_matching("asha.testperson", "Asha Testperson") == ["first.last"]


def test_extract_emails_handles_obfuscation_and_assets():
    html = 'Mail <a href="mailto:Asha.Testperson@acme.example">us</a>, ravi [at] acme [dot] example, logo@2x.png'
    assert K.extract_emails(html) == ["asha.testperson@acme.example", "ravi@acme.example"]


def test_infer_pattern_named_shape_prior():
    named = E.infer_pattern(D, [("ravi.example@acme.example", "u")], ["Ravi Example"])
    assert named["pattern"] == "first.last" and named["basis"] == "named" and named["confidence"] == 0.6
    shape = E.infer_pattern(D, [("john.doe@acme.example", "u"), ("info@acme.example", "u")], [])
    assert shape["basis"] == "shape" and shape["confidence"] == 0.3
    prior = E.infer_pattern(D, [("info@acme.example", "u")], [])
    assert prior["basis"] == "prior" and prior["confidence"] == 0.15


def test_mx_is_never_mailbox_proof():
    assert "does not prove" in K.MXResult("ok", ["mx"]).as_dict()["note"]


def test_mx_nslookup_parsing(monkeypatch):
    monkeypatch.setattr(K.shutil, "which", lambda x: "nslookup")
    monkeypatch.setattr(K.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="acme.example  MX preference = 10, mail exchanger = mx1.acme.example\n", stderr=""))
    assert K._mx_nslookup(D).hosts == ["mx1.acme.example"]
    monkeypatch.setattr(K.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="", stderr="*** Non-existent domain"))
    assert K._mx_nslookup(D).status == "none"


# ---------- classification ----------
def cand(name="Asha Testperson"):
    return E.EmailCandidate(1, name, 1, D)


def test_published_address_is_valid():
    f = E.classify(cand(), [("asha.testperson@acme.example", "https://acme.example/team")], E.infer_pattern(D, [], []),
                   mx_ok(D))
    assert (f.status, f.method, f.source_url, f.confidence) == ("valid", "website", "https://acme.example/team", 0.9)


def test_named_pattern_is_risky_not_valid():
    site = [("ravi.example@acme.example", "https://acme.example/contact")]
    f = E.classify(cand(), site, E.infer_pattern(D, site, ["Ravi Example"]), mx_ok(D))
    assert (f.email, f.status, f.pattern) == ("asha.testperson@acme.example", "risky", "first.last")


def test_prior_only_is_unverified_and_mx_unknown_downgrades():
    f = E.classify(cand(), [], E.infer_pattern(D, [], []), mx_ok(D))
    assert f.status == "unverified" and f.confidence == 0.15
    site = [("ravi.example@acme.example", "u")]
    f2 = E.classify(cand(), site, E.infer_pattern(D, site, ["Ravi Example"]), K.MXResult("unknown"))
    assert f2.status == "unverified" and f2.confidence == 0.42


def test_no_mx_is_not_found():
    f = E.classify(cand(), [("asha.testperson@acme.example", "u")], E.infer_pattern(D, [], []), mx_none(D))
    assert f.status == "not_found" and f.email is None


# ---------- agent ----------
def setup(con, approve=True, qualify=True):
    cid = seed_company(con, D, qualify=qualify)
    return seed_person(con, cid, approve=approve) if qualify else None


def test_gate_requires_approved_role_and_qualified_company(con):
    cid = seed_company(con, D, qualify=False)
    seed_person(con, cid, approve=False)
    assert E.select_candidates(con) == []
    seed_company(con, "b.example", "B Co")
    pid = seed_person(con, con.execute("SELECT id FROM companies WHERE domain='b.example'").fetchone()[0], "Bo Two")
    assert [c.person_id for c in E.select_candidates(con)] == [pid]


def test_dry_run_fetches_nothing(con):
    setup(con)
    site = FakeSite({})
    rep = E.run_email_hunter(con, fetcher=site, mx_lookup=mx_ok)
    assert rep.dry_run and site.calls == [] and rep.planned[0]["domain"] == D
    assert con.execute("SELECT COUNT(*) FROM email_records").fetchone()[0] == 0


def test_live_run_stores_record_pages_pattern_and_history(con):
    pid = setup(con)
    site = FakeSite({"https://acme.example/team": "<p>Asha Testperson - asha.testperson@acme.example</p>"})
    rep = E.run_email_hunter(con, allow_live=True, fetcher=site, mx_lookup=mx_ok)
    assert rep.counts["valid"] == 1 and len(site.calls) == len(K.CONTACT_PATHS)
    r = con.execute("SELECT * FROM email_records").fetchone()
    assert (r["person_id"], r["email"], r["review_status"]) == (pid, "asha.testperson@acme.example", "pending_review")
    assert json.loads(r["mx_json"])["status"] == "ok"
    assert con.execute("SELECT COUNT(*) FROM website_pages WHERE domain=?", (D,)).fetchone()[0] == len(K.CONTACT_PATHS)
    assert con.execute("SELECT COUNT(*) FROM domain_email_patterns").fetchone()[0] == 1
    assert E.history(con, r["id"])[0]["action"] == "create"
    assert E.select_candidates(con) == []  # open record -> not re-hunted


def test_fetch_errors_do_not_fail_run(con):
    setup(con)
    rep = E.run_email_hunter(con, allow_live=True, fetcher=lambda u: K.Page(u, None, "", False, "robots"),
                             mx_lookup=mx_ok)
    assert rep.counts["unverified"] == 1


def test_review_flow(con):
    pid = setup(con)
    E.run_email_hunter(con, allow_live=True, fetcher=FakeSite({}), mx_lookup=mx_ok)
    rid = con.execute("SELECT id FROM email_records").fetchone()[0]
    with pytest.raises(E.EmailReviewError):
        E.approve(con, rid, by="", reason="x")
    E.approve(con, rid, by="krisha", reason="checked on website")
    assert E.approved_email(con, pid)["email"] == "asha.testperson@acme.example"
    with pytest.raises(E.EmailReviewError):
        E.approve(con, rid, by="krisha", reason="again")
    nid = E.override(con, pid, "asha@acme.example", by="krisha", reason="she replied", evidence_url="https://x.example")
    assert E.get_record(con, rid)["review_status"] == "rejected"  # superseded, kept
    assert E.approved_email(con, pid)["id"] == nid
    assert [h["action"] for h in E.history(con, rid)] == ["create", "approve", "reject"]
    with pytest.raises(E.EmailReviewError):
        E.override(con, pid, "not-an-email", by="k", reason="r")


# ---------- paid Clay path ----------
class FakeClay:
    def __init__(self, enrich_payload, ctx_payload, workspace=1461095):
        self.calls, self.enrich, self.ctx, self.ws = [], enrich_payload, ctx_payload, workspace

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        payload = {"get-current-workspace": {"workspaceId": self.ws}, "get-credits-available": {"hasCredits": True},
                   "add-contact-data-points": self.enrich, "get-task-context": self.ctx}[name]
        return SimpleNamespace(is_error=False, structured_content=payload, content=[])


def test_spend_approval_required():
    with pytest.raises(SpendBlocked):
        SpendApproval("", "x")
    with pytest.raises(SpendBlocked):
        SpendApproval("k", "r", max_records=50)


def test_paid_gateway_cap_and_allowlist():
    gw = PaidEmailGateway(FakeClay({}, {}), SpendApproval("k", "r", 1))
    asyncio.run(gw.call("add-contact-data-points", {}))
    with pytest.raises(SpendBlocked):
        asyncio.run(gw.call("add-contact-data-points", {}))
    with pytest.raises(SpendBlocked):
        asyncio.run(gw.call("search-companies", {}))


def test_find_email_for_entity():
    p = {"entities": [{"entityId": "e1", "Email": "x@other.example"}, {"entityId": "e2", "work_email": "a@acme.example",
                                                                       "emailStatus": "valid"}]}
    assert find_email_for(p, "e2") == ("a@acme.example", "valid")
    assert find_email_for(p, "e9") == (None, None)


def test_paid_lookup_used_only_after_free_methods_fail(con):
    cid = seed_company(con, D)
    seed_person(con, cid, task_id="mcp-task_SYN", extra={"entityId": "ent-1"})
    fake = FakeClay({"status": "queued"}, {"entities": [{"entityId": "ent-1", "email": "asha.t@acme.example"}]})
    paid = PaidEmailLookup(lambda: fake, SpendApproval("krisha", "pilot", 1), "1461095")
    rep = E.run_email_hunter(con, allow_live=True, fetcher=FakeSite({}), mx_lookup=mx_ok, paid=paid)
    assert rep.paid_used == 1
    names = [c[0] for c in fake.calls]
    assert names == ["get-current-workspace", "get-credits-available", "add-contact-data-points", "get-task-context"]
    assert fake.calls[2][1] == {"taskId": "mcp-task_SYN", "dataPoints": [{"type": "Email"}], "entityIds": ["ent-1"]}
    r = con.execute("SELECT * FROM email_records").fetchone()
    assert (r["method"], r["status"], r["email"]) == ("clay", "unverified", "asha.t@acme.example")
    assert con.execute("SELECT COUNT(*) FROM raw_responses WHERE provider='clay-email'").fetchone()[0] == 2


def test_paid_lookup_skipped_when_website_found_address(con):
    cid = seed_company(con, D)
    seed_person(con, cid, task_id="mcp-task_SYN", extra={"entityId": "ent-1"})
    fake = FakeClay({}, {})
    paid = PaidEmailLookup(lambda: fake, SpendApproval("k", "r", 1), "1461095")
    site = FakeSite({"https://acme.example/": "asha.testperson@acme.example"})
    E.run_email_hunter(con, allow_live=True, fetcher=site, mx_lookup=mx_ok, paid=paid)
    assert fake.calls == []


def test_paid_lookup_refuses_wrong_workspace(con):
    cid = seed_company(con, D)
    seed_person(con, cid, task_id="t", extra={"entityId": "e"})
    paid = PaidEmailLookup(lambda: FakeClay({}, {}, workspace=999), SpendApproval("k", "r", 1), "1461095")
    with pytest.raises(SpendBlocked):
        E.run_email_hunter(con, allow_live=True, fetcher=FakeSite({}), mx_lookup=mx_ok, paid=paid)
    assert con.execute("SELECT status FROM runs WHERE agent='email_hunter'").fetchone()[0] == "failed"
