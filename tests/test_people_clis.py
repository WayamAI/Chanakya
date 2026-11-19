"""Offline tests for scripts/review_companies.py and scripts/find_people.py. Synthetic data, fake Clay client;
no network, no subprocess."""
import asyncio
import importlib.util
import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from chanakya.people.personas import add_human_mapping, make_evidence, review_mapping
from chanakya.storage.db import connect

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/search_contacts_synthetic.json").read_text("utf-8"))
P = "PRD-SYNTH"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    con = connect(path)
    con.execute("INSERT INTO products(id,name,verification,source_path,source_sha256,raw_json,imported_at) "
                "VALUES(?,?,?,?,?,?,?)", (P, "Synth", "unverified", "x.yaml", "0" * 64, "{}", "t"))
    con.execute("INSERT INTO icps(id,version,icp_json,source_shas,created_at,product_id) VALUES(5,1,'{}','{}','t',?)",
                (P,))
    for d in ("alpha.example", "beta.example"):
        con.execute("INSERT INTO companies(domain,name,data_json,first_seen,last_seen,icp_id,qualification_status,"
                    "qualification_source,assessment_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (d, d.split(".")[0].title(), "{}", "t", "t", 5, "needs_manual_review", "system",
                     json.dumps({"business_fit": {"status": "unknown"}, "review_reasons": ["synthetic"]})))
    mid = add_human_mapping(con, None, "IT operations leader", titles=["Head of IT Operations"], seniorities=["Head"],
                            reviewer="R", reason="r", evidence=make_evidence("synthetic"), product_id=P)
    review_mapping(con, mid, "approved", reviewer="R", reason="ok")
    pend = add_human_mapping(con, None, "Pending persona", titles=["CTO"], reviewer="R", reason="r",
                             evidence=make_evidence("synthetic"), product_id=P)
    con.commit()
    monkeypatch.setenv("CHANAKYA_DB_PATH", str(path))
    monkeypatch.setenv("CLAY_WORKSPACE_ID", "1461095")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("subprocess started"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("subprocess started"))
    real_connect = socket.socket.connect

    def guarded(self, addr, *a, **k):  # asyncio on Windows uses a loopback socket pair internally
        if not (isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1")):
            pytest.fail(f"network connect attempted: {addr}")
        return real_connect(self, addr, *a, **k)
    monkeypatch.setattr(socket.socket, "connect", guarded)
    return SimpleNamespace(path=path, approved=mid, pending=pend)


def q(path, sql, *a):
    c = connect(path)
    return c.execute(sql, a).fetchall()


# ---------------- review_companies ----------------
def test_show_lists_status_and_evidence_without_writing(db, capsys):
    rc = _load("review_companies")
    assert rc.main(["show", "alpha.example"]) == 0
    out = capsys.readouterr().out
    assert "needs_manual_review" in out and "business_fit" in out and P in out
    assert q(db.path, "SELECT COUNT(*) FROM human_reviews")[0][0] == 0


def test_qualify_and_disqualify_use_audit_trail(db, capsys):
    rc = _load("review_companies")
    assert rc.main(["qualify", "alpha.example", "--by", "Krisha", "--reason", "fits pilot"]) == 0
    assert "current: needs_manual_review" in capsys.readouterr().out  # shown BEFORE recording
    assert tuple(q(db.path, "SELECT qualification_status, qualification_source FROM companies WHERE domain=?",
                   "alpha.example")[0]) == ("qualified", "human")
    assert rc.main(["disqualify", "alpha.example", "--by", "Krisha", "--reason", "wrong segment"]) == 0
    hist = q(db.path, "SELECT status, reviewer, reason FROM human_reviews ORDER BY id")
    assert [tuple(h) for h in hist] == [("qualified", "Krisha", "fits pilot"), ("disqualified", "Krisha", "wrong segment")]
    assert q(db.path, "SELECT qualification_status FROM companies WHERE domain='alpha.example'")[0][0] == "disqualified"
    assert q(db.path, "SELECT qualification_status FROM companies WHERE domain='beta.example'")[0][0] == "needs_manual_review"


@pytest.mark.parametrize("args", [["qualify", "alpha.example", "--reason", "r"],
                                  ["qualify", "alpha.example", "--by", "K"],
                                  ["qualify", "alpha.example", "--by", "  ", "--reason", "r"],
                                  ["qualify", "alpha.example", "--by", "K", "--reason", "  "],
                                  ["qualify", "nope.example", "--by", "K", "--reason", "r"],
                                  ["qualify"]])
def test_review_companies_rejects_bad_input(db, args):
    assert _load("review_companies").main(args) == 2
    assert q(db.path, "SELECT COUNT(*) FROM human_reviews")[0][0] == 0
    assert {r[0] for r in q(db.path, "SELECT qualification_status FROM companies")} == {"needs_manual_review"}


# ---------------- find_people ----------------
class FakeClay:
    def __init__(self, workspace="1461095", credits=None, payload=FIXTURE):
        self.ws, self.payload, self.calls = workspace, payload, []
        self.credits = credits or {"hasWorkspaceCredits": True, "hasSalesRepCredits": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        body = {"get-current-workspace": {"workspaceId": self.ws},
                "get-credits-available": self.credits}.get(name, self.payload)
        return SimpleNamespace(structured_content=body, content=[], is_error=False)


def run(args, client=None):
    fp = _load("find_people")
    calls = []

    def factory():
        calls.append(1)
        return client or pytest.fail("Clay client created")
    return fp, asyncio.run(fp.main(args, client_factory=factory)), calls


def _qualify(domain="alpha.example"):
    assert _load("review_companies").main(["qualify", domain, "--by", "K", "--reason", "fits"]) == 0


def _query(capsys):
    return next(l[len("Query: "):] for l in capsys.readouterr().out.splitlines() if l.startswith("Query: "))


def test_dry_run_is_default_and_makes_no_call_and_no_write(db, capsys):
    _qualify()
    before = db.path.read_bytes()
    fp, code, calls = run(["--domain", "alpha.example"])
    out = capsys.readouterr().out
    assert code == 0 and calls == [] and "DRY RUN" in out and "NOT validated" in out
    assert 'job_title is_similar_to ("Head of IT Operations")' in out and "limit 1 by clay_company_id" in out
    assert "CTO" not in out  # pending mapping never used
    assert '"companyIdentifiers": ["alpha.example"]' in out
    assert db.path.read_bytes() == before  # read-only: not even a run row


def test_confirm_query_without_live_still_dry(db, capsys):
    _qualify()
    run(["--domain", "alpha.example"]); query = _query(capsys)
    _, code, calls = run(["--domain", "alpha.example", "--confirm-query", query])
    assert code == 0 and calls == []


@pytest.mark.parametrize("args", [["--domain", "nope.example"], ["--domain", "beta.example"],
                                  ["--domain", "alpha.example", "--mapping", "999"]])
def test_blocked_inputs(db, args, capsys):
    _qualify()  # alpha only; beta stays unqualified
    _, code, calls = run(args + ["--live", "--confirm-query", "x"])
    assert code == 2 and calls == []


def test_pending_mapping_cannot_be_selected(db):
    _qualify()
    _, code, calls = run(["--domain", "alpha.example", "--mapping", str(db.pending), "--live", "--confirm-query", "x"])
    assert code == 2 and calls == []


def test_no_approved_mapping_blocks(db):
    _qualify()
    c = connect(db.path)
    review_mapping(c, db.approved, "rejected", reviewer="R", reason="test")
    _, code, calls = run(["--domain", "alpha.example"])
    assert code == 2 and calls == []


def test_mismatched_confirmation_refused(db, capsys):
    _qualify()
    run(["--domain", "alpha.example"]); query = _query(capsys)
    for bad in (None, query + " ", query.replace("limit 1", "limit 3")):
        args = ["--domain", "alpha.example", "--live"] + (["--confirm-query", bad] if bad is not None else [])
        _, code, calls = run(args)
        assert code == 3 and calls == []
    assert q(db.path, "SELECT COUNT(*) FROM runs")[0][0] == 0


@pytest.mark.parametrize("clay", [FakeClay(workspace="999"),
                                  FakeClay(credits={"hasWorkspaceCredits": False, "hasSalesRepCredits": True}),
                                  FakeClay(credits={"hasWorkspaceCredits": True, "hasSalesRepCredits": True,
                                                    "exhaustedMessage": "out of credits"})])
def test_wrong_workspace_or_no_credits_stops_before_search(db, clay, capsys):
    _qualify()
    run(["--domain", "alpha.example"]); query = _query(capsys)
    _, code, calls = run(["--domain", "alpha.example", "--live", "--confirm-query", query], clay)
    assert code == 4 and calls == [1]
    assert "search-contacts" not in [c[0] for c in clay.calls]
    assert q(db.path, "SELECT status FROM runs")[0][0] == "error"
    assert q(db.path, "SELECT COUNT(*) FROM people")[0][0] == 0


def test_live_with_fake_client_caps_and_keeps_provenance(db, capsys):
    _qualify(); _qualify("beta.example")
    run(["--domain", "alpha.example"]); query = _query(capsys)
    clay = FakeClay()
    fp, code, _ = run(["--domain", "alpha.example", "--live", "--confirm-query", query], clay)
    out = capsys.readouterr().out
    assert code == 0 and "NOT validated" in out
    assert [c[0] for c in clay.calls] == ["get-current-workspace", "get-credits-available", "search-contacts"]
    args = clay.calls[2][1]
    assert args["companyIdentifiers"] == ["alpha.example"] and args["dslQuery"] == query  # ONE company
    assert "limit 1 by clay_company_id" in args["dslQuery"]
    assert {c[0] for c in clay.calls} <= fp.policy.PEOPLE_FINDER
    assert q(db.path, "SELECT COUNT(*) FROM people")[0][0] == 1  # ONE person even though fixture has more
    raw = q(db.path, "SELECT run_id, tool, response_json FROM raw_responses")
    assert len(raw) == 1 and raw[0]["tool"] == "search-contacts" and json.loads(raw[0]["response_json"]) == FIXTURE
    src = q(db.path, "SELECT run_id, raw_response_id FROM person_sources")
    assert src[0]["run_id"] == raw[0]["run_id"] and src[0]["raw_response_id"]
    assert q(db.path, "SELECT verification FROM people")[0][0] == "unverified"
    assert q(db.path, "SELECT review_status FROM person_roles")[0][0] == "needs_manual_review"


def test_live_caps_are_constants():
    fp = _load("find_people")
    assert (fp.LIVE_MAX_COMPANIES, fp.LIVE_MAX_PEOPLE) == (1, 1)
    src = (ROOT / "scripts/find_people.py").read_text("utf-8")
    assert "add-contact-data-points" not in src and "linkedin_harness" not in src.lower()
