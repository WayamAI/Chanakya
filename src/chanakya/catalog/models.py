"""Agent 1 contracts. The ICP is the hand-off object consumed by Agent 2 (Company Scout)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    source_path: str
    quote: str  # verbatim excerpt (document text, or the exact YAML value)
    field: str | None = None  # e.g. "products[PRD-RAKSHA].target_industries.stated"
    url: str | None = None  # upstream public evidence URL, if the source records one
    verification: str = "unverified"


class Offering(BaseModel):
    name: str
    description: str
    problems_solved: list[str] = []
    evidence: list[Evidence] = Field(min_length=1)


class Criterion(BaseModel):
    values: list[str]
    evidence: list[Evidence] = Field(min_length=1)


class ICP(BaseModel):
    """Every populated criterion carries evidence. Unstated criteria stay None and are listed in `missing`."""
    industries: Criterion | None = None
    countries: Criterion | None = None
    employee_min: int | None = None
    employee_max: int | None = None
    employee_evidence: list[Evidence] = []
    buyer_roles: Criterion | None = None
    keywords: Criterion | None = None
    disqualifiers: Criterion | None = None
    offerings: list[str] = []
    missing: list[str] = []
    verification: str = "unverified"  # "verified" only after Wayam-owner review
    notes: list[str] = []
