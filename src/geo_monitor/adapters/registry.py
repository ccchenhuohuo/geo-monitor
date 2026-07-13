from __future__ import annotations

from typing import Any

from ..config import Settings
from ..providers import get_provider
from ..request_fingerprint import REQUEST_FINGERPRINT_VERSION, base_url_fingerprint
from .base import AdapterCapabilities, ProviderAdapter
from .deepseek_chat import DeepSeekChatCompletionsTextAdapter
from .doubao_responses import DoubaoArkResponsesTextAdapter, DoubaoArkResponsesWebSearchAdapter
from .openai_responses import OpenAICompatibleResponsesTextAdapter, OpenAICompatibleResponsesWebSearchAdapter
from .qwen_dashscope import QwenDashScopeGenerationSearchAdapter, QwenDashScopeGenerationTextAdapter

_ADAPTERS: dict[str, ProviderAdapter] = {
    "openai_compatible_responses_web_search": OpenAICompatibleResponsesWebSearchAdapter(),
    "openai_compatible_responses_text": OpenAICompatibleResponsesTextAdapter(),
    "doubao_ark_responses_web_search": DoubaoArkResponsesWebSearchAdapter(),
    "doubao_ark_responses_text": DoubaoArkResponsesTextAdapter(),
    "qwen_dashscope_generation_web_search": QwenDashScopeGenerationSearchAdapter(),
    "qwen_dashscope_generation_text": QwenDashScopeGenerationTextAdapter(),
    "deepseek_chat_completions_text": DeepSeekChatCompletionsTextAdapter(),
}


def get_adapter(name: str) -> ProviderAdapter:
    key = str(name or "").strip() or "openai_compatible_responses_web_search"
    try:
        return _ADAPTERS[key]
    except KeyError as exc:
        raise ValueError(f"未知 adapter：{key}") from exc


def get_capabilities(name: str) -> AdapterCapabilities:
    return get_adapter(name).capabilities


def validate_adapter_profile_identity(profile: dict[str, Any], *, purpose: str) -> ProviderAdapter:
    adapter = get_adapter(str(profile.get("adapter") or ""))
    provider = get_provider(adapter.provider)
    expected = {
        "provider": adapter.provider,
        "adapter_version": adapter.adapter_version,
        "api_family": adapter.capabilities.api_family,
        "provider_sdk": provider.sdk_name,
    }
    for key, value in expected.items():
        if profile.get(key) != value:
            raise ValueError(f"profile identity mismatch: {key} 应为 {value!r}")
    if "source_grain" in profile and profile.get("source_grain") != adapter.capabilities.source_grain:
        raise ValueError(f"profile identity mismatch: source_grain 应为 {adapter.capabilities.source_grain!r}")
    model = str(profile.get("model") or "").strip()
    if not model or not adapter.capabilities.supports_model(model):
        raise ValueError(f"profile identity mismatch: adapter {adapter.name} 不支持模型 {model!r}")
    if purpose == "sampling" and adapter.capabilities.source_grain == "none":
        raise ValueError(f"{adapter.name} 不是联网采样 adapter")
    if purpose == "sampling" and profile.get("web_search_required") is not True:
        raise ValueError("profile identity mismatch: web_search_required 必须严格为 true")
    if purpose == "sampling" and profile.get("web_search_limit_effective") is not adapter.capabilities.supports_search_limit:
        raise ValueError(
            "profile identity mismatch: web_search_limit_effective "
            f"应为 {adapter.capabilities.supports_search_limit!r}"
        )
    if purpose == "sampling" and profile.get("request_fingerprint_version") != REQUEST_FINGERPRINT_VERSION:
        raise ValueError(
            f"profile identity mismatch: request_fingerprint_version 应为 {REQUEST_FINGERPRINT_VERSION!r}"
        )
    if purpose == "analysis" and not adapter.capabilities.supports_text_analysis:
        raise ValueError(f"{adapter.name} 不是文本分析 adapter")
    return adapter


def build_sampling_profile(
    *,
    adapter_name: str,
    model: str,
    settings: Settings,
    web_search_limit: int | None = None,
    web_search_required: bool = True,
) -> dict[str, Any]:
    adapter = get_adapter(adapter_name)
    model_text = str(model or "").strip()
    if not model_text:
        raise ValueError("model 不能为空")
    if not adapter.capabilities.supports_model(model_text):
        patterns = ", ".join(adapter.capabilities.supported_model_patterns)
        raise ValueError(f"{adapter.name} 不支持模型 {model_text!r}；支持模式：{patterns}")
    if web_search_required and adapter.capabilities.source_grain == "none":
        raise ValueError(f"{adapter.name} 不支持联网采样；该 adapter 仅用于文本分析")
    limit_value = settings.web_search_limit if web_search_limit is None else web_search_limit
    if isinstance(limit_value, bool) or not isinstance(limit_value, int) or not 1 <= limit_value <= 20:
        raise ValueError("web_search_limit 必须是 1 到 20 之间的整数")
    provider = get_provider(adapter.provider)
    if adapter.provider != "openai_compatible":
        provider.validate_endpoint(settings)
    profile = {
        "provider": adapter.provider,
        "adapter": adapter.name,
        "adapter_version": adapter.adapter_version,
        "api_family": adapter.capabilities.api_family,
        "provider_sdk": provider.sdk_name,
        "model": model_text,
        "base_url_fingerprint": base_url_fingerprint(provider.endpoint_url(settings)),
        "request_fingerprint_version": REQUEST_FINGERPRINT_VERSION,
        "web_search_required": bool(web_search_required),
        "source_grain": adapter.capabilities.source_grain,
        # Kept in every profile for cross-provider comparison; adapters explicitly
        # declare whether the value reaches the provider request.
        "web_search_limit": limit_value,
        "web_search_limit_effective": adapter.capabilities.supports_search_limit,
        "max_output_tokens": (settings.analysis_max_output_tokens if adapter.capabilities.supports_text_analysis else settings.max_output_tokens),
    }
    if adapter.capabilities.api_family == "responses" and not adapter.capabilities.supports_text_analysis:
        profile["max_tool_calls"] = settings.max_tool_calls
    return profile
