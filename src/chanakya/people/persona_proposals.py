"""Draft buyer personas for accelerators that have none, for HUMAN approval (never used until approved).

Sonnet reads only the accelerator's catalogue record (summary, needs, problems solved) and proposes up to two
personas with job titles and Clay seniority values. Each proposal must quote a sentence that appears verbatim in
that catalogue text; proposals without a verifiable quote are dropped. They are stored through
personas.add_human_mapping as `pending`, with persona_source 'ai_proposal', so Review > Personas shows who
proposed them and why. Approval stays an explicit human step.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from chanakya.llm.structured import ask_structured
from chanakya.people.personas import CLAY_SENIORITIES, add_human_mapping


class Persona(BaseModel):
    persona: str
    titles: list[str] = Field(max_length=3)
    seniorities: list[str] = Field(max_length=3)
    quote: str


class Proposal(BaseModel):
    personas: list[Persona] = Field(max_length=2)


def _text(r) -> str:
    needs = [n.get("quote", "") for n in (json.loads(r["needs_json"] or "[]") or []) if isinstance(n, dict)]
    probs = json.loads(r["problems_solved_json"] or "[]") or []
    return "\n".join([r["summary"] or "", *[str(p) for p in probs], *needs])


def propose_missing(con, llm, *, limit: int = 25) -> list[dict]:
    rows = con.execute("SELECT * FROM products p WHERE NOT EXISTS (SELECT 1 FROM persona_role_mappings m WHERE "
                       "m.product_id=p.id) ORDER BY id LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        text = _text(r)
        if not text.strip():
            out.append({"product_id": r["id"], "skipped": "no catalogue text"})
            continue
        p = ask_structured(llm, Proposal, agent="catalog", purpose="persona_proposal", con=con, system=(
            "You propose B2B buyer personas for one Wayam AI accelerator, from its catalogue text ONLY. Choose the "
            "people who would own the problem or budget. `persona` is a SHORT label of at most 6 words (e.g. 'Data / BI "
            "leader'). Titles must be real, common job titles. Seniorities must "
            f"come from: {sorted(CLAY_SENIORITIES)}. `quote` must be copied EXACTLY from the catalogue text and "
            "show why this persona cares."), user=f"Accelerator: {r['name']}\nCatalogue text:\n{text[:4000]}")
        saved = []
        for x in p.personas:
            if x.quote.strip() not in text:
                saved.append({"persona": x.persona, "dropped": "quote not found in catalogue text"})
                continue
            sen = [s for s in x.seniorities if s in CLAY_SENIORITIES]
            try:
                mid = add_human_mapping(con, None, x.persona, titles=x.titles, seniorities=sen,
                                    reviewer="Chanakya proposal (Sonnet)", product_id=r["id"],
                                    reason="Drafted from the catalogue text; needs human approval before any search",
                                    evidence=[{"quote": x.quote.strip(), "source": "catalogue", "url": None}])
            except ValueError as e:
                saved.append({"persona": x.persona[:60], "dropped": str(e)[:120]})
                continue
            con.execute("UPDATE persona_role_mappings SET persona_source='ai_proposal' WHERE id=?", (mid,))
            saved.append({"mapping_id": mid, "persona": x.persona, "titles": x.titles, "seniorities": sen})
        con.commit()
        out.append({"product_id": r["id"], "name": r["name"], "proposals": saved})
    return out
