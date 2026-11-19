"""People Finder role review (offline). Synthetic data only."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from chanakya.people import personas as P
from chanakya.people import role_review as RR
from chanakya.resolver.agent import select_candidates
from chanakya.storage.db import connect, now


def seed(con, qualified=True):
    t = now()
    cid = con.execute("INSERT INTO companies(domain,name,data_json,first_seen,last_seen,qualification_status,"
                      "qualification_source) VALUES(?,?,?,?,?,?,?)",
                      ("example.test", "Example Co", "{}", t, t,
                       "qualified" if qualified else "needs_manual_review", "human" if qualified else "system")).lastrowid
    pid = con.execute("INSERT INTO people(dedupe_key,dedupe_basis,full_name,first_seen,last_seen) VALUES(?,?,?,?,?)",
                      ("k", "name_and_company", "Asha Testperson", t, t)).lastrowid
    rid = con.execute("INSERT INTO person_roles(person_id,company_id,title,is_current,role_match_json,first_seen,"
                      "last_seen) VALUES(?,?,?,?,?,?,?)",
                      (pid, cid, "Head of Security", 1, json.dumps({"status": "matches_approved_role"}), t, t)).lastrowid
    con.execute("INSERT INTO person_sources(person_id,company_id,source,tool,raw_json,fetched_at) VALUES(?,?,?,?,?,?)",
                (pid, cid, "clay", "search-contacts", '{"synthetic": true}', t))
    con.commit()
    return rid


def test_new_roles_start_pending_and_list(con):
    rid = seed(con)
    roles = RR.list_roles(con, RR.PENDING)
    assert [r["id"] for r in roles] == [rid] and roles[0]["role_match_status"] == "matches_approved_role"
    assert RR.list_roles(con, RR.APPROVED) == []
    d = RR.get_role(con, rid)
    assert d["sources"][0]["tool"] == "search-contacts" and d["review_status"] == RR.PENDING


def test_approve_unlocks_agent4_and_records_history(con):
    rid = seed(con)
    assert select_candidates(con) == []
    RR.review_role(con, rid, RR.APPROVED, reviewer="Krisha", reason="CISO-equivalent at target account",
                   evidence=[{"source": "human_review", "text": "title matches persona"}])
    assert [c.person_role_id for c in select_candidates(con)] == [rid]
    h = RR.history(con, rid)
    assert len(h) == 1 and h[0]["action"] == "approve" and h[0]["from_status"] == RR.PENDING
    assert h[0]["snapshot"]["review_status"] == RR.PENDING  # snapshot is BEFORE the change
    assert h[0]["evidence"][0]["text"] == "title matches persona"


def test_reject_and_reopen_keep_full_trail(con):
    rid = seed(con)
    RR.review_role(con, rid, RR.APPROVED, reviewer="K", reason="ok")
    RR.review_role(con, rid, RR.REJECTED, reviewer="K", reason="left the company")
    assert select_candidates(con) == []
    RR.review_role(con, rid, RR.PENDING, reviewer="K", reason="re-check")
    assert [h["action"] for h in RR.history(con, rid)] == ["approve", "reject", "reopen"]
    assert [h["snapshot"]["review_status"] for h in RR.history(con, rid)] == [RR.PENDING, RR.APPROVED, RR.REJECTED]


def test_approval_requires_human_qualified_company(con):
    rid = seed(con, qualified=False)
    with pytest.raises(RR.RoleReviewError, match="not human-qualified"):
        RR.review_role(con, rid, RR.APPROVED, reviewer="K", reason="r")
    RR.review_role(con, rid, RR.REJECTED, reviewer="K", reason="not a target")  # rejecting is always allowed
    assert RR.history(con, rid)[0]["action"] == "reject"


@pytest.mark.parametrize("by,reason", [("", "r"), ("K", ""), ("  ", "  ")])
def test_reviewer_and_reason_required(con, by, reason):
    rid = seed(con)
    with pytest.raises(RR.RoleReviewError):
        RR.review_role(con, rid, RR.APPROVED, reviewer=by, reason=reason)
    assert RR.history(con, rid) == []


def test_bad_inputs(con):
    rid = seed(con)
    with pytest.raises(RR.RoleReviewError):
        RR.review_role(con, rid, "maybe", reviewer="K", reason="r")
    with pytest.raises(RR.RoleReviewError):
        RR.review_role(con, rid, RR.PENDING, reviewer="K", reason="r")  # already pending
    with pytest.raises(RR.RoleReviewError):
        RR.review_role(con, 999, RR.APPROVED, reviewer="K", reason="r")


def test_persona_history_untouched(con):
    rid = seed(con)
    m = P.add_human_mapping(con, None, "Security lead", titles=["Head of Security"], seniorities=[],
                            reviewer="K", reason="r", evidence=P.make_evidence("t"))
    before = con.execute("SELECT COUNT(*) FROM persona_mapping_history").fetchone()[0]
    RR.review_role(con, rid, RR.APPROVED, reviewer="K", reason="r")
    assert con.execute("SELECT COUNT(*) FROM persona_mapping_history").fetchone()[0] == before
    assert m is not None


def test_cli_end_to_end(tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    rid = seed(connect(db))
    monkeypatch.setenv("CHANAKYA_DB_PATH", str(db))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("subprocess started"))
    spec = importlib.util.spec_from_file_location("rr", Path(__file__).parents[1] / "scripts" / "review_roles.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.main(["approve", str(rid), "--by", "K"]) == 2  # reason missing
    assert m.main(["approve", str(rid), "--by", "K", "--reason", "fits", "--evidence", "title"]) == 0
    capsys.readouterr()
    assert m.main(["history", str(rid)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["evidence"][0]["text"] == "title"
