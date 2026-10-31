"""Configuration loading. Every threshold lives in config.yaml, never in code."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve(p: str | os.PathLike[str]) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


@lru_cache(maxsize=4)
def load_config(path: str | None = None) -> dict[str, Any]:
    cfg_path = _resolve(path or os.getenv("PRAHARI_CONFIG", "config.yaml"))
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {cfg_path}. Copy the one in the repo root."
        )
    with cfg_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class Settings:
    """Environment-derived settings. No secrets are ever logged or persisted."""

    def __init__(self) -> None:
        self.crustdata_api_key: str | None = os.getenv("CRUSTDATA_API_KEY") or None
        self.crustdata_base_url: str = os.getenv(
            "CRUSTDATA_BASE_URL", "https://api.crustdata.com"
        ).rstrip("/")
        self.crustdata_api_version: str = os.getenv(
            "CRUSTDATA_API_VERSION", "2025-11-01"
        )
        # LLM provider: Google Gemini (free-tier API key from aistudio.google.com).
        self.gemini_api_key: str | None = (os.getenv("GEMINI_API_KEY")
                                           or os.getenv("GOOGLE_API_KEY") or None)
        self.gemini_base_url: str = os.getenv(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        # gemini-2.5-* is restricted to pre-existing users; 3.5 Flash-Lite is the
        # recommended current model for new projects. Fallbacks are tried in order.
        self.llm_model: str = os.getenv("PRAHARI_LLM_MODEL", "gemini-3.5-flash-lite")
        self.webhook_secret: str | None = os.getenv("CRUSTDATA_WEBHOOK_SECRET") or None
        self.webhook_tolerance_seconds: int = int(
            os.getenv("PRAHARI_WEBHOOK_TOLERANCE_SECONDS", "300")
        )
        self.db_path: Path = _resolve(os.getenv("PRAHARI_DB", "prahari.db"))

    @property
    def has_crustdata(self) -> bool:
        return bool(self.crustdata_api_key)

    @property
    def has_llm(self) -> bool:
        return bool(self.gemini_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
