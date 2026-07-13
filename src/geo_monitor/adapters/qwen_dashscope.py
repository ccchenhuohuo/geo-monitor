"""Qwen adapters using the native DashScope Generation API."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..response_parser import response_to_dict
from ..schemas import QueryRecord
from .base import AdapterCapabilities, BaseAdapter, NormalizedProviderResponse, ProviderRequest

QWEN_DASHSCOPE_TEXT_MODELS = (
    "qwen-plus",
    "qwen-plus-*",
    "qwen-max",
    "qwen-max-*",
    "qwen-flash",
    "qwen-flash-*",
    "qwen-turbo",
    "qwen-turbo-*",
)

QWEN_DASHSCOPE_SEARCH_MODELS = (
    "qwen-plus",
    "qwen-plus-latest",
    "qwen-max",
    "qwen-max-latest",
    "qwen-flash",
    "qwen-flash-latest",
    "qwen-turbo",
)

QWEN_SEARCH_OPTION_KEYS = {
    "assigned_site_list",
    "citation_format",
    "enable_citation",
    "enable_search_extension",
    "enable_source",
    "forced_search",
    "freshness",
    "intention_options",
    "search_strategy",
}


class QwenDashScopeGenerationTextAdapter(BaseAdapter):
    name = "qwen_dashscope_generation_text"
    provider = "qwen"
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        api_family="dashscope_generation",
        supported_model_patterns=QWEN_DASHSCOPE_TEXT_MODELS,
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
            "result_format": "message",
            "max_tokens": settings.analysis_max_output_tokens,
        }
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        return client.call(request.payload)

    def normalize_response(self, response: Any, request: ProviderRequest) -> NormalizedProviderResponse:
        raw = _dashscope_raw(response)
        normalized = super().normalize_response(raw, request)
        return _with_dashscope_meta(normalized, raw)


class QwenDashScopeGenerationSearchAdapter(QwenDashScopeGenerationTextAdapter):
    name = "qwen_dashscope_generation_web_search"
    capabilities = AdapterCapabilities(
        api_family="dashscope_generation",
        supported_model_patterns=QWEN_DASHSCOPE_SEARCH_MODELS,
        supports_forced_search=True,
        supports_sources=True,
        supports_search_trace=True,
        source_grain="url",
    )
    allowed_options = {"search_options"}

    def validate_options(self, options: dict[str, Any]) -> None:
        super().validate_options(options)
        search_options = self._require_object_option(options, "search_options")
        unknown = sorted(set(search_options) - QWEN_SEARCH_OPTION_KEYS)
        if unknown:
            raise ValueError(f"{self.name} adapter_options.search_options 包含未知字段：{', '.join(unknown)}")
        for key in ("forced_search", "enable_source", "enable_citation", "enable_search_extension"):
            if key in search_options and not isinstance(search_options[key], bool):
                raise ValueError(f"{self.name} adapter_options.search_options.{key} 必须是布尔值")
        strategy = search_options.get("search_strategy", "turbo")
        if strategy not in {"turbo", "max"}:
            raise ValueError(f"{self.name} adapter_options.search_options.search_strategy 必须是 turbo 或 max")
        if "freshness" in search_options:
            freshness = search_options["freshness"]
            if isinstance(freshness, bool) or freshness not in {7, 30, 180, 365}:
                raise ValueError(f"{self.name} adapter_options.search_options.freshness 必须是 7、30、180 或 365")
            if strategy != "turbo":
                raise ValueError(f"{self.name} adapter_options.search_options.freshness 仅适用于 search_strategy=turbo")
        if "assigned_site_list" in search_options:
            sites = search_options["assigned_site_list"]
            if not isinstance(sites, list) or len(sites) > 25 or any(not isinstance(site, str) or not site.strip() for site in sites):
                raise ValueError(f"{self.name} adapter_options.search_options.assigned_site_list 必须是不超过 25 项的非空字符串数组")
            if strategy != "turbo":
                raise ValueError(f"{self.name} adapter_options.search_options.assigned_site_list 仅适用于 search_strategy=turbo")
        citation_format = search_options.get("citation_format")
        if citation_format is not None and citation_format not in {"[<number>]", "[ref_<number>]"}:
            raise ValueError(
                f"{self.name} adapter_options.search_options.citation_format 必须是 '[<number>]' 或 '[ref_<number>]'"
            )
        intention_options = search_options.get("intention_options")
        if intention_options is not None:
            if not isinstance(intention_options, dict) or set(intention_options) != {"prompt_intervene"}:
                raise ValueError(
                    f"{self.name} adapter_options.search_options.intention_options 必须只包含 prompt_intervene"
                )
            prompt_intervene = intention_options["prompt_intervene"]
            if not isinstance(prompt_intervene, str) or not prompt_intervene.strip():
                raise ValueError(
                    f"{self.name} adapter_options.search_options.intention_options.prompt_intervene 必须是非空字符串"
                )
        if search_options.get("enable_citation") is True and search_options.get("enable_source", True) is not True:
            raise ValueError(f"{self.name} adapter_options.search_options.enable_citation=true 要求 enable_source=true")

    def build_request(
        self,
        query_record: QueryRecord,
        sampling_profile: dict[str, Any],
        settings: Settings,
        adapter_options: dict[str, Any],
    ) -> ProviderRequest:
        self.validate_options(adapter_options)
        search_options = {
            "forced_search": bool(sampling_profile.get("web_search_required", True)),
            "enable_source": True,
            "enable_citation": True,
            "citation_format": "[ref_<number>]",
        }
        search_options.update(self._require_object_option(adapter_options, "search_options"))
        if sampling_profile.get("web_search_required", True) and not search_options["forced_search"]:
            raise ValueError(f"{self.name} 要求联网搜索时 search_options.forced_search 不能为 false")
        if sampling_profile.get("web_search_required", True) and not search_options["enable_source"]:
            raise ValueError(f"{self.name} 要求联网搜索时 search_options.enable_source 不能为 false")
        if search_options["enable_citation"] and not search_options["enable_source"]:
            raise ValueError(f"{self.name} search_options.enable_citation=true 要求 enable_source=true")
        payload = {
            "model": sampling_profile["model"],
            "messages": [{"role": "user", "content": query_record.query}],
            "result_format": "message",
            "enable_search": True,
            "search_options": search_options,
            "max_tokens": settings.max_output_tokens,
        }
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload),
        )


def _dashscope_raw(response: Any) -> dict[str, Any]:
    return dict(response_to_dict(response))


def _with_dashscope_meta(normalized: NormalizedProviderResponse, raw: dict[str, Any]) -> NormalizedProviderResponse:
    return NormalizedProviderResponse(
        text=normalized.text,
        sources=normalized.sources,
        usage=normalized.usage,
        raw=normalized.raw,
        provider_meta={
            key: raw[key]
            for key in ("request_id", "status_code", "code")
            if raw.get(key) not in (None, "")
        },
        web_search_performed=normalized.web_search_performed,
        web_search_evidence=normalized.web_search_evidence,
        web_search_requirement_status=normalized.web_search_requirement_status,
        source_parse_status=normalized.source_parse_status,
    )


__all__ = ["QwenDashScopeGenerationSearchAdapter", "QwenDashScopeGenerationTextAdapter"]
