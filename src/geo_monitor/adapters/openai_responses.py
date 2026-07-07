from __future__ import annotations

from typing import Any

from ..config import Settings
from ..schemas import QueryRecord
from .base import AdapterCapabilities, BaseAdapter, ProviderRequest


class OpenAIResponsesWebSearchAdapter(BaseAdapter):
    name = "openai_responses_web_search"
    provider = "openai_compatible"
    adapter_version = "1"
    capabilities = AdapterCapabilities(api_family="responses", source_grain="url")
    allowed_options: set[str] = set()

    def build_request(
        self,
        query_record: QueryRecord,
        sampling_profile: dict[str, Any],
        settings: Settings,
        adapter_options: dict[str, Any],
    ) -> ProviderRequest:
        self.validate_options(adapter_options)
        limit_value = int(sampling_profile.get("web_search_limit") or settings.web_search_limit)
        if limit_value < 1 or limit_value > 20:
            raise ValueError("web_search_limit 必须在 1 到 20 之间")
        payload = {
            "model": sampling_profile["model"],
            "input": query_record.query,
            "tools": [{"type": "web_search", "limit": limit_value}],
            "max_tool_calls": settings.max_tool_calls,
        }
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        if hasattr(client, "create_response"):
            return client.create_response(request.payload)
        return client.responses.create(**request.payload)


class OpenAIResponsesTextAdapter(BaseAdapter):
    name = "openai_responses_text"
    provider = "openai_compatible"
    adapter_version = "1"
    capabilities = AdapterCapabilities(api_family="responses", source_grain="none")
    allowed_options: set[str] = set()

    def build_request(
        self,
        query_record: QueryRecord,
        sampling_profile: dict[str, Any],
        settings: Settings,
        adapter_options: dict[str, Any],
    ) -> ProviderRequest:
        self.validate_options(adapter_options)
        payload = {"model": sampling_profile["model"], "input": query_record.query}
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        if hasattr(client, "create_response"):
            return client.create_response(request.payload)
        return client.responses.create(**request.payload)
