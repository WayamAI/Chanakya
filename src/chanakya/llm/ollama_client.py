"""Thin Ollama /api/chat client with tool calling, retries and model fallback.

Follows the documented format: tools=[{type:function,function:{...}}], response
message.tool_calls[].function{name,arguments}, results returned as role=tool + tool_name.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from chanakya.config import Settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OllamaError(RuntimeError):
    pass


class OllamaAuthError(OllamaError):
    """401/403: bad or expired key. Not retried."""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class ChatResult:
    model: str
    content: str
    tool_calls: list[ToolCall]
    message: dict[str, Any]  # assistant message, ready to append to history
    latency_s: float
    prompt_tokens: int
    output_tokens: int


class OllamaClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self._s = settings
        self._http = httpx.Client(
            base_url=settings.ollama_host,
            headers={"Authorization": f"Bearer {settings.ollama_api_key}"},
            timeout=settings.ollama_timeout_s,
            transport=transport,
        )

    def list_models(self) -> list[str]:
        """Names from /api/tags (used by `chanakya llm-check` to confirm the configured model exists)."""
        r = self._http.get("/api/tags")
        if r.status_code in (401, 403):
            raise OllamaAuthError(f"Ollama auth failed ({r.status_code})")
        r.raise_for_status()
        return [m.get("name") or m.get("model") for m in r.json().get("models", [])]

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- public API -------------------------------------------------------
    def chat(self, messages: list[dict], tools: list[dict] | None = None, *,
             model: str | None = None, temperature: float = 0.2,
             json_format: bool = False, max_tokens: int = 4000) -> ChatResult:
        """One chat turn. The fallback model is tried once ONLY if OLLAMA_ALLOW_FALLBACK=true; the model actually
        used is always returned in ChatResult.model (and logged by llm.structured)."""
        primary = model or self._s.ollama_model
        try:
            return self._chat_with_retries(primary, messages, tools, temperature, json_format, max_tokens)
        except OllamaAuthError:
            raise
        except OllamaError as e:
            fb = self._s.ollama_fallback_model
            if model is None and fb and fb != primary and self._s.ollama_allow_fallback:
                log.warning("primary model %s failed (%s); falling back to %s", primary, e, fb)
                return self._chat_with_retries(fb, messages, tools, temperature, json_format, max_tokens)
            raise

    # ---- internals --------------------------------------------------------
    def _chat_with_retries(self, model, messages, tools, temperature, json_format, max_tokens) -> ChatResult:
        body: dict[str, Any] = {
            "model": model, "messages": messages, "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools:
            body["tools"] = tools
        if json_format:
            body["format"] = "json"

        attempts = self._s.ollama_max_retries + 1
        last: Exception | None = None
        for i in range(attempts):
            t0 = time.monotonic()
            try:
                r = self._http.post("/api/chat", json=body)
            except httpx.TransportError as e:  # timeouts, connection resets
                last = OllamaError(f"transport error: {type(e).__name__}")
            else:
                if r.status_code in (401, 403):
                    raise OllamaAuthError(f"Ollama auth failed ({r.status_code}); check/rotate OLLAMA_API_KEY")
                if r.status_code == 200:
                    return self._parse(model, r.json(), time.monotonic() - t0)
                last = OllamaError(f"HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code not in RETRYABLE_STATUS:
                    raise last
            if i < attempts - 1:
                time.sleep(min(2 ** i, 8))
        raise last or OllamaError("unknown failure")

    @staticmethod
    def _parse(model: str, data: dict, latency: float) -> ChatResult:
        msg = data.get("message") or {}
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):  # some models return a JSON string
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_unparseable": args}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args, raw=tc))
        assistant = {"role": "assistant", "content": msg.get("content", "")}
        if msg.get("thinking"):
            assistant["thinking"] = msg["thinking"]
        if msg.get("tool_calls"):
            assistant["tool_calls"] = msg["tool_calls"]
        return ChatResult(model=model, content=msg.get("content", ""), tool_calls=calls,
                          message=assistant, latency_s=latency,
                          prompt_tokens=data.get("prompt_eval_count", 0) or 0,
                          output_tokens=data.get("eval_count", 0) or 0)


def tool_result_message(name: str, result: Any) -> dict:
    """Build the role=tool message for a tool result."""
    return {"role": "tool", "tool_name": name,
            "content": result if isinstance(result, str) else json.dumps(result, default=str)}
