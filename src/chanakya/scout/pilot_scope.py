"""Pilot-level search constraints (e.g. India-only), kept separate from product ICPs.

The scope file is an explicit human decision about *where to search in this pilot*. It is applied to an in-memory
copy of the ICP at query time. It never modifies the catalogue, the products table or stored ICP rows, and its
evidence is labelled `approved_pilot_constraint` so it cannot be mistaken for a stated product requirement.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from chanakya.catalog.models import ICP, Criterion, Evidence

PILOT_VERIFICATION = "approved_pilot_constraint"


class PilotScopeError(ValueError):
    pass


@dataclass
class PilotScope:
    path: str
    countries: list[str] = field(default_factory=list)
    approved_by: str | None = None
    approved_at: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> "PilotScope | None":
        p = Path(path)
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("countries") and not (d.get("approved_by") and d.get("approved_at")):
            raise PilotScopeError(f"{p}: constraints present but approved_by/approved_at missing")
        return cls(p.as_posix(), list(d.get("countries") or []), d.get("approved_by"), d.get("approved_at"))

    def apply(self, icp: ICP) -> ICP:
        """Return a COPY of the ICP with pilot constraints. The original object is not modified."""
        out = icp.model_copy(deep=True)
        if self.countries:
            if icp.countries is not None:
                raise PilotScopeError("ICP already states countries; refusing to override a stated criterion")
            out.countries = Criterion(values=list(self.countries), evidence=[Evidence(
                source_path=self.path, quote=", ".join(self.countries), field="pilot_scope.countries",
                verification=PILOT_VERIFICATION)])
            out.missing = [m for m in out.missing if m != "countries"]
            out.notes = out.notes + [f"pilot constraint: countries={self.countries} "
                                     f"(approved by {self.approved_by}, {self.approved_at}); not a product requirement"]
        return out
