from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PLACEHOLDER_API_KEYS = {
    "replace_with_your_api_key",
    "your_api_key",
    "your-api-key",
    "changeme",
    "change_me",
    "sk-xxxx",
}


def workspace_root() -> Path:
    value = os.getenv("GEO_MONITOR_WORKSPACE") or os.getenv("GEO_MONITOR_HOME")
    if value:
        return Path(value).expanduser().resolve()
    return Path.cwd().resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_api_key: SecretStr | None = None
    llm_base_url: str = Field(
        default="https://api.example.com/v1",
    )
    llm_model: str = "provider-model"
    web_search_limit: int = Field(default=5, ge=1, le=20)
    max_tool_calls: int = Field(default=2, ge=1, le=10)
    request_timeout_seconds: int = Field(default=90, ge=5)
    retry_max_attempts: int = Field(default=3, ge=1, le=10)
    concurrency: int = Field(default=1, ge=1, le=8)

    @field_validator("llm_base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:  # noqa: N805
        return value.rstrip("/")

    @property
    def has_api_key(self) -> bool:
        if not self.llm_api_key:
            return False
        value = self.llm_api_key.get_secret_value().strip()
        return bool(value and value.lower() not in PLACEHOLDER_API_KEYS)

    def redacted(self) -> dict[str, Any]:
        data = {
            "LLM_API_KEY": "***" if self.has_api_key else None,
            "LLM_BASE_URL": self.llm_base_url,
            "LLM_MODEL": self.llm_model,
            "WEB_SEARCH_LIMIT": self.web_search_limit,
            "MAX_TOOL_CALLS": self.max_tool_calls,
            "REQUEST_TIMEOUT_SECONDS": self.request_timeout_seconds,
            "RETRY_MAX_ATTEMPTS": self.retry_max_attempts,
            "CONCURRENCY": self.concurrency,
        }
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def redact_secret(text: str | None, settings: Settings | None = None) -> str | None:
    if text is None:
        return None
    settings = settings or get_settings()
    if settings.has_api_key:
        secret = settings.llm_api_key.get_secret_value()  # type: ignore[union-attr]
        if secret:
            text = text.replace(secret, "***")
    return text
