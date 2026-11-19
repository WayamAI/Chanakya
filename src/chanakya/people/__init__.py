"""Agent 3 - People Finder (personas.py, finder.py, role_review.py).

Consumes rows from the Companies DB (Agent 2 output) and finds decision-makers. It uses its own Clay
allowlist (chanakya.clay.policy.PEOPLE_FINDER); Company Scout cannot call search-contacts.
LinkedIn profile research is Mriganka's existing Profile Researcher harness: integrate via ProfileResearcher,
do not reimplement it here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class CompanyRef:
    company_id: int
    domain: str
    name: str | None


@dataclass
class PersonCandidate:
    company_id: int
    full_name: str
    title: str | None
    source: str
    source_url: str | None


class PeopleFinder(Protocol):
    async def find(self, company: CompanyRef, roles: list[str], limit: int) -> list[PersonCandidate]: ...


class ProfileResearcher(Protocol):
    """Historical integration point. Implemented by chanakya.research.agent (harness `research` command)."""
    async def research(self, person: PersonCandidate) -> dict: ...
