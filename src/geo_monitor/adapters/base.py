from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Protocol

from ..config import Settings
from ..request_fingerprint import REQUEST_FINGERPRINT_VERSION, request_fingerprint
from ..response_parser import parse_response, response_to_dict
from ..schemas import QueryRecord, SourceRecord

WEB_SEARCH_EVIDENCE_PROVIDER_TRACE = "provider_trace"
WEB_SEARCH_EVIDENCE_TOOL_USAGE_COUNT = "tool_usage_count"
WEB_SEARCH_EVIDENCE_REQUEST_ONLY = "request_only"
WEB_SEARCH_EVIDENCE_TOKEN_INFERENCE = "token_inference"
WEB_SEARCH_EVIDENCE_NOT_AVAILABLE = "not_available"

WEB_SEARCH_STATUS_SATISFIED = "satisfied"
WEB_SEARCH_STATUS_NOT_SATISFIED = "not_satisfied"
WEB_SEARCH_STATUS_NOT_VERIFIABLE = "not_verifiable"
WEB_SEARCH_STATUS_NOT_SUPPORTED = "not_supported"
WEB_SEARCH_STATUS_NOT_APPLICABLE = "not_applicable"

SOURCE_STATUS_PARSED = "parsed"
SOURCE_STATUS_PROVIDER_RETURNED_EMPTY = "provider_returned_empty"
SOURCE_STATUS_UNSUPPORTED_BY_PROTOCOL = "unsupported_by_protocol"
SOURCE_STATUS_PARSE_ERROR = "parse_error"
SOURCE_STATUS_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class AdapterCapabilities:
    api_family: str
    supported_model_patterns: tuple[str, ...] = ("*",)
    supports_forced_search: bool = False
    supports_sources: bool | str = "partial"
    supports_search_trace: bool | str = "partial"
    source_grain: str = "url"
    supports_text_analysis: bool = False
    supports_search_limit: bool = False

    def supports_model(self, model: str) -> bool:
        text = str(model or "").strip()
        return any(fnmatchcase(text, pattern) for pattern in self.supported_model_patterns)


@dataclass(frozen=True)
class ProviderRequest:
    sampling_profile: dict[str, Any]
    payload: dict[str, Any]
    request_fingerprint_basis: dict[str, Any]
    legacy_request_hashes: tuple[str, ...] = ()

    @property
    def request_hash(self) -> str:
        return request_fingerprint(self.request_fingerprint_basis)

    @property
    def request_fingerprint_version(self) -> str:
        return REQUEST_FINGERPRINT_VERSION

    @property
    def model(self) -> str:
        return str(self.sampling_profile.get("model") or self.payload.get("model") or "")


@dataclass(frozen=True)
class NormalizedProviderResponse:
    text: str | None
    sources: list[SourceRecord]
    usage: dict[str, Any] | None
    raw: dict[str, Any]
    provider_meta: dict[str, Any] = field(default_factory=dict)
    web_search_performed: bool | None = None
    web_search_evidence: str = WEB_SEARCH_EVIDENCE_NOT_AVAILABLE
    web_search_requirement_status: str = WEB_SEARCH_STATUS_NOT_APPLICABLE
    source_parse_status: str = SOURCE_STATUS_NOT_APPLICABLE


class ProviderAdapter(Protocol):
    name: str
    provider: str
    adapter_version: str
    capabilities: AdapterCapabilities
    allowed_options: set[str]

    def validate_options(self, options: dict[str, Any]) -> None: ...

    def build_request(
        self,
        query_record: QueryRecord,
        sampling_profile: dict[str, Any],
        settings: Settings,
        adapter_options: dict[str, Any],
    ) -> ProviderRequest: ...

    def send(self, client: Any, request: ProviderRequest) -> Any: ...

    def normalize_response(self, response: Any, request: ProviderRequest) -> NormalizedProviderResponse: ...


class BaseAdapter:
    name = ""
    provider = ""
    adapter_version = "1"
    capabilities = AdapterCapabilities(api_family="responses")
    allowed_options: set[str] = set()

    def validate_options(self, options: dict[str, Any]) -> None:
        unknown = sorted(set(options) - self.allowed_options)
        if unknown:
            raise ValueError(f"{self.name} adapter_options 包含未知字段：{', '.join(unknown)}")

    def _require_object_option(self, options: dict[str, Any], key: str) -> dict[str, Any]:
        value = options.get(key)
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"{self.name} adapter_options.{key} 必须是对象")
        return dict(value)

    def _require_bool_option(self, options: dict[str, Any], key: str) -> bool | None:
        value = options.get(key)
        if value in (None, ""):
            return None
        if not isinstance(value, bool):
            raise ValueError(f"{self.name} adapter_options.{key} 必须是布尔值")
        return value

    def _string_list_option(
        self,
        options: dict[str, Any],
        key: str,
        *,
        default: list[str] | None = None,
    ) -> list[str] | None:
        if key not in options:
            return list(default) if default is not None else None
        value = options[key]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{self.name} adapter_options.{key} 必须是字符串数组")
        return list(value)

    def _required_web_search_tool_choice(self, options: dict[str, Any], key: str = "tool_choice") -> str | dict[str, str] | None:
        if key not in options:
            return None
        value = options[key]
        if value == "required":
            return "required"
        if isinstance(value, dict) and set(value) == {"type"} and value.get("type") == "web_search":
            return {"type": "web_search"}
        raise ValueError(
            f"{self.name} adapter_options.{key} 必须是 'required' 或严格对象 {{'type': 'web_search'}}；"
            "联网搜索为必需语义，不接受 auto、none、拼写变体或额外字段"
        )

    def _positive_int_option(
        self,
        options: dict[str, Any],
        key: str,
        default: int,
        *,
        maximum: int | None = None,
    ) -> int:
        value = options.get(key, default)
        if isinstance(value, bool):
            raise ValueError(f"{self.name} adapter_options.{key} 必须是正整数")
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float) and value.is_integer():
            parsed = int(value)
        elif isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
        else:
            raise ValueError(f"{self.name} adapter_options.{key} 必须是正整数")
        if parsed < 1:
            raise ValueError(f"{self.name} adapter_options.{key} 必须是正整数")
        if maximum is not None and parsed > maximum:
            raise ValueError(f"{self.name} adapter_options.{key} 不能大于 {maximum}")
        return parsed

    def _fingerprint_basis(self, query_record: QueryRecord, sampling_profile: dict[str, Any], payload_basis: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": sampling_profile.get("provider"),
            "adapter": sampling_profile.get("adapter"),
            "adapter_version": sampling_profile.get("adapter_version"),
            "api_family": sampling_profile.get("api_family"),
            "provider_sdk": sampling_profile.get("provider_sdk"),
            "base_url_fingerprint": sampling_profile.get("base_url_fingerprint"),
            "model": sampling_profile.get("model"),
            "query_id": query_record.query_id,
            "input": query_record.query,
            "web_search_required": sampling_profile.get("web_search_required"),
            "source_grain": sampling_profile.get("source_grain"),
            "payload": payload_basis,
        }

    def normalize_response(self, response: Any, request: ProviderRequest) -> NormalizedProviderResponse:
        raw = response_to_dict(response)
        try:
            text, sources, usage, parsed_raw = parse_response(raw)
            source_status = _source_parse_status(sources, self.capabilities)
        except Exception:
            text, sources, usage, parsed_raw = None, [], None, raw
            source_status = SOURCE_STATUS_PARSE_ERROR
        performed, evidence, requirement_status = infer_web_search_status(parsed_raw, request.payload, request.sampling_profile)
        return NormalizedProviderResponse(
            text=text,
            sources=sources,
            usage=usage,
            raw=parsed_raw,
            provider_meta={},
            web_search_performed=performed,
            web_search_evidence=evidence,
            web_search_requirement_status=requirement_status,
            source_parse_status=source_status,
        )


def infer_web_search_status(raw: dict[str, Any], payload: dict[str, Any], sampling_profile: dict[str, Any]) -> tuple[bool | None, str, str]:
    required = bool(sampling_profile.get("web_search_required", True))
    if not required:
        return None, WEB_SEARCH_EVIDENCE_NOT_AVAILABLE, WEB_SEARCH_STATUS_NOT_APPLICABLE
    if _has_web_search_call(raw):
        return True, WEB_SEARCH_EVIDENCE_PROVIDER_TRACE, WEB_SEARCH_STATUS_SATISFIED
    if _web_search_tool_count(raw) > 0:
        return True, WEB_SEARCH_EVIDENCE_TOOL_USAGE_COUNT, WEB_SEARCH_STATUS_SATISFIED
    if request_has_web_search(payload):
        return None, WEB_SEARCH_EVIDENCE_REQUEST_ONLY, WEB_SEARCH_STATUS_NOT_VERIFIABLE
    return False, WEB_SEARCH_EVIDENCE_NOT_AVAILABLE, WEB_SEARCH_STATUS_NOT_SATISFIED


def request_has_web_search(payload: dict[str, Any]) -> bool:
    if payload.get("enable_search") is True:
        return True
    for tool in payload.get("tools", []) or []:
        if isinstance(tool, dict) and str(tool.get("type") or "") == "web_search":
            return True
    extra_body = payload.get("extra_body") if isinstance(payload.get("extra_body"), dict) else {}
    return bool(extra_body.get("enable_search"))


def _source_parse_status(sources: list[SourceRecord], capabilities: AdapterCapabilities) -> str:
    if capabilities.source_grain == "none":
        return SOURCE_STATUS_NOT_APPLICABLE
    if sources:
        return SOURCE_STATUS_PARSED
    if capabilities.supports_sources is False:
        return SOURCE_STATUS_UNSUPPORTED_BY_PROTOCOL
    return SOURCE_STATUS_PROVIDER_RETURNED_EMPTY


def _has_web_search_call(raw: Any) -> bool:
    if isinstance(raw, dict):
        if raw.get("type") == "web_search_call":
            return True
        search_info = raw.get("search_info")
        if isinstance(search_info, dict) and isinstance(search_info.get("search_results"), list):
            return True
        return any(_has_web_search_call(value) for value in raw.values())
    if isinstance(raw, list):
        return any(_has_web_search_call(item) for item in raw)
    return False


def _web_search_tool_count(raw: dict[str, Any]) -> int:
    total = 0
    for path in [
        ("x_tools", "web_search", "count"),
        ("usage", "tool_usage", "web_search"),
        ("usage", "plugins", "web_search", "count"),
        ("plugins", "web_search", "count"),
    ]:
        value: Any = raw
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        try:
            total += int(value or 0)
        except Exception:
            continue
    return total
