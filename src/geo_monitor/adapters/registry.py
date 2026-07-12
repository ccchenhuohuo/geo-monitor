from __future__ import annotations

from typing import Any

from ..config import Settings
from ..request_fingerprint import REQUEST_FINGERPRINT_VERSION, base_url_fingerprint
from .base import AdapterCapabilities, ProviderAdapter
from .doubao_responses import DoubaoResponsesWebSearchAdapter
from .openai_responses import OpenAIResponsesTextAdapter, OpenAIResponsesWebSearchAdapter
from .qwen_chat import QwenChatEnableSearchAdapter
from .qwen_responses import QwenResponsesWebSearchBasicAdapter

_ADAPTERS: dict[str, ProviderAdapter] = {
    "openai_responses_web_search": OpenAIResponsesWebSearchAdapter(),
    "openai_responses_text": OpenAIResponsesTextAdapter(),
    "doubao_responses_web_search": DoubaoResponsesWebSearchAdapter(),
    "qwen_responses_web_search_basic": QwenResponsesWebSearchBasicAdapter(),
    "qwen_chat_enable_search": QwenChatEnableSearchAdapter(),
}


def get_adapter(name: str) -> ProviderAdapter:
    key = str(name or "").strip() or "openai_responses_web_search"
    try:
        return _ADAPTERS[key]
    except KeyError as exc:
        raise ValueError(f"未知 adapter：{key}") from exc


def get_capabilities(name: str) -> AdapterCapabilities:
    return get_adapter(name).capabilities


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
    return {
        "provider": adapter.provider,
        "adapter": adapter.name,
        "adapter_version": adapter.adapter_version,
        "api_family": adapter.capabilities.api_family,
        "model": model_text,
        "base_url_fingerprint": base_url_fingerprint(settings.llm_base_url),
        "request_fingerprint_version": REQUEST_FINGERPRINT_VERSION,
        "web_search_required": bool(web_search_required),
        "source_grain": adapter.capabilities.source_grain,
        # Retained for manifest compatibility. No current provider adapter exposes
        # a semantically equivalent hard result-count parameter, so it must not be
        # treated as an effective request condition.
        "web_search_limit": web_search_limit if web_search_limit is not None else settings.web_search_limit,
        "web_search_limit_effective": False,
        "max_tool_calls": settings.max_tool_calls,
        "max_output_tokens": (settings.analysis_max_output_tokens if adapter.name == "openai_responses_text" else settings.max_output_tokens),
    }
