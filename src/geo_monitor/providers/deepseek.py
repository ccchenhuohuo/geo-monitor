"""DeepSeek provider using the SDK transport prescribed by DeepSeek docs."""

from __future__ import annotations

from urllib.parse import urlparse

from openai import OpenAI

from ..config import LiveSettingsError, Settings, validate_endpoint_url, validate_provider_settings


class DeepSeekProvider:
    """DeepSeek has no standalone official Python SDK; its docs prescribe openai-python."""

    name = "deepseek"
    sdk_name = "openai"
    client_thread_safe = True

    def endpoint_url(self, settings: Settings) -> str:
        return settings.provider_base_url(self.name)

    def validate_endpoint(self, settings: Settings) -> None:
        endpoint = self.endpoint_url(settings)
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme == "http":
            raise ValueError("deepseek 原生 provider 仅支持 HTTPS；本地代理或自定义 host 请使用 openai_compatible adapter")
        try:
            validate_endpoint_url(
                endpoint,
                label="deepseek provider endpoint",
                allow_insecure_http=settings.allow_insecure_http,
            )
        except LiveSettingsError as exc:
            raise ValueError(str(exc)) from exc
        if parsed_endpoint.scheme != "https" or (parsed_endpoint.hostname or "").lower() != "api.deepseek.com":
            raise ValueError("deepseek provider 只接受官方 https://api.deepseek.com endpoint；兼容代理请使用 openai_compatible adapter")
        if parsed_endpoint.query or parsed_endpoint.params or parsed_endpoint.path.rstrip("/") not in {"", "/v1"}:
            raise ValueError("deepseek provider endpoint 只接受官方根路径或 /v1，且不能包含 query 或 params")

    def create_client(self, settings: Settings) -> OpenAI:
        try:
            validate_provider_settings(settings, self.name)
        except LiveSettingsError as exc:
            raise ValueError(str(exc)) from exc
        self.validate_endpoint(settings)
        endpoint = self.endpoint_url(settings)
        return OpenAI(
            base_url=endpoint,
            api_key=settings.provider_api_key(self.name).get_secret_value(),  # type: ignore[union-attr]
            timeout=settings.request_timeout_seconds,
            max_retries=0,
        )


__all__ = ["DeepSeekProvider"]
