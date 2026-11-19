"""Agent 4 (LinkedIn Resolver) - offline tests. The harness subprocess is replaced by a fake runner that replays
SYNTHETIC JSON fixtures; nothing touches LinkedIn, Clay or Ollama."""
import json
import subprocess
from pathlib import Path

import pytest

from chanakya.resolver import review as R
from chanakya.resolver.agent import ResolverBlocked, interpret, run_resolver, select_candidates, single_run_lock
from chanakya.resolver.harness_client import (CompletedRun, HarnessClient, HarnessError, _child_env, classify_error,
                                              harness_version, parse_output)
from chanakya.storage.db import now

FIX = Path(__file__).parent / "fixtures" / "harness"


def fx(name: str) -> dict:
    d = json.loads((FIX / name).read_text(encoding="utf-8"))
    d.pop("_note", None)
    return d


class FakeRunner:
    """Replays fixtures: `status` -> status_fixture; `resolve <name>` -> by_name[name]. Records every call."""

    def __init__(self, by_name: dict, status_fixture="status_logged_in.json"):
        self.by_name, self.status_fixture, self.calls, self.active = by_name, status_fixture, [], 0

    def __call__(self, argv, cwd, timeout):
        self.active += 1
        assert self.active == 1, "harness calls overlapped"
        try:
            self.calls.append(argv)
            cmd = argv[3]
            obj = fx(self.status_fixture) if cmd == "status" else self.by_name[argv[4]]
            if isinstance(obj, Exception):
                raise obj
            obj = fx(obj) if isinstance(obj, str) else obj
            return CompletedRun(0 if obj["ok"] else 1, "-> https://www.linkedin.com/...\nJSON: " + json.dumps(obj), "log")
        finally:
            self.active -= 1


@pytest.fixture
def harness_dir(tmp_path):
    d = tmp_path / "harness"
    d.mkdir()
    (d / "linkedin_harness.py").write_text("# stand-in for the real harness (never executed)\n", encoding="utf-8")
    return d


def client(harness_dir, runner):
    return HarnessClient(harness_dir, runner=runner)


def seed(con, people=(("Asha Testperson", "Head of Security", "approved"),
                      ("Ravi Samplename", "CTO", "approved"),
                      ("Meera Fictional", None, "approved"))):
    t = now()
    cid = con.execute("INSERT INTO companies(domain,name,data_json,first_seen,last_seen) VALUES(?,?,?,?,?)",
                      ("example.test", "Example Analytics Pvt Ltd", "{}", t, t)).lastrowid
    ids = []
    for i, (name, title, status) in enumerate(people):
        pid = con.execute("INSERT INTO people(dedupe_key,dedupe_basis,full_name,first_seen,last_seen) "
                          "VALUES(?,?,?,?,?)", (f"k{i}", "name_and_company", name, t, t)).lastrowid
        con.execute("INSERT INTO person_roles(person_id,company_id,title,is_current,role_match_json,review_status,"
                    "first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?)", (pid, cid, title, 1, "{}", status, t, t))
        ids.append(pid)
    con.commit()
    return ids


ALL = {"Asha Testperson": "resolve_found_high.json", "Ravi Samplename": "resolve_manual_queue.json",
       "Meera Fictional": "resolve_empty.json"}


# ---------------------------------------------------------------- harness client

def test_parse_output_uses_last_json_line():
    out = 'JSON: {"ok": false}\nnoise\nJSON: {"ok": true, "data": {"a": 1}}\n'
    assert parse_output(out) == {"ok": True, "data": {"a": 1}}


@pytest.mark.parametrize("stdout", ["", "no json here", "JSON: {broken", 'JSON: ["ok"]'])
def test_parse_output_rejects_bad_output(stdout):
    with pytest.raises(HarnessError) as e:
        parse_output(stdout)
    assert e.value.kind == "bad_output"


@pytest.mark.parametrize("cmd", ["run", "chat", "people", "profile", "feed", "login", "serve"])
def test_only_read_only_llm_free_commands_allowed(harness_dir, cmd):
    with pytest.raises(HarnessError) as e:
        client(harness_dir, FakeRunner({})).build_argv(cmd, "x")
    assert e.value.kind == "command_not_allowed"


def test_resolve_argv_shape(harness_dir):
    argv = client(harness_dir, FakeRunner({})).resolve_argv("  Asha Testperson ", "Example Co", "")
    assert argv == ["uv", "run", "linkedin_harness.py", "resolve", "Asha Testperson", "--company", "Example Co"]


def test_classify_errors():
    assert classify_error(fx("error_not_logged_in.json")["error"]) == "not_logged_in"
    assert classify_error(fx("error_page_limit.json")["error"]) == "page_limit"
    assert classify_error("CAPTCHA shown") == "security_check"
    assert classify_error("boom") == "harness_error"


def test_child_env_drops_azure(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "x")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "y")
    env = _child_env()
    assert not any(k.startswith("AZURE_OPENAI_") for k in env)


def test_harness_version_git_and_hash(harness_dir):
    assert harness_version(harness_dir).startswith("sha256:")
    g = harness_dir / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "refs" / "heads" / "main").write_text("404dd7e5f1abc\n")
    assert harness_version(harness_dir) == "git:404dd7e5f1abc"


def test_missing_harness_is_reported(tmp_path):
    c = HarnessClient(tmp_path / "nope", runner=FakeRunner({}))
    with pytest.raises(HarnessError) as e:
        c.status()
    assert e.value.kind == "not_installed"


def test_from_env_requires_dir(monkeypatch):
    monkeypatch.delenv("LINKEDIN_HARNESS_DIR", raising=False)
    with pytest.raises(HarnessError) as e:
        HarnessClient.from_env()
    assert e.value.kind == "not_configured"


def test_default_runner_timeout(monkeypatch, harness_dir):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="uv", timeout=1)
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(HarnessError) as e:
        HarnessClient(harness_dir, timeout_s=1, automation_allowed=True).status()  # the human-enabled path
    assert e.value.kind == "timeout"


def test_linkedin_policy_blocks_real_subprocess_by_default(monkeypatch, harness_dir):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("LinkedIn reached while policy is disabled"))
    with pytest.raises(HarnessError) as e:
        HarnessClient(harness_dir).status()
    assert e.value.kind == "policy_disabled"


# ---------------------------------------------------------------- selection + dry run

def test_dry_run_is_default_and_starts_no_process(con, harness_dir, monkeypatch):
    seed(con)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("subprocess started in dry run"))
    runner = FakeRunner(ALL)
    rep = run_resolver(client(harness_dir, runner), con)
    assert rep.dry_run and runner.calls == [] and len(rep.planned) == 3
    assert rep.planned[0]["argv"][3:] == ["resolve", "Asha Testperson", "--company", "Example Analytics Pvt Ltd",
                                          "--title", "Head of Security"]
    assert con.execute("SELECT COUNT(*) FROM linkedin_resolutions").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_selection_requires_approved_role_and_skips_existing_urls(con):
    ids = seed(con, (("A Person", "CTO", "approved"), ("B Person", "CTO", "needs_manual_review"),
                     ("C Person", "CTO", "approved")))
    con.execute("UPDATE people SET linkedin_url='https://www.linkedin.com/in/synthetic-person-005' WHERE id=?", (ids[2],))
    assert [c.full_name for c in select_candidates(con)] == ["A Person"]
    con.execute("UPDATE person_roles SET review_status='rejected' WHERE person_id=?", (ids[0],))
    assert select_candidates(con) == []


def test_no_unreviewed_override_exists():
    import inspect
    from chanakya.resolver import agent
    assert "role_statuses" not in inspect.signature(agent.select_candidates).parameters
    assert "role_statuses" not in inspect.signature(agent.run_resolver).parameters
    cli = (Path(__file__).parents[1] / "scripts" / "resolve_linkedin.py").read_text(encoding="utf-8")
    assert "unreviewed" not in cli


def test_limit_bounds(con, harness_dir):
    with pytest.raises(ValueError):
        run_resolver(client(harness_dir, FakeRunner({})), con, limit=0)
    with pytest.raises(ValueError):
        run_resolver(client(harness_dir, FakeRunner({})), con, limit=26)


# ---------------------------------------------------------------- live path (fake runner)

def test_live_run_stores_everything_unverified(con, harness_dir, tmp_path):
    seed(con)
    runner = FakeRunner(ALL)
    rep = run_resolver(client(harness_dir, runner), con, allow_live=True, lock_path=tmp_path / "l.lock")
    assert rep.counts == {"found": 1, "manual_queue": 2, "error": 0}
    assert [a[3] for a in runner.calls] == ["status", "resolve", "resolve", "resolve"]
    rows = con.execute("SELECT * FROM linkedin_resolutions ORDER BY id").fetchall()
    assert {r["verification"] for r in rows} == {"unverified"}
    assert {r["review_status"] for r in rows} == {"pending_review"}
    hi = rows[0]
    assert hi["status"] == "found" and hi["confidence"] == "high" and hi["score"] == 1.0
    assert hi["profile_url"] == "https://www.linkedin.com/in/synthetic-person-001"
    assert hi["harness_version"].startswith("sha256:") and hi["harness_saved_path"].startswith("outputs/")
    assert json.loads(hi["harness_json"])["ok"] is True and json.loads(hi["searches_json"])
    assert json.loads(hi["harness_argv_json"])[3] == "resolve" and hi["run_id"] == rep.run_id
    assert rows[1]["status"] == "manual_queue" and rows[1]["profile_url"] is None
    assert rows[2]["status"] == "manual_queue" and rows[2]["best_json"] is None  # empty results != 'not on LinkedIn'
    # nothing is copied to people before a human approves
    assert con.execute("SELECT COUNT(*) FROM people WHERE linkedin_url IS NOT NULL").fetchone()[0] == 0
    assert con.execute("SELECT status FROM runs WHERE id=?", (rep.run_id,)).fetchone()[0] == "ok"
    # re-run: open resolutions are not resolved again
    assert run_resolver(client(harness_dir, FakeRunner(ALL)), con, allow_live=True,
                        lock_path=tmp_path / "l.lock").planned == []


def test_live_run_respects_limit(con, harness_dir, tmp_path):
    seed(con)
    runner = FakeRunner(ALL)
    rep = run_resolver(client(harness_dir, runner), con, allow_live=True, limit=1, lock_path=tmp_path / "l")
    assert len(rep.resolution_ids) == 1 and len(runner.calls) == 2


def test_logged_out_blocks_before_any_resolve(con, harness_dir, tmp_path):
    seed(con)
    runner = FakeRunner(ALL, status_fixture="status_logged_out.json")
    with pytest.raises(ResolverBlocked):
        run_resolver(client(harness_dir, runner), con, allow_live=True, lock_path=tmp_path / "l")
    assert [a[3] for a in runner.calls] == ["status"]
    assert not (tmp_path / "l").exists()  # lock released


@pytest.mark.parametrize("fixture,kind", [("error_not_logged_in.json", "not_logged_in"),
                                          ("error_page_limit.json", "page_limit")])
def test_session_errors_stop_the_run(con, harness_dir, tmp_path, fixture, kind):
    seed(con)
    runner = FakeRunner({**ALL, "Asha Testperson": fixture})
    rep = run_resolver(client(harness_dir, runner), con, allow_live=True, lock_path=tmp_path / "l")
    assert rep.stopped == kind and rep.counts["error"] == 1 and len(rep.resolution_ids) == 1
    r = con.execute("SELECT * FROM linkedin_resolutions").fetchone()
    assert r["status"] == "error" and r["error_kind"] == kind
    assert con.execute("SELECT status FROM runs").fetchone()[0] == "stopped"
    # error rows do not block a retry
    assert len(select_candidates(con)) == 3


def test_timeout_is_recorded_and_run_continues(con, harness_dir, tmp_path):
    seed(con)
    runner = FakeRunner({**ALL, "Asha Testperson": HarnessError("timeout", "too slow")})
    rep = run_resolver(client(harness_dir, runner), con, allow_live=True, lock_path=tmp_path / "l")
    assert rep.counts == {"found": 0, "manual_queue": 2, "error": 1} and rep.stopped is None
    r = con.execute("SELECT * FROM linkedin_resolutions ORDER BY id").fetchone()
    assert r["error_kind"] == "timeout" and r["harness_version"].startswith("sha256:")


def test_lock_prevents_parallel_runs(con, harness_dir, tmp_path):
    seed(con)
    lock = tmp_path / "l"
    with single_run_lock(lock):
        with pytest.raises(ResolverBlocked):
            run_resolver(client(harness_dir, FakeRunner(ALL)), con, allow_live=True, lock_path=lock)
    assert not lock.exists()


def test_interpret_never_trusts_unexpected_shapes():
    from chanakya.resolver.harness_client import HarnessResult
    base = dict(saved=None, error=None, error_kind=None, raw={}, argv=[], harness_version="v")
    weird = HarnessResult(ok=True, data={"status": "found", "confidence": "certain",
                                         "profile_url": "https://evil.example/in/x"}, **base)
    assert interpret(weird)["status"] == "manual_queue"
    no_url = HarnessResult(ok=True, data={"status": "found", "confidence": "high", "profile_url": None}, **base)
    assert interpret(no_url)["status"] == "manual_queue"


# ---------------------------------------------------------------- human review

def _live(con, harness_dir, tmp_path):
    seed(con)
    run_resolver(client(harness_dir, FakeRunner(ALL)), con, allow_live=True, lock_path=tmp_path / "l")
    return [r["id"] for r in con.execute("SELECT id FROM linkedin_resolutions ORDER BY id")]


def test_approve_copies_url_and_logs_history(con, harness_dir, tmp_path):
    hi, *_ = _live(con, harness_dir, tmp_path)
    url = R.approve(con, hi, by="Krisha", reason="headline and company match")
    r = R.get(con, hi)
    assert r["review_status"] == "approved" and r["verification"] == "human_reviewed" and r["approved_url"] == url
    assert con.execute("SELECT linkedin_url FROM people WHERE id=?", (r["person_id"],)).fetchone()[0] == url
    h = R.history(con, hi)
    assert len(h) == 1 and h[0]["action"] == "approve" and h[0]["reviewer"] == "Krisha"
    with pytest.raises(R.ReviewError):
        R.approve(con, hi, by="Krisha", reason="again")


def test_approve_alternative_only_from_stored_candidates(con, harness_dir, tmp_path):
    _, mq, empty = _live(con, harness_dir, tmp_path)
    with pytest.raises(R.ReviewError):
        R.approve(con, mq, by="K", reason="r")  # manual_queue has no default URL
    with pytest.raises(R.ReviewError):
        R.approve(con, mq, by="K", reason="r", url="https://www.linkedin.com/in/synthetic-person-098")
    assert R.approve(con, mq, by="K", reason="alt matches", url="https://www.linkedin.com/in/synthetic-person-004/") \
        == "https://www.linkedin.com/in/synthetic-person-004"
    with pytest.raises(R.ReviewError):
        R.approve(con, empty, by="K", reason="r", url="https://www.linkedin.com/in/synthetic-person-099")


def test_review_requires_reviewer_and_reason(con, harness_dir, tmp_path):
    hi, *_ = _live(con, harness_dir, tmp_path)
    with pytest.raises(R.ReviewError):
        R.approve(con, hi, by="", reason="x")
    with pytest.raises(R.ReviewError):
        R.reject(con, hi, by="K", reason=" ")


def test_reject_keeps_people_untouched(con, harness_dir, tmp_path):
    hi, *_ = _live(con, harness_dir, tmp_path)
    R.reject(con, hi, by="K", reason="wrong person")
    assert R.get(con, hi)["review_status"] == "rejected"
    assert con.execute("SELECT COUNT(*) FROM people WHERE linkedin_url IS NOT NULL").fetchone()[0] == 0
    assert R.history(con, hi)[0]["to_status"] == "rejected"


def test_resolver_has_no_browser_code():
    src = Path(__file__).parents[1] / "src" / "chanakya" / "resolver"
    text = "\n".join(p.read_text(encoding="utf-8") for p in src.glob("*.py"))
    for banned in ("browser_use", "playwright", "selenium", "import linkedin_harness"):
        assert banned not in text


def test_cli_dry_run(tmp_path, monkeypatch, capsys):
    import importlib.util
    db = tmp_path / "t.db"
    from chanakya.storage.db import connect
    seed(connect(db))
    monkeypatch.setenv("CHANAKYA_DB_PATH", str(db))
    monkeypatch.delenv("LINKEDIN_HARNESS_DIR", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("subprocess started"))
    spec = importlib.util.spec_from_file_location("rl", Path(__file__).parents[1] / "scripts" / "resolve_linkedin.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.main(["run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and len(out["planned"]) == 3
    assert m.main(["run", "--live"]) == 2  # no harness configured -> refused, nothing started
