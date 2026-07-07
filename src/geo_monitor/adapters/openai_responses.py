from __future__ import annotations

from typing import Any

from ..config import Settings
from ..request_fingerprint import legacy_payload_hash
from ..schemas import QueryRecord
from .base import AdapterCapabilities, BaseAdapter, ProviderRequest


class OpenAIResponsesWebSearchAdapter(BaseAdapter):
    name = "openai_responses_web_search"
    provider = "openai_compatible"
    adapter_version = "1"
    capabilities = AdapterCapabilities(api_family="responses", source_grain="url")
    allowed_options = {
        "external_web_access",
        "filters",
        "include",
        "return_token_budget",
        "search_context_size",
        "tool_choice",
        "user_location",
    }

    def validate_options(self, options: dict[str, Any]) -> None:
        super().validate_options(options)
        tool_choice = options.get("tool_choice")
        if isinstance(tool_choice, str) and tool_choice in {"auto", "none"}:
            raise ValueError("openai_responses_web_search 要求联网搜索，tool_choice 不能是 auto 或 none")
        if isinstance(tool_choice, dict) and str(tool_choice.get("type") or "") != "web_search":
            raise ValueError("openai_responses_web_search tool_choice 必须指向 web_search")

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
        payload = {
            "model": sampling_profile["model"],
            "input": query_record.query,
            "tools": [tool],
            "tool_choice": adapter_options.get("tool_choice") or ("required" if sampling_profile.get("web_search_required", True) else "auto"),
            "include": adapter_options.get("include", ["web_search_call.action.sources"]),
            "max_tool_calls": settings.max_tool_calls,
        }
        legacy_payload = {
            "model": sampling_profile["model"],
            "input": query_record.query,
            "tools": [{"type": "web_search", "limit": limit_value}],
            "max_tool_calls": settings.max_tool_calls,
        }
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
            legacy_request_hashes=(legacy_payload_hash(legacy_payload),),
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
