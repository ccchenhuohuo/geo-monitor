from __future__ import annotations

from typing import Any

from ..config import Settings
from ..schemas import QueryRecord
from .base import AdapterCapabilities, BaseAdapter, ProviderRequest


class QwenResponsesWebSearchBasicAdapter(BaseAdapter):
    name = "qwen_responses_web_search_basic"
    provider = "qwen"
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        api_family="responses",
        supported_model_patterns=("qwen3.7*", "qwen3.6*", "qwen3.5*", "qwen3-max*", "qwen-max*"),
        supports_forced_search=False,
        supports_sources="partial",
        supports_search_trace="partial",
        source_grain="url",
    )
    allowed_options = {"include", "max_tool_calls"}

    def validate_options(self, options: dict[str, Any]) -> None:
        super().validate_options(options)
        if "max_tool_calls" in options:
            self._positive_int_option(options, "max_tool_calls", 1)

    def build_request(
        self,
        query_record: QueryRecord,
        sampling_profile: dict[str, Any],
        settings: Settings,
        adapter_options: dict[str, Any],
    ) -> ProviderRequest:
        self.validate_options(adapter_options)
        payload: dict[str, Any] = {
            "model": sampling_profile["model"],
            "input": query_record.query,
            "tools": [{"type": "web_search"}],
            "max_tool_calls": self._positive_int_option(adapter_options, "max_tool_calls", settings.max_tool_calls),
        }
        if "include" in adapter_options:
            payload["include"] = adapter_options["include"]
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        return client.responses.create(**request.payload)
