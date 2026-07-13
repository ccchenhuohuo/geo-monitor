from __future__ import annotations

from typing import Any

from ..config import Settings
from ..schemas import QueryRecord
from .base import AdapterCapabilities, BaseAdapter, ProviderRequest


class DoubaoArkResponsesWebSearchAdapter(BaseAdapter):
    name = "doubao_ark_responses_web_search"
    provider = "doubao"
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        api_family="responses",
        supported_model_patterns=("*",),
        supports_forced_search=True,
        supports_sources="partial",
        supports_search_trace="partial",
        source_grain="url",
        supports_search_limit=True,
    )
    allowed_options = {"max_keyword", "max_tool_calls", "sources", "tool_choice", "user_location"}

    def validate_options(self, options: dict[str, Any]) -> None:
        super().validate_options(options)
        self._required_web_search_tool_choice(options)
        sources = self._string_list_option(options, "sources", default=["search_engine"])
        allowed_sources = {"toutiao", "douyin", "moji", "search_engine"}
        if not sources or set(sources) - allowed_sources:
            raise ValueError(f"{self.name} adapter_options.sources 只能包含 toutiao/douyin/moji/search_engine 且不能为空")
        if "max_keyword" in options:
            self._positive_int_option(options, "max_keyword", 1)
        user_location = self._require_object_option(options, "user_location")
        if "user_location" in options:
            unknown_location = sorted(set(user_location) - {"type", "city", "country", "region", "timezone"})
            if unknown_location:
                raise ValueError(f"{self.name} adapter_options.user_location 包含未知字段：{', '.join(unknown_location)}")
            if user_location.get("type") != "approximate":
                raise ValueError(f"{self.name} adapter_options.user_location.type 必须是 approximate")
            for key in ("city", "country", "region"):
                if key in user_location and not isinstance(user_location[key], str):
                    raise ValueError(f"{self.name} adapter_options.user_location.{key} 必须是字符串")
            timezone = user_location.get("timezone")
            if timezone is not None and (isinstance(timezone, bool) or not isinstance(timezone, (int, float))):
                raise ValueError(f"{self.name} adapter_options.user_location.timezone 必须是数值")
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
        limit_value = int(sampling_profile.get("web_search_limit", settings.web_search_limit))
        if limit_value < 1 or limit_value > 20:
            raise ValueError("web_search_limit 必须在 1 到 20 之间")
        tool: dict[str, Any] = {
            "type": "web_search",
            "sources": self._string_list_option(adapter_options, "sources", default=["search_engine"]),
            "limit": limit_value,
        }
        if "max_keyword" in adapter_options:
            tool["max_keyword"] = self._positive_int_option(adapter_options, "max_keyword", 1)
        if "user_location" in adapter_options:
            tool["user_location"] = self._require_object_option(adapter_options, "user_location")
        tool_choice = self._required_web_search_tool_choice(adapter_options) or (
            {"type": "web_search"} if sampling_profile.get("web_search_required", True) else "auto"
        )
        payload: dict[str, Any] = {
            "model": sampling_profile["model"],
            "input": query_record.query,
            "tools": [tool],
            "tool_choice": tool_choice,
            "max_tool_calls": self._positive_int_option(adapter_options, "max_tool_calls", settings.max_tool_calls, maximum=10),
            "max_output_tokens": settings.max_output_tokens,
        }
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        return client.responses.create(**request.payload)


class DoubaoArkResponsesTextAdapter(BaseAdapter):
    name = "doubao_ark_responses_text"
    provider = "doubao"
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
            "thinking": {"type": "disabled"},
        }
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        return client.responses.create(**request.payload)
