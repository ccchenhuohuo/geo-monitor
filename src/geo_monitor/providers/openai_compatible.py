"""Generic OpenAI-compatible transport."""

from __future__ import annotations

from openai import OpenAI

from ..config import LiveSettingsError, Settings, validate_endpoint_url, validate_live_settings


class OpenAICompatibleProvider:
    name = "openai_compatible"
    sdk_name = "openai"
    client_thread_safe = True

    def endpoint_url(self, settings: Settings) -> str:
        return settings.llm_base_url

    def validate_endpoint(self, settings: Settings) -> None:
        try:
            validate_endpoint_url(
                self.endpoint_url(settings),
                label="LLM_BASE_URL",
                allow_insecure_http=settings.allow_insecure_http,
            )
        except LiveSettingsError as exc:
            raise ValueError(str(exc)) from exc

    def create_client(self, settings: Settings) -> OpenAI:
        try:
            validate_live_settings(settings)
        except LiveSettingsError as exc:
            raise ValueError(str(exc)) from exc
        return OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),  # type: ignore[union-attr]
            timeout=settings.request_timeout_seconds,
            max_retries=0,
        )


__all__ = ["OpenAICompatibleProvider"]
