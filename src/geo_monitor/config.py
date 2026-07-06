from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_LLM_BASE_URL = "https://api.example.com/v1"
GEO_MONITOR_ENV_FILE = "GEO_MONITOR_ENV_FILE"

PLACEHOLDER_API_KEYS = {
    "replace_with_your_api_key",
    "your_api_key",
    "your-api-key",
    "changeme",
    "change_me",
    "sk-xxxx",
}


class LiveSettingsError(ValueError):
    pass


def workspace_root() -> Path:
    value = os.getenv("GEO_MONITOR_WORKSPACE") or os.getenv("GEO_MONITOR_HOME")
    if value:
        return Path(value).expanduser().resolve()
    return Path.cwd().resolve()


def configured_env_file() -> Path | None:
    value = os.getenv(GEO_MONITOR_ENV_FILE)
    if not value:
        return None
    return Path(value).expanduser().resolve()


def is_placeholder_base_url(value: str | None) -> bool:
    text = str(value or "").strip().rstrip("/").lower()
    return text == DEFAULT_LLM_BASE_URL.lower().rstrip("/")


def live_endpoint_status(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "invalid"
    if is_placeholder_base_url(text):
        return "placeholder"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "invalid"
    return "configured"


def validate_live_settings(settings: Settings) -> None:
    if not settings.has_api_key:
        status = settings.api_key_status
        if status == "placeholder":
            raise LiveSettingsError("LLM_API_KEY 仍是示例占位值；请配置真实 API key 后再执行 live 调用")
        raise LiveSettingsError("缺少 LLM_API_KEY；请通过环境变量或 GEO_MONITOR_ENV_FILE 指向的 .env 配置")
    endpoint_status = live_endpoint_status(settings.llm_base_url)
    if endpoint_status == "placeholder":
        raise LiveSettingsError("LLM_BASE_URL 仍是默认示例 endpoint；请配置真实 OpenAI-compatible endpoint 后再执行 live 调用")
    if endpoint_status == "invalid":
        raise LiveSettingsError("LLM_BASE_URL 无效；请配置包含 http(s) scheme 和 host 的 OpenAI-compatible endpoint")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_api_key: SecretStr | None = None
    llm_base_url: str = Field(default=DEFAULT_LLM_BASE_URL)
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
    def api_key_status(self) -> str:
        if not self.llm_api_key:
            return "missing"
        value = self.llm_api_key.get_secret_value().strip()
        if not value:
            return "missing"
        if value.lower() in PLACEHOLDER_API_KEYS:
            return "placeholder"
        return "configured"

    @property
    def has_api_key(self) -> bool:
        return self.api_key_status == "configured"

    @property
    def llm_base_url_status(self) -> str:
        return live_endpoint_status(self.llm_base_url)

    def redacted(self) -> dict[str, Any]:
        env_file = configured_env_file()
        return {
            "CONFIG_ENV_FILE": str(env_file) if env_file else None,
            "CONFIG_ENV_FILE_EXISTS": env_file.exists() if env_file else False,
            "WORKSPACE_ROOT": str(workspace_root()),
            "LLM_API_KEY": "***" if self.has_api_key else None,
            "LLM_API_KEY_STATUS": self.api_key_status,
            "LLM_API_KEY_SOURCE": _setting_source("LLM_API_KEY"),
            "LLM_BASE_URL": self.llm_base_url,
            "LLM_BASE_URL_STATUS": self.llm_base_url_status,
            "LLM_BASE_URL_SOURCE": _setting_source("LLM_BASE_URL"),
            "LLM_MODEL": self.llm_model,
            "WEB_SEARCH_LIMIT": self.web_search_limit,
            "MAX_TOOL_CALLS": self.max_tool_calls,
            "REQUEST_TIMEOUT_SECONDS": self.request_timeout_seconds,
            "RETRY_MAX_ATTEMPTS": self.retry_max_attempts,
            "CONCURRENCY": self.concurrency,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = configured_env_file()
    if env_file is not None:
        return Settings(_env_file=str(env_file))
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


def _setting_source(env_name: str) -> str:
    if os.getenv(env_name) is not None:
        return "environment"
    env_file = configured_env_file()
    if env_file and env_file.exists() and _env_file_contains(env_file, env_name):
        return GEO_MONITOR_ENV_FILE
    return "default"


def _env_file_contains(path: Path, env_name: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    prefix = f"{env_name}="
    quoted_prefix = f"export {env_name}="
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(prefix) or stripped.startswith(quoted_prefix):
            return True
    return False
