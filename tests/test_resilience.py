import pytest
from tenacity import wait_none

from geo_monitor.config import Settings
from geo_monitor.resilience import is_retryable_api_error, retry_api_call


class FakeStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_retryable_status_classification():
    assert is_retryable_api_error(FakeStatusError(500)) is True
    assert is_retryable_api_error(FakeStatusError(429)) is True
    assert is_retryable_api_error(FakeStatusError(401)) is False
    assert is_retryable_api_error(ValueError("bad input")) is False


def test_retry_api_call_retries_retryable_status_then_succeeds(monkeypatch):
    monkeypatch.setattr("geo_monitor.resilience.wait_exponential", lambda **_: wait_none())
    settings = Settings(retry_max_attempts=3)
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise FakeStatusError(500)
        return "ok"

    assert retry_api_call(operation, settings) == "ok"
    assert calls == 3


def test_retry_api_call_does_not_retry_non_retryable_status(monkeypatch):
    monkeypatch.setattr("geo_monitor.resilience.wait_exponential", lambda **_: wait_none())
    settings = Settings(retry_max_attempts=3)
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        raise FakeStatusError(401)

    with pytest.raises(FakeStatusError):
        retry_api_call(operation, settings)
    assert calls == 1
