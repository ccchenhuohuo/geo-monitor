"""Frozen provider, analysis, and comparability profiles."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..adapters.registry import get_adapter
from ..config import Settings
from ..request_fingerprint import analysis_fingerprint, base_url_fingerprint
from .contracts import JobError


def build_analysis_profile(adapter_name: str, model: str, settings: Settings) -> dict[str, Any]:
    adapter = get_adapter(adapter_name)
    if adapter.name != "openai_responses_text":
        raise ValueError("analysis_adapter 目前只支持 openai_responses_text")
    model_text = str(model or "").strip()
    if not model_text:
        raise ValueError("analysis_model 不能为空")
    if not adapter.capabilities.supports_model(model_text):
        patterns = ", ".join(adapter.capabilities.supported_model_patterns)
        raise ValueError(f"{adapter.name} 不支持 analysis_model {model_text!r}；支持模式：{patterns}")
    profile = {
        "provider": adapter.provider,
        "adapter": adapter.name,
        "adapter_version": adapter.adapter_version,
        "api_family": adapter.capabilities.api_family,
        "model": model_text,
        "base_url_fingerprint": base_url_fingerprint(settings.llm_base_url),
        "max_output_tokens": settings.analysis_max_output_tokens,
    }
    profile["analysis_fingerprint"] = analysis_fingerprint(profile)
    return profile


def freeze_sampling_profile(profile: dict[str, Any], settings: Settings, adapter_options: dict[str, Any]) -> dict[str, Any]:
    frozen = dict(profile)
    effective_runtime: dict[str, Any] = {"adapter_options": dict(adapter_options)}
    max_tool_calls = effective_max_tool_calls(profile, settings, adapter_options)
    if max_tool_calls is not None:
        effective_runtime["max_tool_calls"] = max_tool_calls
        frozen["max_tool_calls"] = max_tool_calls
    effective_runtime["max_output_tokens"] = settings.max_output_tokens
    frozen["max_output_tokens"] = settings.max_output_tokens
    frozen["effective_runtime"] = effective_runtime
    return frozen


def validate_runtime_profile(
    manifest: dict[str, Any],
    settings: Settings,
    *,
    require_request_match: bool,
    require_endpoint_match: bool,
) -> None:
    profile = dict(manifest.get("sampling_profile") or {})
    effective = profile.get("effective_runtime")
    if require_request_match and isinstance(effective, dict):
        expected_max_tool_calls = effective.get("max_tool_calls")
        actual_max_tool_calls = effective_max_tool_calls(profile, settings, dict(manifest.get("adapter_options") or {}))
        if expected_max_tool_calls is not None and int(expected_max_tool_calls) != actual_max_tool_calls:
            raise JobError("运行时 MAX_TOOL_CALLS 与 sampling_profile.effective_runtime.max_tool_calls 不一致；请使用构建 job 时的配置")
        expected_max_output_tokens = effective.get("max_output_tokens")
        if expected_max_output_tokens is not None and int(expected_max_output_tokens) != settings.max_output_tokens:
            raise JobError("运行时 MAX_OUTPUT_TOKENS 与 sampling_profile.effective_runtime.max_output_tokens 不一致；请使用构建 job 时的配置")
        if dict(effective.get("adapter_options") or {}) != dict(manifest.get("adapter_options") or {}):
            raise JobError("运行时 adapter_options 与 sampling_profile.effective_runtime.adapter_options 不一致")
    if require_endpoint_match:
        expected_endpoint = str(profile.get("base_url_fingerprint") or "")
        actual_endpoint = base_url_fingerprint(settings.llm_base_url)
        if expected_endpoint and expected_endpoint != actual_endpoint:
            raise JobError("运行时 LLM_BASE_URL 与 sampling_profile.base_url_fingerprint 不一致；请使用构建 job 时的 endpoint 或重新构建 job")


def effective_max_tool_calls(profile: dict[str, Any], settings: Settings, adapter_options: dict[str, Any]) -> int | None:
    if str(profile.get("api_family") or "") != "responses":
        return None
    return int(adapter_options.get("max_tool_calls", settings.max_tool_calls))


def build_comparability_profile(
    query_manifest_info: dict[str, Any],
    queries: list[dict[str, Any]],
    repeats: int,
    sampling_profile: dict[str, Any],
    analysis_profile: dict[str, Any],
    *,
    target_brand: str = "",
    target_aliases: list[str] | None = None,
    owned_domains: list[str] | None = None,
    industry: str = "",
    market: str = "",
) -> dict[str, Any]:
    query_digest = str(query_manifest_info.get("sha256") or "") or query_rows_digest(queries)
    profile = {
        "query_manifest_sha256": query_digest,
        "repeats": repeats,
        "analysis_fingerprint": analysis_profile.get("analysis_fingerprint", ""),
        "source_grain": sampling_profile.get("source_grain", "unknown"),
    }
    study_basis = {
        "target_brand": target_brand,
        "target_aliases": sorted(str(alias) for alias in (target_aliases or [])),
        "owned_domains": sorted(str(domain) for domain in (owned_domains or [])),
        "industry": industry,
        "market": market,
        "query_manifest_sha256": query_digest,
        "query_count": len(queries) or int(query_manifest_info.get("row_count") or 0),
        "repeats": repeats,
    }
    sampling_basis = {key: value for key, value in sampling_profile.items() if key not in {"inferred_from_legacy", "web_search_limit"}}
    if bool(sampling_profile.get("web_search_limit_effective")):
        sampling_basis["web_search_limit"] = sampling_profile.get("web_search_limit")
    profile["study_fingerprint"] = stable_digest(study_basis)
    profile["sampling_fingerprint"] = stable_digest(sampling_basis)
    return profile


def query_rows_digest(queries: list[dict[str, Any]]) -> str:
    rows = [{key: row.get(key, "") for key in sorted(row)} for row in queries]
    stable = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def stable_digest(value: Any) -> str:
    stable = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()
