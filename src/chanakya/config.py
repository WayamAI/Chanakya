"""Configuration from environment (optionally a .env file). Secrets are never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    ollama_api_key: str = field(repr=False)  # excluded from repr so it never lands in logs
    ollama_model: str
    ollama_fallback_model: str | None
    ollama_host: str
    ollama_timeout_s: float
    ollama_max_retries: int
    db_path: Path
    clay_mcp_url: str | None
    # The fallback model is used ONLY when this is explicitly true (no silent model switch).
    ollama_allow_fallback: bool = False

    def __str__(self) -> str:
        return (f"Settings(model={self.ollama_model}, fallback={self.ollama_fallback_model}, "
                f"host={self.ollama_host}, db={self.db_path}, api_key=***)")


def load_settings(env_file: str | os.PathLike | None = ".env") -> Settings:
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)  # real env vars win over .env

    key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not key:
        raise ConfigError("OLLAMA_API_KEY is not set")
    model = os.getenv("OLLAMA_MODEL", "").strip()
    if not model:
        raise ConfigError("OLLAMA_MODEL is not set (e.g. deepseek-v4-pro:0813)")

    try:
        timeout = float(os.getenv("OLLAMA_TIMEOUT_S", "180"))
        retries = int(os.getenv("OLLAMA_MAX_RETRIES", "2"))
    except ValueError as e:
        raise ConfigError(f"invalid numeric setting: {e}") from None

    return Settings(
        ollama_api_key=key,
        ollama_model=model,
        ollama_fallback_model=os.getenv("OLLAMA_FALLBACK_MODEL", "").strip() or None,
        ollama_host=os.getenv("OLLAMA_HOST", "https://ollama.com").rstrip("/"),
        ollama_timeout_s=timeout,
        ollama_max_retries=retries,
        db_path=Path(os.getenv("CHANAKYA_DB_PATH", "data/chanakya.db")),
        clay_mcp_url=os.getenv("CLAY_MCP_URL", "").strip() or None,
        ollama_allow_fallback=os.getenv("OLLAMA_ALLOW_FALLBACK", "false").strip().lower() == "true",
    )
