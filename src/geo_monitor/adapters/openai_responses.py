from __future__ import annotations

from typing import Any

from ..config import Settings
from ..request_fingerprint import legacy_payload_hash, request_fingerprint
from ..schemas import QueryRecord
from .base import AdapterCapabilities, BaseAdapter, ProviderRequest


class OpenAICompatibleResponsesWebSearchAdapter(BaseAdapter):
    name = "openai_compatible_responses_web_search"
    provider = "openai_compatible"
    adapter_version = "1"
    capabilities = AdapterCapabilities(api_family="responses", source_grain="url")
    allowed_options = {
        "external_web_access",
        "filters",
        "include",
        "max_tool_calls",
        "return_token_budget",
        "search_context_size",
        "tool_choice",
        "user_location",
    }

    def validate_options(self, options: dict[str, Any]) -> None:
        super().validate_options(options)
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
        limit_value = int(sampling_profile.get("web_search_limit") or settings.web_search_limit)
        if limit_value < 1 or limit_value > 20:
            raise ValueError("web_search_limit 必须在 1 到 20 之间")
        tool: dict[str, Any] = {"type": "web_search"}
        for key in ["external_web_access", "filters", "return_token_budget", "search_context_size", "user_location"]:
            if key in adapter_options:
                tool[key] = adapter_options[key]
        tool_choice = self._required_web_search_tool_choice(adapter_options) or ("required" if sampling_profile.get("web_search_required", True) else "auto")
        include = self._string_list_option(adapter_options, "include")
        payload: dict[str, Any] = {
            "model": sampling_profile["model"],
            "input": query_record.query,
            "tools": [tool],
            "tool_choice": tool_choice,
            "max_tool_calls": self._positive_int_option(adapter_options, "max_tool_calls", settings.max_tool_calls, maximum=10),
            "max_output_tokens": settings.max_output_tokens,
        }
        if include is not None:
            payload["include"] = include
        legacy_payload = {
            "model": sampling_profile["model"],
            "input": query_record.query,
            "tools": [{"type": "web_search", "limit": limit_value}],
            "max_tool_calls": payload["max_tool_calls"],
        }
        legacy_fingerprint_basis = self._fingerprint_basis(query_record, sampling_profile, payload)
        legacy_fingerprint_basis["adapter"] = "openai_responses_web_search"
        legacy_fingerprint_basis.pop("provider_sdk", None)
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
            legacy_request_hashes=(
                legacy_payload_hash(legacy_payload),
                request_fingerprint(legacy_fingerprint_basis),
            ),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        if hasattr(client, "create_response"):
            return client.create_response(request.payload)
        return client.responses.create(**request.payload)


class OpenAICompatibleResponsesTextAdapter(BaseAdapter):
    name = "openai_compatible_responses_text"
    provider = "openai_compatible"
    adapter_version = "1"
    capabilities = AdapterCapabilities(api_family="responses", source_grain="none", supports_text_analysis=True)
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
            "input": query_record.query,
            "max_output_tokens": settings.analysis_max_output_tokens,
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
