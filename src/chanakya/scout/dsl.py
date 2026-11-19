"""ICP -> Clay Search DSL, validated against the *discovered* search-companies tool description.

Field names and enum values come from docs/clay_capabilities.json (tools/list), not hard-coding.
Criteria that cannot be mapped to a valid Clay field/enum value are reported, never silently guessed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from chanakya.catalog.models import ICP
from chanakya.scout.industry_map import USABLE

MAX_COMPANIES = 5  # acceptance-test hard cap


class DSLError(ValueError):
    pass


@dataclass
class ClaySchema:
    fields: set[str]
    enums: dict[str, list[str]]  # field -> allowed values

    @classmethod
    def from_capabilities(cls, path: str | Path = "docs/clay_capabilities.json") -> "ClaySchema":
        tools = {t["name"]: t for t in json.loads(Path(path).read_text(encoding="utf-8"))["tools"]}
        if "search-companies" not in tools:
            raise DSLError("search-companies not found in capability report; re-run discovery")
        return cls.from_description(tools["search-companies"]["description"])

    @classmethod
    def from_description(cls, desc: str) -> "ClaySchema":
        enums: dict[str, list[str]] = {}
        fields: set[str] = set()
        # Field reference lines look like: "- industry (string, enum): ... Values: "A", "B""
        for m in re.finditer(r"^- ([a-z_.]+) \(([^)]*)\):(.*)$", desc, re.M):
            name, typ, rest = m.group(1), m.group(2), m.group(3)
            fields.add(name)
            vm = re.search(r"Values: (.*)$", rest)
            if "enum" in typ and vm:
                enums[name] = re.findall(r'"([^"]+)"', vm.group(1))
        # Subfield lines for locations are indented the same way; record dotted name too.
        if "country_name" in fields:
            fields.add("locations.country_name")
            if "country_name" in enums:
                enums["locations.country_name"] = enums["country_name"]
        return cls(fields, enums)


@dataclass
class DSLQuery:
    text: str
    used: dict[str, list[str]] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    pending_review: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """A live search is allowed only when no criterion is awaiting review."""
        return not self.pending_review


def _q(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _match_enum(value: str, allowed: list[str]) -> str | None:
    for a in allowed:
        if a.lower() == value.strip().lower():
            return a
    return None


def build_company_query(icp: ICP, schema: ClaySchema, limit: int = MAX_COMPANIES,
                        industry_mappings: list | None = None) -> DSLQuery:
    """industry_mappings: output of industry_map.map_industries. Only exact/approved values are used."""
    if not 1 <= limit <= MAX_COMPANIES:
        raise DSLError(f"limit must be 1..{MAX_COMPANIES}")
    preds, used, unmapped, pending = [], {}, [], []

    def enum_pred(field_name: str, values: list[str], label: str):
        allowed = schema.enums.get(field_name)
        if allowed is None:
            unmapped.append(f"{label}: Clay field '{field_name}' not in discovered schema"); return
        ok = []
        for v in values:
            m = _match_enum(v, allowed)
            (ok.append(m) if m else unmapped.append(f"{label}: '{v}' is not a Clay {field_name} value"))
        if ok:
            used[field_name] = ok
            preds.append(f"{field_name} in ({', '.join(map(_q, ok))})")

    if industry_mappings is not None:
        allowed = set(schema.enums.get("industry", []))
        vals: list[str] = []
        for m in industry_mappings:
            if m.status in USABLE:
                vals += [v for v in m.canonical if v in allowed and v not in vals]
            else:
                pending.append(f"industries: '{m.original_term}' -> {m.canonical or '[]'} is {m.status} "
                               f"(confidence {m.confidence:.2f}); excluded until approved")
        if vals:
            used["industry"] = vals
            preds.append(f"industry in ({', '.join(map(_q, vals))})")
    elif icp.industries:
        enum_pred("industry", icp.industries.values, "industries")
    if icp.countries:
        loc_field = "country_name" if "country_name" in schema.enums else "locations.country_name"
        allowed = schema.enums.get(loc_field)
        ok = [m for v in icp.countries.values if (m := _match_enum(v, allowed or []))]
        unmapped += [f"countries: '{v}' not a Clay country value" for v in icp.countries.values
                     if not _match_enum(v, allowed or [])]
        if ok:
            used["locations.country_name"] = ok
            preds.append(f"locations.any(country_name in ({', '.join(map(_q, ok))}))")
    if "estimated_employee_count" in schema.fields:
        if icp.employee_min is not None:
            preds.append(f"estimated_employee_count >= {int(icp.employee_min)}")
        if icp.employee_max is not None:
            preds.append(f"estimated_employee_count <= {int(icp.employee_max)}")
    if icp.keywords and "description" in schema.fields:
        used["description"] = icp.keywords.values
        preds.append(f"description contains ({', '.join(map(_q, icp.keywords.values))})")
    if not preds:
        if pending:  # blocked on review: report, never fall back to an unfiltered search
            return DSLQuery("", used, unmapped, pending)
        raise DSLError("ICP has no criteria mappable to Clay search; refusing an unfiltered search")
    text = "select from companies where " + " and ".join(preds) + f" limit {limit}"
    return DSLQuery(text, used, unmapped, pending)
