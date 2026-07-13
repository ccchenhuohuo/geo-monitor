"""Shared retry policy for provider API operations."""

from __future__ import annotations

from typing import Callable, TypeVar

from openai import APIConnectionError, APIStatusError, APITimeoutError, InternalServerError, RateLimitError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from .config import Settings

RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
T = TypeVar("T")


def is_retryable_api_error(exc: BaseException) -> bool:
    """Return whether an API failure is safe to retry."""

    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError, TimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in RETRYABLE_STATUS_CODES
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code in RETRYABLE_STATUS_CODES
    return False


def retry_api_call(operation: Callable[[], T], settings: Settings) -> T:
    """Run a provider operation within the application's single retry boundary."""

    retrying = Retrying(
        reraise=True,
        stop=stop_after_attempt(settings.retry_max_attempts),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception(is_retryable_api_error),
    )
    return retrying(operation)


__all__ = ["is_retryable_api_error", "retry_api_call"]
