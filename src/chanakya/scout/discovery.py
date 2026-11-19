"""Company-discovery provider interface for Agent 2 (Company Scout).

A provider turns an ICP into a provider-specific query (offline, `prepare`) and, only when explicitly
allowed, executes it (`discover`). The Scout handles dedupe, fit and persistence identically for every
provider, and each candidate carries its provider, tool, query and source URL for provenance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from chanakya.catalog.models import ICP


@dataclass
class DiscoveryRequest:
    icp: ICP
    icp_id: int | None
    limit: int
    industry_mappings: list | None = None
    context: dict = field(default_factory=dict)  # search-time context (e.g. pilot scope) saved with the run
    # Providers append every raw response here AS IT ARRIVES, so responses survive a later failure.
    response_log: list = field(default_factory=list)


@dataclass
class PreparedQuery:
    provider: str
    text: str  # provider-native query (Clay DSL, SPARQL, ...); "" when blocked
    ready: bool  # False -> must not be executed
    issues: list[str] = field(default_factory=list)  # pending reviews / blockers
    unmapped: list[str] = field(default_factory=list)  # criteria the provider cannot express
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompanyCandidate:
    domain: str
    name: str | None
    record: dict  # provider record, kept verbatim for audit
    provider: str
    tool: str  # e.g. "search-companies", "wikidata-sparql"
    source_url: str | None
    task_id: str | None = None


@dataclass
class DiscoveryResult:
    prepared: PreparedQuery
    candidates: list[CompanyCandidate]
    raw: Any = None  # the INITIAL response (never overwritten by follow-ups)
    skipped: list[str] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)  # [{tool, task_id, phase, payload}] in call order
    records_response_index: int | None = None  # which response the candidates were parsed from


class DiscoveryBlocked(PermissionError):
    """Execution refused: not ready, not allowed, or would incur unapproved cost."""


@runtime_checkable
class DiscoveryProvider(Protocol):
    name: str
    cost_note: str  # human-readable cost status, shown in reports

    def prepare(self, req: DiscoveryRequest) -> PreparedQuery: ...

    async def discover(self, prepared: PreparedQuery, req: DiscoveryRequest) -> DiscoveryResult: ...
