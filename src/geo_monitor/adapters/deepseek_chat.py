"""DeepSeek Chat Completions adapters."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..schemas import QueryRecord
from .base import AdapterCapabilities, BaseAdapter, ProviderRequest


class DeepSeekChatCompletionsTextAdapter(BaseAdapter):
    name = "deepseek_chat_completions_text"
    provider = "deepseek"
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        api_family="chat_completions",
        supported_model_patterns=("deepseek-v4-flash", "deepseek-v4-pro"),
        supports_forced_search=False,
        supports_sources=False,
        supports_search_trace=False,
        source_grain="none",
        supports_text_analysis=True,
    )
    allowed_options: set[str] = set()

    def build_request(
        self,
        query_record: QueryRecord,
        sampling_profile: dict[str, Any],
        settings: Settings,
        adapter_options: dict[str, Any],
    ) -> ProviderRequest:
        self.validate_options(adapter_options)
        payload = {
            "model": sampling_profile["model"],
            "messages": [{"role": "user", "content": query_record.query}],
            "max_tokens": settings.analysis_max_output_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        return client.chat.completions.create(**request.payload)


__all__ = ["DeepSeekChatCompletionsTextAdapter"]
