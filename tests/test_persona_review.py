"""Persona Review CLI and workflow. Offline; synthetic products in an in-memory DB."""
import importlib.util
import json
import pathlib

import pytest

from chanakya.catalog.models import ICP
from chanakya.people import personas as P
from chanakya.people.finder import PeopleRequest, build_contact_query
from chanakya.people import CompanyRef

spec = importlib.util.spec_from_file_location(
    "review_personas", pathlib.Path(__file__).parents[1] / "scripts/review_personas.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

EV = P.make_evidence("Wayam sales notes: CISO signs off on security tooling", "https://example.com/notes")


@pytest.fixture
def db(con):
    for pid, name, roles in (("PRD-A", "Alpha", None), ("PRD-B", "Beta", json.dumps(["CISO"]))):
        con.execute("INSERT INTO products(id,name,summary,buyer_roles_json,evidence_json,verification,source_path,"
                    "source_sha256,raw_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (pid, name, f"{name} summary", roles, json.dumps([{"url": f"https://{name}.example",
                                                                      "supports": "features"}]),
                     "unverified", "cat.yaml", "sha", "{}", "t"))
    con.commit()
    return con


def run(con, capsys, *argv, inputs=None, monkeypatch=None):
    if inputs is not None:
        it = iter(inputs)
        monkeypatch.setattr("builtins.input", lambda _p="": next(it))
    code = cli.cmd(list(argv), con)
    return code, capsys.readouterr().out


def _add(con, **kw):
    args = dict(titles=["Chief Information Security Officer"], seniorities=["C-suite"], reviewer="Krisha",
                reason="pilot plan", evidence=EV, product_id="PRD-A")
    args.update(kw)
    return P.add_human_mapping(con, None, "Security leader", **args)


# ---- display ----
def test_products_lists_all_and_flags_missing(db, capsys):
    code, out = run(db, capsys, "products")
    assert code == 0 and "2 products; 1 with NO buyer persona" in out
    assert "PRD-A" in out and "MISSING buyer_roles" in out and "buyer_roles=['CISO']" in out
    _, out = run(db, capsys, "products", "--missing")
    assert "PRD-A" in out and "PRD-B " not in out


def test_show_displays_description_roles_and_evidence(db, capsys):
    _, out = run(db, capsys, "show", "PRD-A")
    assert "Alpha summary" in out and "MISSING" in out and "https://Alpha.example" in out and "supports: features" in out
    with pytest.raises(SystemExit):
        run(db, capsys, "show", "PRD-NOPE")


def test_real_catalogue_all_25_missing_personas_readonly():
    """Read-only check of the working DB, if present: 25 products, none states buyer_roles."""
    path = pathlib.Path(__file__).parents[1] / "data/chanakya.db"
    if not path.exists():
        pytest.skip("working DB not present")
    import sqlite3
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT id, buyer_roles_json FROM products")]
    assert len(rows) == 25 and all(r["buyer_roles_json"] in (None, "null", "[]") for r in rows)


# ---- validation ----
@pytest.mark.parametrize("kw", [dict(reviewer=" "), dict(reason=""), dict(evidence=[]),
                                dict(evidence=[{"quote": "  "}]), dict(titles=[], seniorities=[]),
                                dict(seniorities=["Boss"]), dict(titles=['CISO" or x']),
                                dict(product_id="PRD-NOPE")])
def test_add_validation(db, kw):
    with pytest.raises(ValueError):
        _add(db, **kw)
    assert db.execute("SELECT COUNT(*) FROM persona_role_mappings").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM persona_mapping_history").fetchone()[0] == 0


def test_make_evidence_validation():
    with pytest.raises(ValueError):
        P.make_evidence(" ")
    with pytest.raises(ValueError):
        P.make_evidence("note", "ftp://x")
    assert P.make_evidence("note")[0]["verification"] == "human_provided"


def test_cli_add_requires_evidence_and_reports_errors(db, capsys):
    code, out = run(db, capsys, "add", "PRD-A", "--persona", "Sec", "--title", "CISO", "--seniority", "Boss",
                    "--by", "K", "--reason", "r", "--evidence", "notes")
    assert code == 2 and "unknown Clay seniority" in out


def test_cli_add_interactive_prompts(db, capsys, monkeypatch):
    code, out = run(db, capsys, "add", "PRD-A",
                    inputs=["Security leader", "CISO, Head of Security", "C-suite", "Krisha", "pilot", "sales notes"],
                    monkeypatch=monkeypatch)
    assert code == 0 and "[pending]" in out
    m = P.mappings_for(db, None, "PRD-A")[0]
    assert (m.titles, m.seniorities, m.status, m.reviewed_by) == (["CISO", "Head of Security"], ["C-suite"],
                                                                  "pending", "Krisha")


# ---- never auto-approved / only approved usable ----
def test_new_mapping_pending_and_not_usable(db):
    mid = _add(db)
    m = P.get_mapping(db, mid)
    assert m.status == "pending" and m.persona_source == "human"
    q = build_contact_query(PeopleRequest([CompanyRef(1, "a.example", None)], [m]))
    assert not q.ready


def test_catalogue_persona_recorded_pending_not_usable(db):
    from chanakya.catalog.models import Criterion, Evidence
    icp = ICP(buyer_roles=Criterion(values=["CISO"], evidence=[Evidence(source_path="cat.yaml", quote="CISO")]))
    plan = P.plan_personas(db, None, icp, "PRD-B")
    assert not plan.ready and plan.mappings[0].status == "pending"
    assert P.history(db, plan.mappings[0].id)[0]["reviewer"] == "system:catalogue"


def test_products_table_never_modified(db, capsys):
    before = [tuple(r) for r in db.execute("SELECT * FROM products ORDER BY id")]
    mid = _add(db)
    run(db, capsys, "approve", str(mid), "--by", "K", "--reason", "ok")
    assert [tuple(r) for r in db.execute("SELECT * FROM products ORDER BY id")] == before


# ---- approval / rejection ----
def test_approve_makes_usable(db, capsys):
    mid = _add(db)
    code, out = run(db, capsys, "approve", str(mid), "--by", "Krisha", "--reason", "confirmed with sales")
    assert code == 0 and "approved" in out
    m = P.get_mapping(db, mid)
    q = build_contact_query(PeopleRequest([CompanyRef(1, "a.example", None)], [m]))
    assert q.ready and 'job_title is_similar_to ("Chief Information Security Officer")' in q.dsl
    code, out = run(db, capsys, "approve", str(mid), "--by", "Krisha", "--reason", "again")
    assert code == 2 and "already approved" in out


def test_reject_not_usable_and_reason_required(db, capsys):
    mid = _add(db)
    with pytest.raises(ValueError):
        P.review_mapping(db, mid, "rejected", reviewer="K", reason=" ")
    run(db, capsys, "reject", str(mid), "--by", "K", "--reason", "wrong buyer")
    m = P.get_mapping(db, mid)
    assert m.status == "rejected" and not build_contact_query(PeopleRequest([CompanyRef(1, "a.example", None)],
                                                                            [m])).ready
    code, out = run(db, capsys, "approve", "999", "--by", "K", "--reason", "x")
    assert code == 2 and "no persona mapping" in out


# ---- updating + audit history ----
def test_update_returns_to_pending_and_keeps_history(db, capsys):
    mid = _add(db)
    P.review_mapping(db, mid, "approved", reviewer="Krisha", reason="ok")
    code, out = run(db, capsys, "update", str(mid), "--title", "CISO", "--title", "Head of Information Security",
                    "--clear-seniority", "--by", "Mriganka", "--reason", "broaden titles",
                    "--evidence", "call notes 2026-11-03")
    assert code == 0 and "pending re-approval" in out
    m = P.get_mapping(db, mid)
    assert (m.titles, m.seniorities, m.status) == (["CISO", "Head of Information Security"], [], "pending")
    assert len(m.evidence) == 2  # original evidence kept, new appended
    h = P.history(db, mid)
    assert [x["action"] for x in h] == ["create", "approve", "update"]
    assert [(x["from_status"], x["to_status"]) for x in h] == [(None, "pending"), ("pending", "approved"),
                                                               ("approved", "pending")]
    assert h[2]["snapshot"]["titles"] == ["Chief Information Security Officer"]  # pre-update version preserved
    assert h[2]["snapshot"]["seniorities"] == ["C-suite"] and h[2]["reviewer"] == "Mriganka"
    _, out = run(db, capsys, "history", str(mid))
    assert out.count("\n") == 3 and "broaden titles" in out
    # re-approve; history grows, never shrinks
    P.review_mapping(db, mid, "approved", reviewer="Krisha", reason="re-approved")
    assert len(P.history(db, mid)) == 4


def test_update_validation_leaves_mapping_and_history_untouched(db):
    mid = _add(db)
    before, hist = P.get_mapping(db, mid), P.history(db, mid)
    for kw in (dict(evidence=[]), dict(titles=[], seniorities=[]), dict(seniorities=["Boss"]), dict(reviewer="")):
        args = dict(reviewer="K", reason="r", evidence=EV)
        args.update(kw)
        with pytest.raises(ValueError):
            P.update_mapping(db, mid, **args)
    assert P.get_mapping(db, mid) == before and P.history(db, mid) == hist


def test_cli_update_requires_reviewer_reason_evidence(db, capsys):
    mid = _add(db)
    with pytest.raises(SystemExit):
        run(db, capsys, "update", str(mid), "--title", "CTO", "--by", "K", "--reason", "r")


def test_list_filters(db, capsys):
    a, b = _add(db), _add(db, product_id="PRD-B")
    P.review_mapping(db, b, "approved", reviewer="K", reason="ok")
    _, out = run(db, capsys, "list", "--status", "approved")
    assert "1 mapping(s)" in out and f"#{b} " in out
    _, out = run(db, capsys, "list", "--product", "PRD-A")
    assert f"#{a} " in out and f"#{b} " not in out


def test_legacy_stated_status_migrated_to_pending(tmp_path):
    from chanakya.storage.db import connect
    path = tmp_path / "x.db"
    c = connect(path)
    c.execute("INSERT INTO persona_role_mappings(persona,persona_source,titles_json,seniorities_json,status,"
              "evidence_json,created_at) VALUES('CISO','catalogue','[\"CISO\"]','[]','stated','[]','t')")
    c.commit(); c.close()
    assert connect(path).execute("SELECT status FROM persona_role_mappings").fetchone()[0] == "pending"


def test_cli_has_no_network_imports():
    src = (pathlib.Path(__file__).parents[1] / "scripts/review_personas.py").read_text("utf-8")
    for banned in ("httpx", "requests", "mcp", "clay.mcp_client", "ollama"):
        assert f"import {banned}" not in src and f"from {banned}" not in src
