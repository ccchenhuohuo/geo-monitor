import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import ModuleType, SimpleNamespace

import pytest

from geo_monitor.adapters import build_sampling_profile, get_adapter
from geo_monitor.analysis import estimate_job_analysis
from geo_monitor.analysis.cache import extraction_cache_key
from geo_monitor.config import Settings
from geo_monitor.job import build_job_bundle, run_job_bundle
from geo_monitor.providers import ProviderResponseError, ProviderTransportError, create_runtime_client, get_provider
from geo_monitor.providers.doubao import DoubaoArkClient
from geo_monitor.providers.qwen import DashScopeGenerationClient
from geo_monitor.request_fingerprint import base_url_fingerprint
from geo_monitor.resilience import is_retryable_api_error
from geo_monitor.schemas import QueryRecord


def test_provider_registry_has_distinct_sdk_boundaries():
    assert get_provider("openai_compatible").sdk_name == "openai"
    assert get_provider("doubao").sdk_name == "volcenginesdkarkruntime"
    assert get_provider("qwen").sdk_name == "dashscope"
    assert get_provider("deepseek").sdk_name == "openai"
    assert get_provider("doubao").client_thread_safe is False


@pytest.mark.parametrize(
    "old_name",
    ["openai_responses_text", "doubao_responses_web_search", "qwen_chat_enable_search", "qwen_responses_web_search_basic"],
)
def test_removed_adapter_names_are_not_runtime_aliases(old_name):
    with pytest.raises(ValueError, match="未知 adapter"):
        get_adapter(old_name)


def test_provider_specific_credentials_and_endpoints_are_independent():
    settings = Settings(
        llm_api_key="generic",
        llm_base_url="https://generic.example/v1",
        ark_api_key="ark",
        dashscope_api_key="qwen",
        deepseek_api_key="deepseek",
    )

    assert settings.provider_api_key("doubao").get_secret_value() == "ark"
    assert settings.provider_base_url("doubao") == "https://ark.cn-beijing.volces.com/api/v3"
    assert settings.provider_base_url("qwen") == "https://dashscope.aliyuncs.com/api/v1"
    assert settings.provider_base_url("deepseek") == "https://api.deepseek.com"
    assert settings.provider_base_url("openai_compatible") == "https://generic.example/v1"


def test_sampling_profile_fingerprints_actual_provider_endpoint():
    settings = Settings(llm_base_url="https://generic.example/v1", ark_api_key="ark")
    profile = build_sampling_profile(
        adapter_name="doubao_ark_responses_web_search",
        model="doubao-test",
        settings=settings,
    )

    assert profile["provider_sdk"] == "volcenginesdkarkruntime"
    assert profile["base_url_fingerprint"] == base_url_fingerprint("https://ark.cn-beijing.volces.com/api/v3")


@pytest.mark.parametrize(
    ("adapter_name", "model", "endpoint"),
    [
        ("doubao_ark_responses_web_search", "doubao-test", "https://ark.cn-beijing.volces.com/api/v3"),
        ("qwen_dashscope_generation_web_search", "qwen-plus", "https://dashscope.aliyuncs.com/api/v1"),
    ],
)
def test_native_profiles_freeze_official_endpoints_without_credentials(adapter_name, model, endpoint):
    profile = build_sampling_profile(adapter_name=adapter_name, model=model, settings=Settings())

    assert profile["base_url_fingerprint"] == base_url_fingerprint(endpoint)


def test_native_profile_build_rejects_wrong_api_family_endpoint_without_credentials():
    with pytest.raises(ValueError, match="compatible-mode"):
        build_sampling_profile(
            adapter_name="qwen_dashscope_generation_web_search",
            model="qwen-plus",
            settings=Settings(dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )


@pytest.mark.parametrize(
    ("adapter_name", "model", "settings"),
    [
        (
            "doubao_ark_responses_web_search",
            "doubao-test",
            Settings(ark_base_url="https://attacker.invalid/api/v3"),
        ),
        (
            "qwen_dashscope_generation_web_search",
            "qwen-plus",
            Settings(dashscope_base_url="https://attacker.invalid/api/v1"),
        ),
        (
            "qwen_dashscope_generation_web_search",
            "qwen-plus",
            Settings(dashscope_base_url="http://dashscope.aliyuncs.com/api/v1", allow_insecure_http=True),
        ),
    ],
)
def test_native_profile_build_never_sends_credentials_to_non_official_or_http_hosts(adapter_name, model, settings):
    with pytest.raises(ValueError, match="官方|HTTP"):
        build_sampling_profile(adapter_name=adapter_name, model=model, settings=settings)


def test_qwen_native_provider_accepts_official_workspace_endpoint():
    settings = Settings(dashscope_base_url="https://workspace.cn-beijing.maas.aliyuncs.com/api/v1")

    get_provider("qwen").validate_endpoint(settings)


def test_analysis_cache_identity_includes_provider_profile():
    common = {
        "response_text_hash_value": "response",
        "schema_version": "schema",
        "extractor_model": "same-model-name",
    }

    assert extraction_cache_key(**common, analysis_fingerprint="provider-a") != extraction_cache_key(
        **common,
        analysis_fingerprint="provider-b",
    )


def test_qwen_native_client_passes_transport_fields_outside_payload():
    captured = {}

    class FakeGeneration:
        @staticmethod
        def call(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(status_code=200, output={"choices": [{"finish_reason": "stop"}]})

    client = DashScopeGenerationClient(FakeGeneration, "secret", "https://dashscope.aliyuncs.com/api/v1", 17)
    client.call({"model": "qwen-plus", "messages": [{"role": "user", "content": "hi"}]})

    assert captured["base_address"].endswith("/api/v1")
    assert captured["request_timeout"] == 17
    assert "timeout" not in captured
    assert captured["api_key"] == "secret"


def test_qwen_native_client_converts_non_200_response():
    class FakeGeneration:
        @staticmethod
        def call(**kwargs):
            return SimpleNamespace(status_code=429, code="Throttled", request_id="req-1", message="slow down")

    client = DashScopeGenerationClient(FakeGeneration, "secret", "https://dashscope.aliyuncs.com/api/v1", 17)

    with pytest.raises(ProviderResponseError) as caught:
        client.call({"model": "qwen-plus", "messages": [{"role": "user", "content": "hi"}]})

    assert caught.value.status_code == 429
    assert is_retryable_api_error(caught.value)


def test_qwen_native_client_rejects_partial_200_response():
    class FakeGeneration:
        @staticmethod
        def call(**kwargs):
            return SimpleNamespace(
                status_code=200,
                request_id="req-partial",
                headers={"X-DashScope-PartialResponse": "true"},
            )

    client = DashScopeGenerationClient(FakeGeneration, "secret", "https://dashscope.aliyuncs.com/api/v1", 17)

    with pytest.raises(ProviderTransportError) as caught:
        client.call({"model": "qwen-plus", "messages": [{"role": "user", "content": "hi"}]})

    assert caught.value.retryable is True
    assert caught.value.request_id == "req-partial"


@pytest.mark.parametrize(
    ("finish_reason", "error_type", "retryable"),
    [
        (None, ProviderTransportError, True),
        ("length", ProviderResponseError, False),
        ("content_filter", ProviderResponseError, False),
        ("insufficient_system_resource", ProviderTransportError, True),
    ],
)
def test_qwen_native_client_rejects_incomplete_finish_reasons(finish_reason, error_type, retryable):
    class FakeGeneration:
        @staticmethod
        def call(**kwargs):
            return SimpleNamespace(
                status_code=200,
                request_id="req-incomplete",
                headers={},
                output={"choices": [{"finish_reason": finish_reason}]},
            )

    client = DashScopeGenerationClient(FakeGeneration, "secret", "https://dashscope.aliyuncs.com/api/v1", 17)

    with pytest.raises(error_type) as caught:
        client.call({"model": "qwen-plus", "messages": [{"role": "user", "content": "hi"}]})

    assert is_retryable_api_error(caught.value) is retryable


def test_qwen_adapter_normalizes_real_sdk_response_object():
    response_module = pytest.importorskip("dashscope.api_entities.dashscope_response")
    response = response_module.GenerationResponse(
        200,
        request_id="req-sdk",
        output={
            "choices": [{"message": {"role": "assistant", "content": "answer [ref_1]"}}],
            "search_info": {
                "search_results": [{"title": "Official source", "url": "https://example.com/source"}]
            },
        },
        usage={"total_tokens": 3},
    )
    settings = Settings()
    adapter = get_adapter("qwen_dashscope_generation_web_search")
    profile = build_sampling_profile(adapter_name=adapter.name, model="qwen-plus", settings=settings)
    request = adapter.build_request(QueryRecord(query_id="q-sdk", query="latest"), profile, settings, {})

    normalized = adapter.normalize_response(response, request)

    assert normalized.text == "answer [ref_1]"
    assert [source.url for source in normalized.sources] == ["https://example.com/source"]
    assert normalized.provider_meta == {"request_id": "req-sdk", "status_code": 200}
    assert normalized.web_search_performed is True


@pytest.mark.parametrize(
    ("provider_name", "settings"),
    [
        ("qwen", Settings(dashscope_api_key="secret", dashscope_base_url="https://dashscope.aliyuncs.com/api/v1?token=x")),
        ("doubao", Settings(ark_api_key="secret", ark_base_url="https://ark.cn-beijing.volces.com/api/v3?token=x")),
    ],
)
def test_native_provider_endpoints_reject_query_strings(provider_name, settings):
    with pytest.raises(ValueError, match="query"):
        get_provider(provider_name).create_client(settings)


def test_doubao_client_translates_private_sdk_errors():
    class ConnectionError(Exception):
        request_id = "req-ark"

    class TimeoutError(ConnectionError):
        pass

    class StatusError(Exception):
        status_code = 503
        request_id = "req-ark"
        code = "ServiceUnavailable"

    class Responses:
        def create(self, **payload):
            raise TimeoutError("timeout")

    sdk_client = SimpleNamespace(responses=Responses(), close=lambda: None)
    client = DoubaoArkClient(
        sdk_client,
        connection_error=ConnectionError,
        timeout_error=TimeoutError,
        status_error=StatusError,
    )

    with pytest.raises(ProviderTransportError) as caught:
        client.responses.create(model="doubao-test", input="hi")

    assert caught.value.provider == "doubao"
    assert caught.value.request_id == "req-ark"
    assert is_retryable_api_error(caught.value)


def test_doubao_provider_uses_official_sdk_contract(monkeypatch):
    captured = {}

    class FakeArk:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.responses = SimpleNamespace(create=lambda **payload: payload)

        def close(self):
            pass

    sdk_module = ModuleType("volcenginesdkarkruntime")
    sdk_module.Ark = FakeArk
    errors_module = ModuleType("volcenginesdkarkruntime._exceptions")
    errors_module.ArkAPIConnectionError = type("ArkAPIConnectionError", (Exception,), {})
    errors_module.ArkAPITimeoutError = type("ArkAPITimeoutError", (errors_module.ArkAPIConnectionError,), {})
    errors_module.ArkAPIStatusError = type("ArkAPIStatusError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "volcenginesdkarkruntime", sdk_module)
    monkeypatch.setitem(sys.modules, "volcenginesdkarkruntime._exceptions", errors_module)

    client = get_provider("doubao").create_client(Settings(ark_api_key="ark-secret"))

    assert isinstance(client, DoubaoArkClient)
    assert captured == {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "ark-secret",
        "timeout": 90,
        "max_retries": 0,
    }


def test_thread_unsafe_provider_gets_one_client_per_worker_thread():
    created = []

    class Client:
        def __init__(self, number):
            self.number = number
            self.closed = False

        def identity(self):
            return self.number

        def close(self):
            self.closed = True

    class Provider:
        client_thread_safe = False

        def create_client(self, settings):
            client = Client(len(created) + 1)
            created.append(client)
            return client

    runtime = create_runtime_client(Provider(), Settings(), concurrency=2)
    barrier = Barrier(2)

    def use_client() -> int:
        identity = runtime.identity()
        barrier.wait()
        return identity

    with ThreadPoolExecutor(max_workers=2) as executor:
        identities = list(executor.map(lambda _: use_client(), range(2)))
    runtime.close()

    assert len(created) == 2
    assert set(identities) == {1, 2}
    assert all(client.closed for client in created)


def test_deepseek_is_analysis_only_and_rejects_retired_models():
    settings = Settings(deepseek_api_key="secret")
    adapter = get_adapter("deepseek_chat_completions_text")

    with pytest.raises(ValueError, match="不支持模型"):
        build_sampling_profile(adapter_name=adapter.name, model="deepseek-chat", settings=settings, web_search_required=False)
    with pytest.raises(ValueError, match="不支持联网采样"):
        build_sampling_profile(adapter_name=adapter.name, model="deepseek-v4-flash", settings=settings, web_search_required=True)

    profile = build_sampling_profile(
        adapter_name=adapter.name,
        model="deepseek-v4-flash",
        settings=settings,
        web_search_required=False,
    )
    request = adapter.build_request(QueryRecord(query_id="analysis", query="Return JSON"), profile, settings, {})
    assert request.payload["response_format"] == {"type": "json_object"}
    assert request.payload["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "tools" not in request.payload


def test_doubao_text_analysis_disables_thinking():
    settings = Settings(ark_api_key="secret", analysis_max_output_tokens=128)
    adapter = get_adapter("doubao_ark_responses_text")
    profile = build_sampling_profile(
        adapter_name=adapter.name,
        model="doubao-seed-2-0-lite-260215",
        settings=settings,
        web_search_required=False,
    )

    request = adapter.build_request(QueryRecord(query_id="analysis", query="Return JSON"), profile, settings, {})

    assert request.payload["thinking"] == {"type": "disabled"}
    assert request.payload["max_output_tokens"] == 128


def test_native_provider_key_is_sufficient_for_live_job_preflight(tmp_path, monkeypatch):
    settings = Settings(ark_api_key="ark-secret", max_output_tokens=128)
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    config.write_text(
        json.dumps(
            {
                "target_brand": "TestBrand",
                "industry": "TestIndustry",
                "queries": ["best providers"],
                "repeats": 1,
                "model": "doubao-test",
                "adapter": "doubao_ark_responses_web_search",
            }
        ),
        encoding="utf-8",
    )
    build_job_bundle(config, bundle, settings=settings)

    class FakeClient:
        def __init__(self):
            self.responses = SimpleNamespace(create=self.create_response)
            self.closed = False

        def create_response(self, **payload):
            return {
                "status": "completed",
                "output_text": "TestBrand",
                "output": [{"type": "web_search_call"}],
                "usage": {"total_tokens": 2},
            }

        def close(self):
            self.closed = True

    client = FakeClient()
    monkeypatch.setattr(
        "geo_monitor.runner.create_runtime_client",
        lambda provider, runtime_settings, *, concurrency: client,
    )

    result = run_job_bundle(bundle, settings=settings, confirm_cost=True)

    assert result["errors"] == 0
    assert result["completed_units"] == 1
    assert client.closed is True


def test_analysis_estimate_reports_analysis_provider_model(tmp_path):
    config = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    config.write_text(
        json.dumps(
            {
                "target_brand": "TestBrand",
                "industry": "TestIndustry",
                "queries": ["best providers"],
                "repeats": 1,
                "model": "sampling-model",
                "analysis_model": "deepseek-v4-flash",
                "analysis_adapter": "deepseek_chat_completions_text",
            }
        ),
        encoding="utf-8",
    )
    build_job_bundle(config, bundle, settings=Settings())
    run_job_bundle(bundle, settings=Settings(), mock=True)

    estimate = estimate_job_analysis(bundle, include_mock=True)

    assert estimate["model"] == "deepseek-v4-flash"
    assert estimate["analysis_profile"]["provider"] == "deepseek"
