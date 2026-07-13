"""Job configuration loading and scalar validation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..adapters.registry import build_sampling_profile, get_adapter
from ..config import Settings
from ..schemas import MAX_QUERY_CHARS, utc_now_iso
from .contracts import JOB_CONFIG_KEYS, JobError
from .profiles import build_analysis_profile, build_comparability_profile, freeze_sampling_profile


@dataclass(frozen=True, slots=True)
class ValidatedJobSpec:
    """Canonical, fully validated configuration shared by build and inspect flows."""

    queries: list[dict[str, Any]]
    query_manifest_info: dict[str, Any] | None
    target_brand: str
    target_aliases: list[str]
    owned_domains: list[str]
    industry: str
    market: str
    repeats: int
    model: str
    web_search_limit: int
    adapter: str
    adapter_options: dict[str, Any]
    sampling_profile: dict[str, Any]
    analysis_profile: dict[str, Any]
    comparability_profile: dict[str, Any]
    concurrency: int
    start_interval_seconds: float

    @property
    def planned_units(self) -> int:
        return len(self.queries) * self.repeats

    def validation_result(self) -> dict[str, Any]:
        result = {
            "target_brand": self.target_brand,
            "target_aliases": self.target_aliases,
            "owned_domains": self.owned_domains,
            "industry": self.industry,
            "market": self.market,
            "query_count": len(self.queries),
            "repeats": self.repeats,
            "planned_units": self.planned_units,
            "model": self.model,
            "web_search_limit": self.web_search_limit,
            "adapter": self.adapter,
            "sampling_profile": self.sampling_profile,
            "analysis_profile": self.analysis_profile,
            "comparability_profile": self.comparability_profile,
            "concurrency": self.concurrency,
            "start_interval_seconds": self.start_interval_seconds,
        }
        if self.query_manifest_info is not None:
            result["query_manifest"] = self.query_manifest_info
        return result


def load_job_config(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise JobError(f"任务配置不存在：{path}")
    if path.is_symlink() or not path.is_file():
        raise JobError(f"任务配置必须是普通非 symlink 文件：{path}")
    if path.stat().st_size > 5 * 1024 * 1024:
        raise JobError(f"任务配置超过 5 MiB 上限：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise JobError(f"job_config JSON 格式错误：{path}:{exc.lineno}:{exc.colno}，请检查 JSON 格式") from exc
    if not isinstance(data, dict):
        raise JobError("job_config.json 必须是 JSON 对象")
    return data


def load_validated_job_config(config_path: str | Path) -> dict[str, Any]:
    """Load a job config and reject unsupported root fields immediately."""

    config = load_job_config(config_path)
    validate_job_config_keys(config)
    return config


def validate_job_config_keys(config: dict[str, Any]) -> None:
    unknown = sorted(set(config) - JOB_CONFIG_KEYS)
    if unknown:
        raise JobError(f"job_config 包含未知字段：{', '.join(unknown)}")


def validate_job_spec(
    config: dict[str, Any],
    queries: list[dict[str, Any]],
    settings: Settings,
    *,
    context: str,
    query_manifest_info: dict[str, Any] | None = None,
) -> ValidatedJobSpec:
    """Parse and validate every setting shared by job build and validation."""

    repeats = positive_int(config.get("repeats", 20), "repeats")
    ensure_unit_limit(len(queries) * repeats, settings, context=context)
    web_search_limit = bounded_int(
        config.get("web_search_limit", settings.web_search_limit),
        "web_search_limit",
        minimum=1,
        maximum=20,
    )
    concurrency = positive_int(config.get("concurrency", settings.concurrency), "concurrency")
    if concurrency > 8:
        raise JobError("concurrency 必须在 1 到 8 之间")
    start_interval_seconds = non_negative_float(config.get("start_interval_seconds", 0.0), "start_interval_seconds")
    model = str(config.get("model") or settings.llm_model).strip()
    if not model:
        raise JobError("model 不能为空")
    adapter_name = str(config.get("adapter") or "openai_compatible_responses_web_search").strip()
    adapter_options = object_dict(config.get("adapter_options"), "adapter_options")
    try:
        adapter = get_adapter(adapter_name)
        adapter.validate_options(adapter_options)
        sampling_profile = build_sampling_profile(
            adapter_name=adapter_name,
            model=model,
            settings=settings,
            web_search_limit=web_search_limit,
            web_search_required=True,
        )
        sampling_profile = freeze_sampling_profile(sampling_profile, settings, adapter_options)
        analysis_model = str(config.get("analysis_model") or model).strip()
        if not analysis_model:
            raise JobError("analysis_model 不能为空")
        analysis_adapter = str(config.get("analysis_adapter") or "openai_compatible_responses_text").strip()
        analysis_profile = build_analysis_profile(analysis_adapter, analysis_model, settings)
    except ValueError as exc:
        raise JobError(str(exc)) from exc

    target_brand = required_str(config, "target_brand")
    target_aliases = string_list(config.get("target_aliases"), "target_aliases")
    owned_domains = domain_list(config.get("owned_domains"), "owned_domains")
    industry = required_str(config, "industry")
    market = optional_str(config, "market", default="未指定市场")
    comparability_profile = build_comparability_profile(
        query_manifest_info or {"sha256": "", "row_count": len(queries)},
        queries,
        repeats,
        sampling_profile,
        analysis_profile,
        target_brand=target_brand,
        target_aliases=target_aliases,
        owned_domains=owned_domains,
        industry=industry,
        market=market,
    )
    return ValidatedJobSpec(
        queries=queries,
        query_manifest_info=query_manifest_info,
        target_brand=target_brand,
        target_aliases=target_aliases,
        owned_domains=owned_domains,
        industry=industry,
        market=market,
        repeats=repeats,
        model=model,
        web_search_limit=web_search_limit,
        adapter=adapter.name,
        adapter_options=adapter_options,
        sampling_profile=sampling_profile,
        analysis_profile=analysis_profile,
        comparability_profile=comparability_profile,
        concurrency=concurrency,
        start_interval_seconds=start_interval_seconds,
    )


def normalize_queries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise JobError("queries 必须是非空数组")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if isinstance(item, str):
            query = item.strip()
            query_id = f"q{index:03d}"
            row: dict[str, Any] = {"query_id": query_id, "query": query}
        elif isinstance(item, dict):
            query = str(item.get("query") or item.get("text") or "").strip()
            query_id = str(item.get("query_id") or f"q{index:03d}").strip()
            row = {key: value for key, value in item.items() if value not in (None, "")}
            row["query_id"] = query_id
            row["query"] = query
        else:
            raise JobError("queries 只能包含字符串或对象")
        if not query:
            raise JobError(f"queries 第 {index} 项为空")
        if len(query) > MAX_QUERY_CHARS:
            raise JobError(f"queries 第 {index} 项超过 {MAX_QUERY_CHARS} 字符上限")
        if not query_id:
            raise JobError(f"queries 第 {index} 项 query_id 不能为空")
        if query_id in seen:
            raise JobError(f"query_id 重复：{query_id}")
        seen.add(query_id)
        if isinstance(row.get("tags"), list):
            row["tags"] = ",".join(str(tag).strip() for tag in row["tags"] if str(tag).strip())
        rows.append(row)
    return rows


def make_job_id() -> str:
    stamp = utc_now_iso().replace("+00:00", "Z").replace("-", "").replace(":", "")
    return f"job_{stamp}_{uuid4().hex[:6]}"


def required_str(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise JobError(f"{key} 不能为空")
    return value


def optional_str(data: dict[str, Any], key: str, *, default: str) -> str:
    value = str(data.get(key) or "").strip()
    return value or default


def string_list(value: Any, key: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise JobError(f"{key} 必须是字符串数组")
    return [str(item).strip() for item in value if str(item).strip()]


def domain_list(value: Any, key: str) -> list[str]:
    domains = string_list(value, key)
    output: list[str] = []
    for domain in domains:
        candidate = domain.lower().rstrip(".")
        if candidate.startswith("www."):
            candidate = candidate[4:]
        if not candidate or any(token in candidate for token in ("://", "/", "@", "?", "#", ":")):
            raise JobError(f"{key} 只能包含域名，不包含 scheme、端口、路径或凭据：{domain}")
        try:
            ascii_domain = candidate.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise JobError(f"{key} 包含无效域名：{domain}") from exc
        labels = ascii_domain.split(".")
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
            raise JobError(f"{key} 包含无效域名：{domain}")
        if ascii_domain not in output:
            output.append(ascii_domain)
    return output


def object_dict(value: Any, key: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise JobError(f"{key} 必须是对象")
    return dict(value)


def bounded_int(value: Any, key: str, *, minimum: int, maximum: int) -> int:
    parsed = positive_int(value, key)
    if parsed < minimum or parsed > maximum:
        raise JobError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def non_negative_float(value: Any, key: str) -> float:
    try:
        parsed = float(value)
    except Exception as exc:
        raise JobError(f"{key} 必须是非负数") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise JobError(f"{key} 必须是非负数")
    return parsed


def positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise JobError(f"{key} 必须是正整数")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise JobError(f"{key} 必须是正整数")
        parsed = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise JobError(f"{key} 必须是正整数")
        parsed = int(text)
    else:
        raise JobError(f"{key} 必须是正整数")
    if parsed < 1:
        raise JobError(f"{key} 必须是正整数")
    return parsed


def ensure_unit_limit(units: int, settings: Settings, *, context: str) -> None:
    if units > settings.max_job_units:
        raise JobError(f"{context}计划 {units} 个单元，超过 MAX_JOB_UNITS={settings.max_job_units}；请缩小 query/repeats 范围")


def validate_persisted_queries(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise JobError("job_manifest queries 必须是非空数组")
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise JobError("job_manifest queries 只能包含对象")
        if "query_id" not in item or "query" not in item:
            raise JobError(f"job_manifest queries 第 {index} 项必须包含 query_id 和 query")
        query_id = str(item.get("query_id") or "").strip()
        query = str(item.get("query") or "").strip()
        if not query_id or not query:
            raise JobError(f"job_manifest queries 第 {index} 项 query_id/query 不能为空")
        if query_id in seen:
            raise JobError(f"query_id 重复：{query_id}")
        seen.add(query_id)
