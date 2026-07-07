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

    def build_request(
        self,
        query_record: QueryRecord,
        sampling_profile: dict[str, Any],
        settings: Settings,
        adapter_options: dict[str, Any],
    ) -> ProviderRequest:
        self.validate_options(adapter_options)
        tool: dict[str, Any] = {"type": "web_search"}
        if isinstance(adapter_options.get("web_search_options"), dict):
            tool.update(adapter_options["web_search_options"])
        payload: dict[str, Any] = {
            "model": sampling_profile["model"],
            "input": query_record.query,
            "tools": [tool],
            "max_tool_calls": int(adapter_options.get("max_tool_calls") or settings.max_tool_calls),
        }
        for key in ["include", "tool_choice"]:
            if key in adapter_options:
                payload[key] = adapter_options[key]
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        return client.responses.create(**request.payload)

