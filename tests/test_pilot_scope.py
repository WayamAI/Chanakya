import json

import pytest

from chanakya.catalog.models import ICP, Criterion, Evidence
from chanakya.scout.agent import ClaySearchProvider
from chanakya.scout.discovery import DiscoveryRequest
from chanakya.scout.dsl import ClaySchema
from chanakya.scout.industry_map import IndustryMapping
from chanakya.scout.pilot_scope import PilotScope, PilotScopeError

E = [Evidence(source_path="cat.yaml", quote="Public Sector")]
REAL = ClaySchema.from_capabilities()


def scope(tmp_path, **kw):
    d = {"countries": ["India"], "approved_by": "owner", "approved_at": "2026-11-03", **kw}
    p = tmp_path / "scope.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return PilotScope.load(p)


def product_icp():
    return ICP(industries=Criterion(values=["Public Sector"], evidence=E), missing=["countries", "employee_range"])


def test_apply_returns_copy_and_labels_constraint(tmp_path):
    orig = product_icp()
    out = scope(tmp_path).apply(orig)
    assert orig.countries is None and "countries" in orig.missing  # product ICP untouched
    ev = out.countries.evidence[0]
    assert out.countries.values == ["India"] and ev.verification == "approved_pilot_constraint"
    assert ev.field == "pilot_scope.countries" and "countries" not in out.missing


def test_unapproved_scope_rejected(tmp_path):
    with pytest.raises(PilotScopeError):
        scope(tmp_path, approved_by=None)


def test_never_overrides_stated_country(tmp_path):
    stated = product_icp().model_copy(update={"countries": Criterion(values=["Brazil"], evidence=E)})
    with pytest.raises(PilotScopeError):
        scope(tmp_path).apply(stated)


def _m(status):
    return IndustryMapping("Public Sector", ["Government Administration"], status, "llm", 0.75, "r", id=2)


def test_india_query_still_gated_by_mapping(tmp_path):
    icp = scope(tmp_path).apply(product_icp())
    prov = ClaySearchProvider(None, REAL)
    blocked = prov.prepare(DiscoveryRequest(icp, None, 1, [_m("needs_review")]))
    assert not blocked.ready  # country alone must never make it runnable
    ok = prov.prepare(DiscoveryRequest(icp, None, 1, [_m("approved")]))
    assert ok.ready and ok.text == ('select from companies where industry in ("Government Administration") and '
                                    'locations.any(country_name in ("India")) limit 1')
