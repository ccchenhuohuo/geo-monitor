from __future__ import annotations

from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, InternalServerError, OpenAI, RateLimitError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import LiveSettingsError, Settings, validate_live_settings
from .schemas import QueryRecord


class LLMClientError(RuntimeError):
    pass


RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


def is_retryable_api_error(exc: BaseException) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError, TimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in RETRYABLE_STATUS_CODES
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code in RETRYABLE_STATUS_CODES
    return False


def build_responses_payload(
    query_record: QueryRecord,
    settings: Settings,
    *,
    model: str | None = None,
    web_search_limit: int | None = None,
) -> dict[str, Any]:
    tool: dict[str, Any] = {"type": "web_search"}
    limit_value = web_search_limit if web_search_limit is not None else settings.web_search_limit
    if limit_value < 1 or limit_value > 20:
        raise ValueError("web_search_limit 必须在 1 到 20 之间")

    return {
        "model": model or settings.llm_model,
        "input": query_record.query,
        "tools": [tool],
        "tool_choice": "required",
        "include": ["web_search_call.action.sources"],
        "max_tool_calls": settings.max_tool_calls,
    }


class LLMResponsesClient:
    def __init__(self, settings: Settings):
        try:
            validate_live_settings(settings)
        except LiveSettingsError as exc:
            raise LLMClientError(str(exc)) from exc
        self.settings = settings
        self.client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),  # type: ignore[union-attr]
            timeout=settings.request_timeout_seconds,
            max_retries=0,
        )

    def create_response(self, payload: dict[str, Any]) -> Any:
        attempts = self.settings.retry_max_attempts

        @retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception(is_retryable_api_error),
        )
        def _call() -> Any:
            return self.client.responses.create(**payload)

        return _call()
