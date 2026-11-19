"""Central model routing: which model each workload uses. Configured here and in .env, nowhere else.

Tiers (Claude):  opus   complex reasoning, difficult research, multi-agent coordination, complex qualification
                 sonnet regular agent execution: research, enrichment, matching, drafting
                 haiku  lightweight classification, extraction, simple checks
Chanakya agent:  qwen    Qwen Max through Alibaba Cloud Model Studio (DashScope, OpenAI-compatible API):
                         DASHSCOPE_API_KEY, QWEN_MODEL (default qwen-max), DASHSCOPE_BASE_URL (default international).
                 ollama  explicit alternative only (CHANAKYA_ROUTE_CHANAKYA_AGENT=ollama): an OPEN Qwen model such as
                         qwen3.5 on local Ollama. That is not Qwen Max; Qwen Max weights are not published, so Ollama
                         cannot serve it.

Overrides: CHANAKYA_MODEL_OPUS / _SONNET / _HAIKU (model ids), CHANAKYA_ROUTE_<TASK>=opus|sonnet|haiku|ollama.
There is no silent substitution: when the routed provider is not configured, `client_for` returns None and the agent
falls back to its deterministic rules, which label their output as rule-based. Health reports three separate facts:
configured (settings present), available (the provider lists the model) and tested (a real call answered).
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass

import httpx

DEFAULT_MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
TASKS = {  # task -> (tier, what it is)
    "multi_agent_coordination": ("opus", "Orchestrating a combined top-down + bottom-up run"),
    "complex_qualification": ("opus", "Qualification of ambiguous or high-value accounts; gap reasoning"),
    "difficult_research": ("opus", "Deep company research across many sources"),
    "company_research": ("sonnet", "Company research and enrichment"),
    "people_research": ("sonnet", "Professional-context research for a person (bottom-up)"),
    "accelerator_matching": ("sonnet", "Match needs to Wayam accelerators with reasons"),
    "outreach_draft": ("sonnet", "Personalised email drafts from approved content"),
    "signal_classification": ("haiku", "Classify a page or item into a signal type"),
    "extraction": ("haiku", "Extract fields, dates and names from text"),
    "simple_checks": ("haiku", "Voice, placeholder and format checks"),
    "chanakya_agent": ("qwen", "Chanakya's main agent model (Qwen Max via Alibaba Cloud Model Studio)"),
}
ANTHROPIC_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class Route:
    task: str
    tier: str
    provider: str  # anthropic | dashscope | ollama
    model: str | None
    purpose: str
    configured: bool
    reason: str


def model_for_tier(tier: str) -> str | None:
    if tier == "ollama":
        return os.getenv("OLLAMA_MODEL") or None
    if tier == "qwen":
        return os.getenv("QWEN_MODEL") or "qwen-max"
    return os.getenv(f"CHANAKYA_MODEL_{tier.upper()}") or DEFAULT_MODELS[tier]


def route(task: str) -> Route:
    if task not in TASKS:
        raise KeyError(f"unknown task {task!r}; known: {sorted(TASKS)}")
    tier, purpose = TASKS[task]
    tier = (os.getenv(f"CHANAKYA_ROUTE_{task.upper()}") or tier).lower()
    if tier not in (*DEFAULT_MODELS, "ollama", "qwen"):
        raise ValueError(f"CHANAKYA_ROUTE_{task.upper()} must be opus, sonnet, haiku, qwen or ollama")
    model = model_for_tier(tier)
    if tier == "qwen":
        ok = bool(os.getenv("DASHSCOPE_API_KEY"))
        return Route(task, tier, "dashscope", model, purpose, ok,
                     "DASHSCOPE_API_KEY set" if ok else "DASHSCOPE_API_KEY is empty (Alibaba Cloud Model Studio key)")
    if tier == "ollama":
        ok = bool(os.getenv("OLLAMA_API_KEY") and model)
        why = "OLLAMA_API_KEY and OLLAMA_MODEL set" if ok else "OLLAMA_API_KEY or OLLAMA_MODEL is empty"
        return Route(task, tier, "ollama", model, purpose, ok, why)
    ok = bool(os.getenv("ANTHROPIC_API_KEY"))
    return Route(task, tier, "anthropic", model, purpose, ok,
                 "ANTHROPIC_API_KEY set" if ok else "ANTHROPIC_API_KEY is empty")


def routes() -> list[dict]:
    return [asdict(route(t)) for t in TASKS]


ORCHESTRATOR_TASK, ORCHESTRATOR_FALLBACK_TASK = "chanakya_agent", "multi_agent_coordination"


def orchestrator() -> dict:
    """Which model coordinates a run. Intended: Qwen Max (task `chanakya_agent`, Alibaba Cloud Model Studio). When that
    route is not configured the coordinator runs on the Claude Opus coordination route instead, and this is REPORTED
    (fallback=True with the reason) on every run and in Settings > Models; it is never silent. If neither is
    configured there is no coordinator and the run says so."""
    primary, fb = route(ORCHESTRATOR_TASK), route(ORCHESTRATOR_FALLBACK_TASK)
    if primary.configured:
        return {"task": primary.task, "provider": primary.provider, "model": primary.model, "fallback": False,
                "intended_model": primary.model, "reason": primary.reason}
    if fb.configured:
        return {"task": fb.task, "provider": fb.provider, "model": fb.model, "fallback": True,
                "intended_model": primary.model,
                "reason": f"intended orchestrator {primary.model} is not configured ({primary.reason}); "
                          f"coordination runs on {fb.model} until it is"}
    return {"task": None, "provider": None, "model": None, "fallback": False, "intended_model": primary.model,
            "reason": f"no orchestrator model: {primary.reason}; {fb.reason}"}


RETRY_STATUS = (429, 500, 502, 503, 504, 529)


def _post_with_retry(http: httpx.Client, path: str, body: dict, *, attempts: int = 3) -> httpx.Response:
    """POST with bounded retries on rate limits, overload and transport errors (1 s, then 3 s). Other statuses
    return at once so the caller reports the real error."""
    for i in range(attempts):
        try:
            r = http.post(path, json=body)
            if r.status_code not in RETRY_STATUS or i == attempts - 1:
                return r
        except httpx.TransportError:
            if i == attempts - 1:
                raise
        if not os.getenv("CHANAKYA_NO_RETRY_SLEEP"):
            time.sleep((1, 3, 6)[min(i, 2)])
    raise RuntimeError("unreachable")


# ------------------------------------------------------------------ Anthropic client (same chat() shape as Ollama)

class AnthropicError(RuntimeError):
    pass


@dataclass
class _Result:
    model: str
    content: str
    message: dict
    prompt_tokens: int
    output_tokens: int
    latency_s: float
    tool_calls: list


class AnthropicClient:
    """Minimal Messages API client exposing `chat(messages, json_format=, temperature=)` like OllamaClient, so
    `llm.structured.ask_structured` works with either provider. The key is sent as a header and never logged."""

    def __init__(self, model: str, *, api_key: str | None = None, transport: httpx.BaseTransport | None = None,
                 timeout: float = 120.0):
        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise AnthropicError("ANTHROPIC_API_KEY is not set")
        self.model = model
        self._http = httpx.Client(base_url=os.getenv("ANTHROPIC_BASE_URL", ANTHROPIC_URL), timeout=timeout,
                                  transport=transport,
                                  headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})

    def list_models(self) -> list[str]:
        r = self._http.get("/v1/models", params={"limit": 100})
        if r.status_code in (401, 403):
            raise AnthropicError(f"Anthropic auth failed ({r.status_code})")
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    def chat(self, messages: list[dict], tools=None, *, model: str | None = None, temperature: float = 0.2,
             json_format: bool = False, max_tokens: int = 4000) -> _Result:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if json_format:
            system += "\n\nReply with one JSON object only, no prose."
        msgs = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] in ("user", "assistant")]
        t0 = time.monotonic()
        body = {"model": model or self.model, "max_tokens": max_tokens, "messages": msgs}  # temperature is not accepted by current models
        if system.strip():
            body["system"] = [{"type": "text", "text": system.strip()}]
        r = _post_with_retry(self._http, "/v1/messages", body)
        if r.status_code >= 400:
            raise AnthropicError(f"Anthropic {r.status_code}: {r.text[:300]}")
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        if json_format and "{" in text:
            text = text[text.index("{"): text.rindex("}") + 1]
        u = d.get("usage") or {}
        return _Result(d.get("model", model or self.model), text, {"role": "assistant", "content": text},
                       u.get("input_tokens", 0), u.get("output_tokens", 0), time.monotonic() - t0, [])

    def close(self):
        self._http.close()


# ------------------------------------------------------------------ Qwen (Alibaba Cloud Model Studio / DashScope)

DASHSCOPE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


class QwenClient:
    """Qwen Max through Model Studio's OpenAI-compatible endpoint. Same chat() shape as the other clients. The model id
    is sent exactly as configured and the response's own model field is recorded: no substitution."""

    def __init__(self, model: str = "qwen-max", *, api_key: str | None = None,
                 transport: httpx.BaseTransport | None = None, timeout: float = 120.0):
        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise AnthropicError("DASHSCOPE_API_KEY is not set")
        self.model = model
        self._http = httpx.Client(base_url=os.getenv("DASHSCOPE_BASE_URL", DASHSCOPE_URL), timeout=timeout,
                                  transport=transport, headers={"Authorization": f"Bearer {key}"})

    def list_models(self) -> list[str]:
        r = self._http.get("/models")
        if r.status_code in (401, 403):
            raise AnthropicError(f"Model Studio auth failed ({r.status_code})")
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    def chat(self, messages: list[dict], tools=None, *, model: str | None = None, temperature: float = 0.2,
             json_format: bool = False, max_tokens: int = 4000) -> _Result:
        body = {"model": model or self.model, "messages": [{"role": m["role"], "content": m["content"]}
                                                          for m in messages], "max_tokens": max_tokens,
                "temperature": temperature}
        if json_format:
            body["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        r = _post_with_retry(self._http, "/chat/completions", body)
        if r.status_code >= 400:
            raise AnthropicError(f"Model Studio {r.status_code}: {r.text[:300]}")
        d = r.json()
        text = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        u = d.get("usage") or {}
        return _Result(d.get("model", body["model"]), text, {"role": "assistant", "content": text},
                       u.get("prompt_tokens", 0), u.get("completion_tokens", 0), time.monotonic() - t0, [])

    def close(self):
        self._http.close()


def client_for(task: str, *, transport: httpx.BaseTransport | None = None):
    """The LLM client for a task, or None when its provider is not configured (callers fall back to rules)."""
    r = route(task)
    if not r.configured:
        return None
    if r.provider == "anthropic":
        return AnthropicClient(r.model, transport=transport)
    if r.provider == "dashscope":
        return QwenClient(r.model, transport=transport)
    from chanakya.config import Settings
    from chanakya.llm.ollama_client import OllamaClient
    return OllamaClient(Settings.from_env(), transport=transport) if hasattr(Settings, "from_env") else \
        OllamaClient(Settings(), transport=transport)


# ------------------------------------------------------------------ health

def health(*, live: bool = True, test_call: bool = False, transport: httpx.BaseTransport | None = None) -> dict:
    """Per provider and per routed model: configured / available / tested, with the exact reason for each."""
    out = {"providers": {}, "routes": routes()}
    # Anthropic
    a = {"configured": bool(os.getenv("ANTHROPIC_API_KEY")), "available_models": None, "error": None, "models": {}}
    if a["configured"] and live:
        try:
            c = AnthropicClient("health", transport=transport)
            listed = set(c.list_models())
            a["available_models"] = sorted(listed)
            for tier in DEFAULT_MODELS:
                m = model_for_tier(tier)
                entry = {"model": m, "available": m in listed, "tested": False}
                if test_call and m in listed:
                    try:
                        c.chat([{"role": "user", "content": "Reply with OK."}], model=m, max_tokens=5)
                        entry["tested"] = True
                    except AnthropicError as e:
                        entry["error"] = str(e)[:200]
                a["models"][tier] = entry
        except (AnthropicError, httpx.HTTPError) as e:
            a["error"] = f"{type(e).__name__}: {e}"[:300]
    if not a["configured"]:
        a["error"] = "ANTHROPIC_API_KEY is not set in .env"
        a["models"] = {t: {"model": model_for_tier(t), "available": None, "tested": False} for t in DEFAULT_MODELS}
    out["providers"]["anthropic"] = a
    # Ollama: the public catalogue is readable without a key, so availability is checked even when not configured
    model = os.getenv("OLLAMA_MODEL") or None
    o = {"configured": bool(os.getenv("OLLAMA_API_KEY") and model), "model": model, "intended_model": "qwen-max",
         "available": None, "intended_available": None, "tested": False, "error": None}
    if live:
        try:
            host = os.getenv("OLLAMA_HOST", "https://ollama.com")
            with httpx.Client(timeout=20, transport=transport) as h:
                r = h.get(f"{host}/api/tags")
            r.raise_for_status()
            names = {m.get("name") or m.get("model") for m in r.json().get("models", [])}
            o["catalogue_size"] = len(names)
            o["available"] = model in names if model else None
            o["intended_available"] = any(n and n.split(":")[0] == "qwen-max" for n in names)
        except httpx.HTTPError as e:
            o["error"] = f"catalogue: {type(e).__name__}: {e}"[:300]
    if not os.getenv("OLLAMA_API_KEY"):
        o["error"] = (o["error"] + "; " if o["error"] else "") + "OLLAMA_API_KEY is empty in .env"
    out["providers"]["ollama"] = o
    q = {"configured": bool(os.getenv("DASHSCOPE_API_KEY")), "model": model_for_tier("qwen"),
         "endpoint": os.getenv("DASHSCOPE_BASE_URL", DASHSCOPE_URL), "available": None, "tested": False, "error": None}
    if not q["configured"]:
        q["error"] = "DASHSCOPE_API_KEY is not set in .env (Alibaba Cloud Model Studio API key)"
    elif live:
        try:
            c = QwenClient(q["model"], transport=transport)
            try:
                q["available"] = q["model"] in set(c.list_models())
            except (AnthropicError, httpx.HTTPError) as e:
                q["error"] = f"model list: {e}"[:200]
            if test_call:
                r = c.chat([{"role": "user", "content": "Reply with OK."}], max_tokens=5)
                q["tested"], q["answered_by"] = True, r.model
        except (AnthropicError, httpx.HTTPError) as e:
            q["error"] = f"{type(e).__name__}: {e}"[:300]
    out["providers"]["qwen"] = q
    out["orchestrator"] = orchestrator()
    return out


def usage(con) -> dict:
    """Per model, from the llm_calls log: how many real calls, the last success, the last error and which agents used
    it. This is invocation evidence, separate from configuration."""
    out: dict = {}
    for r in con.execute(
            "SELECT model, COUNT(*) n, SUM(ok) ok, MAX(CASE WHEN ok=1 THEN at END) last_ok, "
            "MAX(CASE WHEN ok=0 THEN at END) last_err, SUM(prompt_tokens) pt, SUM(output_tokens) ot "
            "FROM llm_calls GROUP BY model"):
        err = con.execute("SELECT error, agent, at FROM llm_calls WHERE model=? AND ok=0 ORDER BY id DESC LIMIT 1",
                          (r["model"],)).fetchone()
        out[r["model"]] = {
            "calls": r["n"], "ok": r["ok"] or 0, "failed": r["n"] - (r["ok"] or 0), "last_success_at": r["last_ok"],
            "last_error_at": r["last_err"], "last_error": (err["error"] or "")[:300] if err else None,
            "last_error_agent": err["agent"] if err else None,
            "prompt_tokens": r["pt"] or 0, "output_tokens": r["ot"] or 0,
            "agents": [x[0] for x in con.execute("SELECT DISTINCT agent FROM llm_calls WHERE model=? ORDER BY agent",
                                                 (r["model"],))]}
    return out
