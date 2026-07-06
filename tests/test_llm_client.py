from geo_monitor.llm_client import LLMResponsesClient, is_retryable_api_error
from geo_monitor.config import Settings


class FakeStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_retryable_status_classification():
    assert is_retryable_api_error(FakeStatusError(500)) is True
    assert is_retryable_api_error(FakeStatusError(429)) is True
    assert is_retryable_api_error(FakeStatusError(401)) is False
    assert is_retryable_api_error(ValueError("bad input")) is False


def test_client_rejects_placeholder_endpoint():
    try:
        LLMResponsesClient(Settings(llm_api_key="test"))
    except RuntimeError as exc:
        assert "LLM_BASE_URL" in str(exc)
    else:
        raise AssertionError("expected placeholder endpoint rejection")


def test_client_retries_retryable_status_then_succeeds():
    settings = Settings(llm_api_key="test", llm_base_url="https://provider.example/v1", retry_max_attempts=3)
    client = LLMResponsesClient(settings)
    calls = {"count": 0}

    def create(**payload):
        calls["count"] += 1
        if calls["count"] < 3:
            raise FakeStatusError(500)
        return {"status": "completed", "output_text": "ok"}

    client.client.responses.create = create

    assert client.create_response({"model": "m", "input": "q"})["output_text"] == "ok"
    assert calls["count"] == 3


def test_client_does_not_retry_non_retryable_status():
    settings = Settings(llm_api_key="test", llm_base_url="https://provider.example/v1", retry_max_attempts=3)
    client = LLMResponsesClient(settings)
    calls = {"count": 0}

    def create(**payload):
        calls["count"] += 1
        raise FakeStatusError(401)

    client.client.responses.create = create

    try:
        client.create_response({"model": "m", "input": "q"})
    except FakeStatusError:
        pass
    else:
        raise AssertionError("expected FakeStatusError")
    assert calls["count"] == 1
