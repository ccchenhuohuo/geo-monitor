"""Job manifest persistence, schema migration, and contract validation."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from ..adapters.registry import build_sampling_profile
from ..config import get_settings
from ..filesystem import ensure_private_directory, open_private_text, secure_private_file
from ..request_fingerprint import REQUEST_FINGERPRINT_VERSION, analysis_fingerprint, base_url_fingerprint
from ..schemas import utc_now_iso
from .bundle_files import ensure_bundle_regular_file
from .config import bounded_int, domain_list, non_negative_float, optional_str, positive_int, required_str, string_list, validate_persisted_queries
from .contracts import (
    ALLOWED_STATUSES,
    CLEANUP_SUMMARY,
    DIAGNOSTIC_RUN_SUMMARY,
    GEO_JOB_V1,
    GEO_JOB_V2,
    GEO_JOB_V3,
    JOB_MANIFEST,
    LOGS_DIR,
    QUERY_MANIFEST,
    RAW_ATTEMPTS,
    RESULT_DIR,
    RUN_SUMMARY,
    WORK_DIR,
    JobError,
)
from .profiles import build_analysis_profile, build_comparability_profile, query_rows_digest


def load_job_manifest(bundle_dir: str | Path) -> dict[str, Any]:
    root = Path(bundle_dir)
    path = root / JOB_MANIFEST
    if not path.exists():
        raise JobError(f"缺少 job_manifest.json：{path}")
    ensure_bundle_regular_file(root, path, "job_manifest.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") not in {GEO_JOB_V1, GEO_JOB_V2, GEO_JOB_V3}:
        raise JobError("job_manifest schema_version 必须是 geo-job-v1、geo-job-v2 或 geo-job-v3")
    data = normalize_job_manifest_profiles(data)
    validate_job_manifest(data)
    return data


def update_job_manifest(bundle_dir: str | Path, *, status: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(bundle_dir)
    manifest = load_job_manifest(root)
    if status:
        manifest["status"] = status
    manifest["updated_at"] = utc_now_iso()
    manifest["paths"] = manifest_paths()
    if extra:
        manifest.update(extra)
    validate_job_manifest(manifest)
    write_json(root / JOB_MANIFEST, manifest)
    return manifest


def query_set_hash(manifest: dict[str, Any]) -> str:
    comparability = manifest.get("comparability_profile")
    if isinstance(comparability, dict) and comparability.get("query_manifest_sha256"):
        return str(comparability["query_manifest_sha256"])[:16]
    info = manifest.get("query_manifest")
    if isinstance(info, dict) and info.get("sha256"):
        return str(info["sha256"])[:16]
    queries = manifest.get("queries") if isinstance(manifest.get("queries"), list) else []
    return query_rows_digest(queries)[:16]


def write_json(path: Path, data: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open_private_text(tmp_path) as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp_path, path)
        secure_private_file(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def manifest_paths() -> dict[str, str]:
    return {
        "query_manifest": QUERY_MANIFEST,
        "raw_attempts": RAW_ATTEMPTS,
        "work_dir": WORK_DIR,
        "result_dir": RESULT_DIR,
        "logs_dir": LOGS_DIR,
        "run_summary": RUN_SUMMARY,
        "diagnostic_run_summary": DIAGNOSTIC_RUN_SUMMARY,
        "cleanup_summary": CLEANUP_SUMMARY,
    }


def normalize_job_manifest_profiles(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    settings = get_settings()
    legacy_schema = str(normalized.get("schema_version") or "") in {GEO_JOB_V1, GEO_JOB_V2}
    model = str(normalized.get("model") or settings.llm_model)
    web_search_limit = int(normalized.get("web_search_limit") or settings.web_search_limit)
    adapter_name = str(normalized.get("adapter") or "openai_responses_web_search")
    if not isinstance(normalized.get("adapter_options"), dict):
        normalized["adapter_options"] = {}
    if not isinstance(normalized.get("sampling_profile"), dict):
        try:
            normalized["sampling_profile"] = build_sampling_profile(
                adapter_name=adapter_name,
                model=model,
                settings=settings,
                web_search_limit=web_search_limit,
                web_search_required=True,
            )
            if legacy_schema:
                normalized["sampling_profile"]["inferred_from_legacy"] = True
        except ValueError:
            normalized["sampling_profile"] = {
                "provider": "openai_compatible",
                "adapter": "openai_responses_web_search",
                "adapter_version": "1",
                "api_family": "responses",
                "model": model,
                "base_url_fingerprint": base_url_fingerprint(settings.llm_base_url),
                "request_fingerprint_version": REQUEST_FINGERPRINT_VERSION,
                "web_search_required": True,
                "source_grain": "url",
                "web_search_limit": web_search_limit,
                "inferred_from_legacy": True,
            }
    if not normalized.get("adapter"):
        normalized["adapter"] = str(normalized["sampling_profile"].get("adapter") or adapter_name)
    if not isinstance(normalized.get("analysis_profile"), dict):
        analysis_model = str(normalized.get("analysis_model") or model)
        try:
            normalized["analysis_profile"] = build_analysis_profile("openai_responses_text", analysis_model, settings)
            if legacy_schema:
                normalized["analysis_profile"]["inferred_from_legacy"] = True
        except ValueError:
            normalized["analysis_profile"] = {
                "provider": "openai_compatible",
                "adapter": "openai_responses_text",
                "adapter_version": "1",
                "api_family": "responses",
                "model": analysis_model,
                "base_url_fingerprint": base_url_fingerprint(settings.llm_base_url),
                "analysis_fingerprint": "",
                "inferred_from_legacy": True,
            }
            normalized["analysis_profile"]["analysis_fingerprint"] = analysis_fingerprint(normalized["analysis_profile"])
    if not isinstance(normalized.get("comparability_profile"), dict):
        info = normalized.get("query_manifest") if isinstance(normalized.get("query_manifest"), dict) else {}
        queries = normalized.get("queries") if isinstance(normalized.get("queries"), list) else []
        normalized["comparability_profile"] = build_comparability_profile(
            info,
            queries,
            int(normalized.get("repeats") or 1),
            normalized["sampling_profile"],
            normalized["analysis_profile"],
            target_brand=str(normalized.get("target_brand") or ""),
            target_aliases=string_list(normalized.get("target_aliases"), "target_aliases") if isinstance(normalized.get("target_aliases"), list) else [],
            owned_domains=domain_list(normalized.get("owned_domains"), "owned_domains") if isinstance(normalized.get("owned_domains"), list) else [],
            industry=str(normalized.get("industry") or ""),
            market=str(normalized.get("market") or ""),
        )
        if legacy_schema:
            normalized["comparability_profile"]["inferred_from_legacy"] = True
    if legacy_schema:
        normalized["sampling_profile"].setdefault("inferred_from_legacy", True)
        normalized["analysis_profile"].setdefault("inferred_from_legacy", True)
        normalized["comparability_profile"].setdefault("inferred_from_legacy", True)
    return normalized


def validate_job_manifest(data: dict[str, Any]) -> None:
    required = [
        "schema_version",
        "job_id",
        "status",
        "target_brand",
        "industry",
        "market",
        "repeats",
        "model",
        "web_search_limit",
        "adapter",
        "concurrency",
        "query_count",
    ]
    missing = [key for key in required if key not in data]
    if missing:
        raise JobError(f"job_manifest 缺少字段：{', '.join(missing)}")
    schema_version = str(data.get("schema_version") or "")
    if str(data.get("status") or "") not in ALLOWED_STATUSES:
        raise JobError(f"job_manifest status 无效：{data.get('status')}")
    if schema_version == GEO_JOB_V1:
        if not isinstance(data.get("queries"), list) or not data["queries"]:
            raise JobError("job_manifest queries 必须是非空数组")
        if positive_int(data.get("query_count"), "query_count") != len(data["queries"]):
            raise JobError("job_manifest query_count 与 queries 数量不一致")
    else:
        if not isinstance(data.get("query_manifest"), dict):
            raise JobError(f"{schema_version} 必须包含 query_manifest")
        info = data["query_manifest"]
        for key in ["source_type", "schema_version", "sha256", "row_count"]:
            if key not in info:
                raise JobError(f"query_manifest 缺少字段：{key}")
        if schema_version == GEO_JOB_V3 and info.get("source_type") == "external_file":
            sha = str(info.get("sha256") or "")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
                raise JobError("geo-job-v3 external query_manifest.sha256 必须是 64 位 hex")
            if not str(info.get("source_uri") or "").strip():
                raise JobError("geo-job-v3 external query_manifest 必须包含 source_uri")
        if positive_int(data.get("query_count"), "query_count") != positive_int(info.get("row_count"), "query_manifest.row_count"):
            raise JobError("job_manifest query_count 与 query_manifest.row_count 不一致")
    positive_int(data.get("repeats"), "repeats")
    bounded_int(data.get("web_search_limit"), "web_search_limit", minimum=1, maximum=20)
    concurrency = positive_int(data.get("concurrency"), "concurrency")
    if concurrency > 8:
        raise JobError("concurrency 必须在 1 到 8 之间")
    non_negative_float(data.get("start_interval_seconds", 0.0), "start_interval_seconds")
    if not str(data.get("model") or "").strip():
        raise JobError("model 不能为空")
    validate_profile_object(
        data.get("sampling_profile"),
        "sampling_profile",
        [
            "provider",
            "adapter",
            "adapter_version",
            "api_family",
            "model",
            "base_url_fingerprint",
            "request_fingerprint_version",
            "web_search_required",
            "source_grain",
        ],
    )
    validate_profile_object(
        data.get("analysis_profile"),
        "analysis_profile",
        ["provider", "adapter", "adapter_version", "api_family", "model", "base_url_fingerprint", "analysis_fingerprint"],
    )
    validate_profile_object(
        data.get("comparability_profile"),
        "comparability_profile",
        ["query_manifest_sha256", "repeats", "analysis_fingerprint", "source_grain"],
    )
    validate_manifest_profile_consistency(data)
    required_str(data, "target_brand")
    string_list(data.get("target_aliases"), "target_aliases")
    domain_list(data.get("owned_domains"), "owned_domains")
    required_str(data, "industry")
    optional_str(data, "market", default="未指定市场")
    if schema_version == GEO_JOB_V1:
        validate_persisted_queries(data.get("queries"))


def validate_profile_object(value: Any, name: str, required: list[str]) -> None:
    if not isinstance(value, dict):
        raise JobError(f"{name} 必须是对象")
    missing = [key for key in required if key not in value]
    if missing:
        raise JobError(f"{name} 缺少字段：{', '.join(missing)}")


def validate_manifest_profile_consistency(data: dict[str, Any]) -> None:
    profile = data["sampling_profile"]
    checks = {
        "adapter": str(data.get("adapter") or ""),
        "model": str(data.get("model") or ""),
        "web_search_limit": int(data.get("web_search_limit") or 0),
    }
    for key, expected in checks.items():
        if key in profile and profile.get(key) != expected:
            raise JobError(f"sampling_profile.{key} 与 job_manifest.{key} 不一致")
    effective = profile.get("effective_runtime")
    if effective is None:
        return
    if not isinstance(effective, dict):
        raise JobError("sampling_profile.effective_runtime 必须是对象")
    if "max_tool_calls" in effective:
        effective_max_tool_calls = positive_int(effective.get("max_tool_calls"), "sampling_profile.effective_runtime.max_tool_calls")
        if "max_tool_calls" in profile and profile.get("max_tool_calls") != effective_max_tool_calls:
            raise JobError("sampling_profile.max_tool_calls 与 effective_runtime.max_tool_calls 不一致")
    if "max_output_tokens" in effective:
        effective_max_output_tokens = positive_int(effective.get("max_output_tokens"), "sampling_profile.effective_runtime.max_output_tokens")
        if "max_output_tokens" in profile and profile.get("max_output_tokens") != effective_max_output_tokens:
            raise JobError("sampling_profile.max_output_tokens 与 effective_runtime.max_output_tokens 不一致")
    frozen_options = effective.get("adapter_options")
    if not isinstance(frozen_options, dict):
        raise JobError("sampling_profile.effective_runtime.adapter_options 必须是对象")
    if frozen_options != dict(data.get("adapter_options") or {}):
        raise JobError("sampling_profile.effective_runtime.adapter_options 与 job_manifest.adapter_options 不一致")
