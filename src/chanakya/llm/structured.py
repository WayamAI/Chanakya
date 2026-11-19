"""Structured (JSON) LLM calls shared by Agents 5-7, bottom-up discovery and outreach.

* One entry point, `ask_structured`: JSON mode, temperature 0, pydantic validation, bounded repair retries.
* Every call (success or failure) is logged in `llm_calls` with the model that actually answered, token counts,
  latency and attempts. Keys are never logged.
* This module only checks SHAPE. Grounding (does a quote really occur in the evidence?) is a separate,
  deterministic step done by each agent (see `grounded`), so LLM judgement and validation stay apart.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from chanakya.storage.db import now

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMOutputError(RuntimeError):
    """The model did not return valid JSON for the schema after all attempts."""


def _log(con, agent, purpose, model, ok, attempts, error, pt, ot, lat, run_id):
    if con is None:
        return
    con.execute("INSERT INTO llm_calls(agent,purpose,model,ok,attempts,error,prompt_tokens,output_tokens,latency_s,"
                "run_id,at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (agent, purpose, model, int(ok), attempts, (error or "")[:500] or None, pt, ot, lat, run_id, now()))
    con.commit()


def ask_structured(llm, schema: type[T], *, system: str, user: str, agent: str, purpose: str, con=None,
                   run_id: int | None = None, attempts: int = 2) -> T:
    sys_msg = system + "\n\nReturn ONLY one JSON object matching this JSON schema:\n" + \
        json.dumps(schema.model_json_schema())
    msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}]
    pt = ot = 0
    model, last, t0 = None, "", time.monotonic()
    for i in range(1, attempts + 1):
        try:
            r = llm.chat(msgs, json_format=True, temperature=0.0)
        except Exception as e:  # transport/auth errors: logged, then re-raised unchanged
            _log(con, agent, purpose, model, False, i, f"{type(e).__name__}: {e}", pt, ot, time.monotonic() - t0,
                 run_id)
            raise
        model = getattr(r, "model", None) or model
        pt += getattr(r, "prompt_tokens", 0) or 0
        ot += getattr(r, "output_tokens", 0) or 0
        try:
            out = schema.model_validate(json.loads(r.content))
            _log(con, agent, purpose, model, True, i, None, pt, ot, time.monotonic() - t0, run_id)
            return out
        except (json.JSONDecodeError, ValidationError) as e:
            last = str(e)[:400]
            msgs += [r.message, {"role": "user", "content": f"Invalid output ({last}). Return only valid JSON "
                                                             "for the schema."}]
    _log(con, agent, purpose, model, False, attempts, last, pt, ot, time.monotonic() - t0, run_id)
    raise LLMOutputError(f"{agent}/{purpose}: model did not return valid JSON: {last}")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def grounded(quote: str | None, corpus: str, min_len: int = 8) -> bool:
    """Deterministic grounding check: the quote must occur verbatim (whitespace/case-insensitive) in corpus."""
    q = _norm(quote or "")
    return len(q) >= min_len and q in _norm(corpus)
