"""Doubao provider backed by Volcengine's official Ark runtime SDK."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ..config import LiveSettingsError, Settings, validate_endpoint_url, validate_provider_settings
from .base import ProviderDependencyError, ProviderTransportError


class _DoubaoArkResponses:
    def __init__(self, resource: Any, *, connection_error: type[Exception], timeout_error: type[Exception], status_error: type[Exception]):
        self._resource = resource
        self._connection_error = connection_error
        self._timeout_error = timeout_error
        self._status_error = status_error

    def create(self, **payload: Any) -> Any:
        try:
            return self._resource.create(**payload)
        except self._timeout_error as exc:
            raise _ark_transport_error(exc, retryable=True, fallback="Doubao Ark request timed out") from exc
        except self._connection_error as exc:
            raise _ark_transport_error(exc, retryable=True, fallback="Doubao Ark connection failed") from exc
        except self._status_error as exc:
            status_code = getattr(exc, "status_code", None)
            raise _ark_transport_error(
                exc,
                retryable=status_code in {408, 409, 429, 500, 502, 503, 504},
                fallback="Doubao Ark API request failed",
            ) from exc


class DoubaoArkClient:
    def __init__(self, sdk_client: Any, *, connection_error: type[Exception], timeout_error: type[Exception], status_error: type[Exception]):
        self._sdk_client = sdk_client
        self.responses = _DoubaoArkResponses(
            sdk_client.responses,
            connection_error=connection_error,
            timeout_error=timeout_error,
            status_error=status_error,
        )

    def close(self) -> None:
        self._sdk_client.close()


def _ark_transport_error(exc: BaseException, *, retryable: bool, fallback: str) -> ProviderTransportError:
    body = getattr(exc, "body", None)
    code = getattr(exc, "code", None)
    if not code and isinstance(body, dict):
        code = body.get("code")
    return ProviderTransportError(
        str(exc) or fallback,
        provider="doubao",
        retryable=retryable,
        status_code=getattr(exc, "status_code", None),
        code=str(code) if code not in (None, "") else None,
        request_id=getattr(exc, "request_id", None),
    )


class DoubaoArkProvider:
    name = "doubao"
    sdk_name = "volcenginesdkarkruntime"
    client_thread_safe = False

    def endpoint_url(self, settings: Settings) -> str:
        return settings.provider_base_url(self.name)

    def validate_endpoint(self, settings: Settings) -> None:
        endpoint = self.endpoint_url(settings)
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme == "http":
            raise ValueError("doubao_ark 原生 provider 仅支持 HTTPS；本地代理或自定义 host 请使用 openai_compatible adapter")
        try:
            validate_endpoint_url(
                endpoint,
                label="doubao provider endpoint",
                allow_insecure_http=False,
            )
        except LiveSettingsError as exc:
            raise ValueError(str(exc)) from exc
        if parsed_endpoint.scheme != "https" or (parsed_endpoint.hostname or "").lower() != "ark.cn-beijing.volces.com":
            raise ValueError(
                "doubao_ark provider 只接受官方 https://ark.cn-beijing.volces.com/api/v3 endpoint；"
                "兼容代理请使用 openai_compatible adapter"
            )
        if parsed_endpoint.query or parsed_endpoint.params:
            raise ValueError("doubao_ark adapter 的原生 endpoint 不能包含 query 或 params")
        if parsed_endpoint.path.rstrip("/") != "/api/v3":
            raise ValueError("doubao_ark adapter 需要 Ark 原生 /api/v3 endpoint")

    def create_client(self, settings: Settings) -> Any:
        try:
            validate_provider_settings(settings, self.name)
        except LiveSettingsError as exc:
            raise ValueError(str(exc)) from exc
        self.validate_endpoint(settings)
        endpoint = self.endpoint_url(settings)
        try:
            from volcenginesdkarkruntime import Ark
            from volcenginesdkarkruntime._exceptions import ArkAPIConnectionError, ArkAPIStatusError, ArkAPITimeoutError
        except ImportError as exc:
            raise ProviderDependencyError(
                "豆包 Ark adapter 需要官方 SDK；请安装 geo-monitor[doubao]"
            ) from exc
        client = Ark(
            base_url=endpoint,
            api_key=settings.provider_api_key(self.name).get_secret_value(),  # type: ignore[union-attr]
            timeout=settings.request_timeout_seconds,
            max_retries=0,
        )
        return DoubaoArkClient(
            client,
            connection_error=ArkAPIConnectionError,
            timeout_error=ArkAPITimeoutError,
            status_error=ArkAPIStatusError,
        )


__all__ = ["DoubaoArkClient", "DoubaoArkProvider"]
