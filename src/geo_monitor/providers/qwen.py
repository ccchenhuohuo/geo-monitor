"""Qwen provider backed by Alibaba Cloud's official DashScope SDK."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..config import LiveSettingsError, Settings, validate_endpoint_url, validate_provider_settings
from .base import ProviderDependencyError, ProviderResponseError, ProviderTransportError


@dataclass(frozen=True)
class DashScopeGenerationClient:
    generation: Any
    api_key: str
    base_address: str
    timeout: int

    def call(self, payload: dict[str, Any]) -> Any:
        try:
            response = self.generation.call(
                api_key=self.api_key,
                base_address=self.base_address,
                request_timeout=self.timeout,
                **payload,
            )
        except Exception as exc:
            if any(
                base.__module__.startswith("requests.") and base.__name__ in {"ConnectionError", "Timeout"}
                for base in exc.__class__.__mro__
            ):
                raise ProviderTransportError(
                    str(exc) or "DashScope transport failed",
                    provider="qwen",
                    retryable=True,
                ) from exc
            raise
        status_code = getattr(response, "status_code", None)
        if status_code != 200:
            code = str(getattr(response, "code", "") or "")
            request_id = str(getattr(response, "request_id", "") or "")
            message = str(getattr(response, "message", "") or "DashScope request failed")
            raise ProviderResponseError(
                f"DashScopeError: status={status_code}, code={code}, request_id={request_id}, message={message}",
                status_code=status_code if isinstance(status_code, int) else None,
                code=code or None,
                request_id=request_id or None,
            )
        headers = getattr(response, "headers", None)
        if isinstance(headers, Mapping) and any(
            str(key).lower() == "x-dashscope-partialresponse" and str(value).lower() == "true" for key, value in headers.items()
        ):
            raise ProviderTransportError(
                "DashScope returned a partial response after transport timeout",
                provider="qwen",
                retryable=True,
                request_id=str(getattr(response, "request_id", "") or "") or None,
            )
        finish_reasons = _finish_reasons(response)
        if not finish_reasons:
            raise ProviderResponseError(
                "DashScope returned HTTP 200 without message choices",
                status_code=200,
                code="MissingChoices",
                request_id=str(getattr(response, "request_id", "") or "") or None,
            )
        if any(reason in (None, "") for reason in finish_reasons):
            raise ProviderTransportError(
                "DashScope returned an incomplete response without finish_reason",
                provider="qwen",
                retryable=True,
                status_code=200,
                code="IncompleteResponse",
                request_id=str(getattr(response, "request_id", "") or "") or None,
            )
        unexpected = [str(reason) for reason in finish_reasons if str(reason) != "stop"]
        if unexpected:
            reason = unexpected[0]
            if reason == "insufficient_system_resource":
                raise ProviderTransportError(
                    "DashScope could not complete the response due to insufficient system resources",
                    provider="qwen",
                    retryable=True,
                    status_code=200,
                    code=reason,
                    request_id=str(getattr(response, "request_id", "") or "") or None,
                )
            raise ProviderResponseError(
                f"DashScope response incomplete: finish_reason={reason}",
                status_code=200,
                code=reason,
                request_id=str(getattr(response, "request_id", "") or "") or None,
            )
        return response


def _finish_reasons(response: Any) -> list[Any]:
    output = getattr(response, "output", None)
    if output is None and isinstance(response, Mapping):
        output = response.get("output")
    choices = output.get("choices") if isinstance(output, Mapping) else getattr(output, "choices", None)
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
        return []
    return [choice.get("finish_reason") if isinstance(choice, Mapping) else getattr(choice, "finish_reason", None) for choice in choices]


class QwenDashScopeProvider:
    name = "qwen"
    sdk_name = "dashscope"
    client_thread_safe = True

    def endpoint_url(self, settings: Settings) -> str:
        return settings.provider_base_url(self.name)

    def validate_endpoint(self, settings: Settings) -> None:
        endpoint = self.endpoint_url(settings)
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme == "http":
            raise ValueError("qwen_dashscope 原生 provider 仅支持 HTTPS；本地代理或自定义 host 请使用 openai_compatible adapter")
        try:
            validate_endpoint_url(
                endpoint,
                label="qwen provider endpoint",
                allow_insecure_http=False,
            )
        except LiveSettingsError as exc:
            raise ValueError(str(exc)) from exc
        hostname = (parsed_endpoint.hostname or "").lower()
        official_host = (
            hostname == "dashscope.aliyuncs.com"
            or (hostname.startswith("dashscope-") and hostname.endswith(".aliyuncs.com"))
            or hostname.endswith(".maas.aliyuncs.com")
        )
        if parsed_endpoint.scheme != "https" or not official_host:
            raise ValueError(
                "qwen_dashscope provider 只接受阿里云官方 DashScope/Model Studio endpoint；"
                "兼容代理请使用 openai_compatible adapter"
            )
        if "/compatible-mode/" in endpoint:
            raise ValueError(
                "qwen_dashscope adapter 需要 DashScope 原生 endpoint（例如 https://dashscope.aliyuncs.com/api/v1），"
                "不能使用 /compatible-mode/v1"
            )
        if parsed_endpoint.query or parsed_endpoint.params:
            raise ValueError("qwen_dashscope adapter 的原生 endpoint 不能包含 query 或 params")
        if parsed_endpoint.path.rstrip("/") != "/api/v1":
            raise ValueError("qwen_dashscope adapter 需要 DashScope 原生 /api/v1 endpoint")

    def create_client(self, settings: Settings) -> DashScopeGenerationClient:
        try:
            validate_provider_settings(settings, self.name)
        except LiveSettingsError as exc:
            raise ValueError(str(exc)) from exc
        self.validate_endpoint(settings)
        endpoint = self.endpoint_url(settings)
        try:
            from dashscope import Generation
        except ImportError as exc:
            raise ProviderDependencyError(
                "千问 DashScope adapter 需要官方 SDK；请安装 geo-monitor[qwen]"
            ) from exc
        return DashScopeGenerationClient(
            generation=Generation,
            api_key=settings.provider_api_key(self.name).get_secret_value(),  # type: ignore[union-attr]
            base_address=endpoint,
            timeout=settings.request_timeout_seconds,
        )


__all__ = ["DashScopeGenerationClient", "QwenDashScopeProvider"]
