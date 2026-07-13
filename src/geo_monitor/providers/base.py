"""Provider SDK boundaries used by adapters and runners."""

from __future__ import annotations

from contextlib import suppress
from threading import Lock, local
from typing import Any, Protocol

from ..config import Settings


class ProviderDependencyError(RuntimeError):
    """Raised when an optional official provider SDK is not installed."""


class ProviderResponseError(RuntimeError):
    """A provider response failed before it could be normalized."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class ProviderTransportError(RuntimeError):
    """Stable error raised when a provider SDK transport fails."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retryable: bool,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class ProviderBackend(Protocol):
    name: str
    sdk_name: str
    client_thread_safe: bool

    def create_client(self, settings: Settings) -> Any: ...

    def endpoint_url(self, settings: Settings) -> str: ...

    def validate_endpoint(self, settings: Settings) -> None: ...


class ThreadLocalProviderClient:
    """Expose one provider client per worker thread for thread-unsafe SDKs."""

    def __init__(self, provider: ProviderBackend, settings: Settings):
        self._provider = provider
        self._settings = settings
        self._local = local()
        self._clients: list[Any] = []
        self._lock = Lock()

    def _client(self) -> Any:
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._provider.create_client(self._settings)
            self._local.client = client
            with self._lock:
                self._clients.append(client)
        return client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client(), name)

    def close(self) -> None:
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            close = getattr(client, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()


def create_runtime_client(provider: ProviderBackend, settings: Settings, *, concurrency: int) -> Any:
    if concurrency > 1 and not provider.client_thread_safe:
        return ThreadLocalProviderClient(provider, settings)
    return provider.create_client(settings)


__all__ = [
    "ProviderBackend",
    "ProviderDependencyError",
    "ProviderResponseError",
    "ProviderTransportError",
    "ThreadLocalProviderClient",
    "create_runtime_client",
]
