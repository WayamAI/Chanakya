"""Knowledge layer: one grounded record per accelerator, with what may be claimed and what is missing."""
from chanakya.intel import knowledge as KN
from chanakya.intel import proof
from chanakya.storage.db import connect


def _con(tmp_path):
    c = connect(tmp_path / "t.db")
    for pid, name, summary in (("PRD-SETU", "Setu", "Moves legacy BI dashboards to modern platforms."),
                               ("PRD-VEDHA", "Vedha", "Autonomous penetration testing.")):
        c.execute("INSERT INTO products(id,name,status,summary,needs_json,caveats_json,verification,source_path,"
                  "source_sha256,raw_json,imported_at) VALUES(?,?,'existing_offering',?,?,?,'unverified','t','t','{}','t')",
                  (pid, name, summary, '[{"need": "migrate", "quote": "legacy BI dashboards"}]', "null"))
    proof.seed(c)
    return c


def test_record_lists_claims_allowed_forbidden_and_gaps(tmp_path):
    con = _con(tmp_path)
    a = KN.accelerator(con, "PRD-SETU")
    assert a["capabilities"] == ["legacy BI dashboards"] and a["limitations"] == []  # JSON null is an empty list
    assert any("1,200" in c for c in a["proof"]["may_claim"])
    assert any("BusinessObjects" in c for c in a["proof"]["must_not_claim"])
    assert "Tableau to Power BI" in a["signal_phrases"]["hiring"] and "OBIEE" in a["signal_phrases"]["tenders"]
    assert any("no ICP" in g for g in a["gaps"]) and any("persona" in g for g in a["gaps"])
    v = KN.accelerator(con, "PRD-VEDHA")
    assert v["proof"]["may_claim"] == [] and any("no registered proof" in g for g in v["gaps"])
    assert KN.accelerator(con, "PRD-NOPE") is None


def test_catalogue_and_search_use_recorded_text_only(tmp_path):
    con = _con(tmp_path)
    cat = {a["id"]: a for a in KN.catalogue(con)}
    assert cat["PRD-SETU"]["proof_points"] >= 1 and cat["PRD-VEDHA"]["proof_points"] == 0
    assert [h["id"] for h in KN.search(con, "penetration")] == ["PRD-VEDHA"]
    assert KN.search(con, "quantum blockchain") == [] and KN.search(con, "") == []
