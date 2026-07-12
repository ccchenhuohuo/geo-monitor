from __future__ import annotations

from typing import Any

from ..config import Settings
from ..schemas import QueryRecord
from .base import AdapterCapabilities, BaseAdapter, ProviderRequest


class DoubaoResponsesWebSearchAdapter(BaseAdapter):
    name = "doubao_responses_web_search"
    provider = "doubao"
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        api_family="responses",
        supported_model_patterns=("*",),
        supports_sources="partial",
        supports_search_trace="partial",
        source_grain="url",
    )
    allowed_options = {"web_search_options", "include", "tool_choice", "max_tool_calls"}

    def validate_options(self, options: dict[str, Any]) -> None:
        super().validate_options(options)
        web_search_options = self._require_object_option(options, "web_search_options")
        reserved = sorted(set(web_search_options) & {"type"})
        if reserved:
            raise ValueError(f"{self.name} adapter_options.web_search_options 不能覆盖保留字段：{', '.join(reserved)}")
        self._required_web_search_tool_choice(options)
        self._string_list_option(options, "include")
        if "max_tool_calls" in options:
            self._positive_int_option(options, "max_tool_calls", 1, maximum=10)

    def build_request(
        self,
        query_record: QueryRecord,
        sampling_profile: dict[str, Any],
        settings: Settings,
        adapter_options: dict[str, Any],
    ) -> ProviderRequest:
        self.validate_options(adapter_options)
        tool: dict[str, Any] = {"type": "web_search"}
        web_search_options = self._require_object_option(adapter_options, "web_search_options")
        if web_search_options:
            tool.update(web_search_options)
        tool_choice = self._required_web_search_tool_choice(adapter_options) or ("required" if sampling_profile.get("web_search_required", True) else "auto")
        payload: dict[str, Any] = {
            "model": sampling_profile["model"],
            "input": query_record.query,
            "tools": [tool],
            "tool_choice": tool_choice,
            "max_tool_calls": self._positive_int_option(adapter_options, "max_tool_calls", settings.max_tool_calls, maximum=10),
            "max_output_tokens": settings.max_output_tokens,
        }
        include = self._string_list_option(adapter_options, "include")
        if include is not None:
            payload["include"] = include
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        return client.responses.create(**request.payload)
