from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .llm_client import build_responses_payload
from .config import Settings, get_settings, workspace_root
from .dataset import load_queries, select_queries
from .exporters import latest_records, read_jsonl, successful_result_hashes
from .runner import MonitorRunner, compute_request_hash
from .schemas import QueryRecord, utc_now_iso


JOB_MANIFEST = "job_manifest.json"
RUNS_DIR = ".runs"
WORK_DIR = "work"
RAW_DIR = "raw"
RESULT_DIR = "result"
LOGS_DIR = "logs"
QUERY_MANIFEST = f"{WORK_DIR}/query_manifest.csv"
RAW_ATTEMPTS = "raw/attempts.jsonl"
GEO_JOB_V1 = "geo-job-v1"
GEO_JOB_V2 = "geo-job-v2"
QUERY_MANIFEST_V1 = "query-manifest-v1"
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
RUN_SUMMARY = "logs/run_summary.json"
CLEANUP_SUMMARY = "logs/cleanup_summary.json"
BUNDLE_LOCK = "logs/bundle.lock"
JOB_CONFIG_KEYS = {
    "target_brand",
    "target_aliases",
    "industry",
    "market",
    "queries",
    "repeats",
    "model",
    "web_search_limit",
    "concurrency",
    "start_interval_seconds",
}
ALLOWED_STATUSES = {
    "built",
    "running",
    "ran",
    "ran_partial",
    "run_failed",
    "analyzing",
    "analyzed",
    "analyzed_partial",
    "analysis_failed",
    "analyzed_cleaned",
    "analyzed_partial_cleaned",
    "cleaned",
}


class JobError(ValueError):
    pass


def build_job_bundle(
    config_path: str | Path,
    out_dir: str | Path | None = None,
    settings: Settings | None = None,
    *,
    force: bool = False,
    query_manifest_path: str | Path | None = None,
    runs_dir: str | Path | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    config = _load_job_config(config_path)
    _validate_job_config_keys(config)
    job_id = _make_job_id()
    if out_dir is not None and runs_dir is not None:
        raise JobError("--out-dir 和 --runs-dir 不能同时使用")
    bundle_dir = Path(out_dir) if out_dir else Path(runs_dir) / job_id if runs_dir else workspace_root() / RUNS_DIR / job_id

    external_query_manifest: Path | None = Path(query_manifest_path) if query_manifest_path else None
    if external_query_manifest is not None:
        queries = _query_rows_from_records(load_queries(external_query_manifest))
        query_manifest_info = _query_manifest_info(external_query_manifest)
        schema_version = GEO_JOB_V2
    else:
        queries = _normalize_queries(config.get("queries"))
        query_manifest_info = {
            "source_type": "config_inline",
            "source_uri": str(config_path),
            "source_uri_base": str(Path.cwd()),
            "schema_version": QUERY_MANIFEST_V1,
            "sha256": "",
            "row_count": len(queries),
        }
        schema_version = GEO_JOB_V1
    repeats = _positive_int(config.get("repeats", 20), "repeats")
    web_search_limit = _bounded_int(config.get("web_search_limit", settings.web_search_limit), "web_search_limit", minimum=1, maximum=20)
    concurrency = _positive_int(config.get("concurrency", settings.concurrency), "concurrency")
    if concurrency > 8:
        raise JobError("concurrency 必须在 1 到 8 之间")
    start_interval_seconds = _non_negative_float(config.get("start_interval_seconds", 0.0), "start_interval_seconds")
    model = str(config.get("model") or settings.llm_model).strip()
    if not model:
        raise JobError("model 不能为空")
    target_brand = _required_str(config, "target_brand")
    industry = _required_str(config, "industry")
    market = _optional_str(config, "market", default="未指定市场")
    target_aliases = _string_list(config.get("target_aliases"), "target_aliases")

    manifest = {
        "schema_version": schema_version,
        "job_id": job_id,
        "status": "built",
        "target_brand": target_brand,
        "target_aliases": target_aliases,
        "industry": industry,
        "market": market,
        "repeats": repeats,
        "model": model,
        "web_search_limit": web_search_limit,
        "concurrency": concurrency,
        "start_interval_seconds": start_interval_seconds,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "query_count": len(queries),
        "query_manifest": query_manifest_info,
        "paths": _manifest_paths(),
    }
    if external_query_manifest is None:
        manifest["queries"] = queries
    _validate_job_manifest(manifest)

    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        if not force:
            raise JobError(f"任务目录已存在且非空：{bundle_dir}。如需覆盖，请使用 --force")
        _assert_safe_force_target(bundle_dir)
        with _job_lock(bundle_dir / BUNDLE_LOCK):
            _assert_safe_force_target(bundle_dir)
            _clear_bundle_for_force(bundle_dir)
            return _materialize_job_bundle(bundle_dir, manifest, queries)
    return _materialize_job_bundle(bundle_dir, manifest, queries)


def _materialize_job_bundle(bundle_dir: Path, manifest: dict[str, Any], queries: list[dict[str, Any]]) -> dict[str, Any]:
    for name in [WORK_DIR, RAW_DIR, RESULT_DIR, LOGS_DIR]:
        (bundle_dir / name).mkdir(parents=True, exist_ok=True)
    _write_json(bundle_dir / JOB_MANIFEST, manifest)
    query_manifest = bundle_dir / QUERY_MANIFEST
    source_path = _resolve_query_manifest_source(manifest)
    if source_path is not None and source_path.exists():
        shutil.copyfile(source_path, query_manifest)
    else:
        _write_query_manifest(query_manifest, queries)
    return {
        "bundle_dir": str(bundle_dir),
        "job_manifest": str(bundle_dir / JOB_MANIFEST),
        "query_manifest": str(query_manifest),
        **manifest,
    }


def validate_job_config(
    config_path: str | Path,
    settings: Settings | None = None,
    *,
    query_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    config = _load_job_config(config_path)
    _validate_job_config_keys(config)
    query_manifest_info: dict[str, Any] | None = None
    if query_manifest_path is not None:
        manifest_path = Path(query_manifest_path)
        records = load_queries(manifest_path)
        queries = _query_rows_from_records(records)
        query_manifest_info = _query_manifest_info(manifest_path)
    else:
        queries = _normalize_queries(config.get("queries"))
    repeats = _positive_int(config.get("repeats", 20), "repeats")
    web_search_limit = _bounded_int(config.get("web_search_limit", settings.web_search_limit), "web_search_limit", minimum=1, maximum=20)
    concurrency = _positive_int(config.get("concurrency", settings.concurrency), "concurrency")
    if concurrency > 8:
        raise JobError("concurrency 必须在 1 到 8 之间")
    start_interval_seconds = _non_negative_float(config.get("start_interval_seconds", 0.0), "start_interval_seconds")
    model = str(config.get("model") or settings.llm_model).strip()
    if not model:
        raise JobError("model 不能为空")
    result = {
        "target_brand": _required_str(config, "target_brand"),
        "target_aliases": _string_list(config.get("target_aliases"), "target_aliases"),
        "industry": _required_str(config, "industry"),
        "market": _optional_str(config, "market", default="未指定市场"),
        "query_count": len(queries),
        "repeats": repeats,
        "planned_units": len(queries) * repeats,
        "model": model,
        "web_search_limit": web_search_limit,
        "concurrency": concurrency,
        "start_interval_seconds": start_interval_seconds,
    }
    if query_manifest_info is not None:
        result["query_manifest"] = query_manifest_info
    return result


def run_job_bundle(
    bundle_dir: str | Path,
    *,
    resume: bool = True,
    dry_run: bool = False,
    mock: bool = False,
    sleep_seconds: float = 0.0,
    start_interval_seconds: float | None = None,
    limit: int | None = None,
    only_query_ids: list[str] | None = None,
    query_manifest_path: str | Path | None = None,
    settings: Settings | None = None,
    confirm_cost: bool = False,
) -> dict[str, Any]:
    settings = settings or get_settings()
    root = Path(bundle_dir)
    manifest = load_job_manifest(root)
    raw_path = root / RAW_ATTEMPTS
    started_at = utc_now_iso()
    with _job_lock(root / BUNDLE_LOCK):
        query_manifest = ensure_query_manifest(root, manifest, replacement_path=query_manifest_path)
        if not query_manifest.exists():
            raise JobError(f"缺少 query_manifest.csv：{query_manifest}")
        all_queries = load_queries(query_manifest)
        queries = select_queries(all_queries, limit=limit, only_query_ids=only_query_ids)
        selected_units = _expected_units_for_queries(queries, int(manifest["repeats"]))
        analysis_statuses = _run_completion_statuses(dry_run=dry_run, mock=mock)
        resume_matched_before = _resume_matched_unit_count(raw_path, queries, manifest, settings) if resume else 0
        live_remaining = 0 if dry_run or mock else max(0, len(selected_units) - resume_matched_before)
        if live_remaining and not confirm_cost:
            raise JobError("真实 live 调用会产生 API 成本；请确认预算后显式传入 confirm_cost=True")
        runner = MonitorRunner(settings)
        update_job_manifest(root, status="running")
        try:
            results = runner.run(
                queries,
                output_path=raw_path,
                job_id=str(manifest["job_id"]),
                run_id=str(manifest["job_id"]),
                dry_run=dry_run,
                mock=mock,
                resume=resume,
                model=str(manifest["model"]),
                web_search_limit=int(manifest["web_search_limit"]),
                repeats=int(manifest["repeats"]),
                repeat_order="round-robin",
                sleep_seconds=sleep_seconds,
                start_interval_seconds=float(start_interval_seconds if start_interval_seconds is not None else manifest.get("start_interval_seconds", 0.0)),
                concurrency=int(manifest["concurrency"]),
            )
        except Exception:
            update_job_manifest(root, status="run_failed")
            raise
        completed_at = utc_now_iso()
        all_units = _expected_units_for_queries(all_queries, int(manifest["repeats"]))
        planned_units = len(selected_units)
        completed_units = _completed_unit_count(raw_path, analysis_statuses, expected_units=selected_units)
        job_completed_units = _completed_unit_count(raw_path, analysis_statuses, expected_units=all_units)
        executed_completed = sum(1 for item in results if item.status in analysis_statuses)
        summary = {
            "job_id": manifest.get("job_id"),
            "run_id": manifest.get("job_id"),
            "planned_units": planned_units,
            "job_planned_units": len(all_units),
            "completed_units": completed_units,
            "job_completed_units": job_completed_units,
            "selected_query_ids": [query.query_id for query in queries],
            "executed": len(results),
            "skipped": resume_matched_before if resume else 0,
            "errors": sum(1 for item in results if item.status == "error"),
            "raw_path": str(raw_path),
            "started_at": started_at,
            "completed_at": completed_at,
            "mode": "dry_run" if dry_run else "mock" if mock else "live",
        }
        _write_json(root / RUN_SUMMARY, summary)
        status = "ran" if summary["errors"] == 0 and job_completed_units == len(all_units) else "ran_partial"
        update_job_manifest(root, status=status)
    return {
        "bundle_dir": str(root),
        "raw_jsonl": str(raw_path),
        "run_id": str(manifest.get("job_id")),
        "executed": len(results),
        "errors": summary["errors"],
        "completed_units": summary["completed_units"],
        "job_completed_units": summary["job_completed_units"],
        "skipped": summary["skipped"],
    }


def estimate_job_run(
    bundle_dir: str | Path,
    *,
    dry_run: bool = False,
    mock: bool = False,
    resume: bool = True,
    limit: int | None = None,
    only_query_ids: list[str] | None = None,
    query_manifest_path: str | Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    root = Path(bundle_dir)
    manifest = load_job_manifest(root)
    if query_manifest_path is not None:
        _ensure_manifest_file_fingerprint(Path(query_manifest_path), manifest)
        all_queries = load_queries(query_manifest_path)
    else:
        all_queries = load_job_queries(root, manifest, materialize=False)
    queries = select_queries(all_queries, limit=limit, only_query_ids=only_query_ids)
    planned_units = len(queries) * int(manifest["repeats"])
    statuses = _run_completion_statuses(dry_run=dry_run, mock=mock)
    completed_units = _completed_unit_count(root / RAW_ATTEMPTS, statuses, expected_units=_expected_units_for_queries(queries, int(manifest["repeats"])))
    resume_matched_units = _resume_matched_unit_count(root / RAW_ATTEMPTS, queries, manifest, settings) if resume else 0
    if dry_run or mock:
        sampling_requests_remaining = 0
    elif resume:
        sampling_requests_remaining = max(0, planned_units - resume_matched_units)
    else:
        sampling_requests_remaining = planned_units
    analysis_extraction_requests = planned_units
    analysis_canonicalization_requests = 1 if planned_units else 0
    return {
        "job_id": manifest.get("job_id"),
        "mode": "dry_run" if dry_run else "mock" if mock else "live",
        "query_count": len(queries),
        "job_query_count": manifest["query_count"],
        "repeats": manifest["repeats"],
        "planned_units": planned_units,
        "completed_units": completed_units,
        "resume_matched_units": resume_matched_units,
        "resume": resume,
        "sampling_requests_remaining": sampling_requests_remaining,
        "analysis_extraction_requests_estimate": analysis_extraction_requests,
        "analysis_canonicalization_requests_estimate": analysis_canonicalization_requests,
        "analysis_llm_requests_estimate": analysis_extraction_requests + analysis_canonicalization_requests,
        "concurrency": manifest["concurrency"],
        "start_interval_seconds": manifest.get("start_interval_seconds", 0.0),
        "web_search_limit": manifest["web_search_limit"],
        "model": manifest["model"],
    }


def load_job_manifest(bundle_dir: str | Path) -> dict[str, Any]:
    path = Path(bundle_dir) / JOB_MANIFEST
    if not path.exists():
        raise JobError(f"缺少 job_manifest.json：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") not in {GEO_JOB_V1, GEO_JOB_V2}:
        raise JobError("job_manifest schema_version 必须是 geo-job-v1 或 geo-job-v2")
    _validate_job_manifest(data)
    return data


def raw_attempts_path(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir) / RAW_ATTEMPTS


def query_manifest_path(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir) / QUERY_MANIFEST


def load_job_queries(bundle_dir: str | Path, manifest: dict[str, Any] | None = None, *, materialize: bool = False) -> list[QueryRecord]:
    root = Path(bundle_dir)
    manifest = manifest or load_job_manifest(root)
    query_manifest = resolve_query_manifest(root, manifest, materialize=materialize)
    if query_manifest.exists():
        return load_queries(query_manifest)
    source = _resolve_query_manifest_source(manifest)
    if source is not None and source.exists():
        _ensure_manifest_file_fingerprint(source, manifest)
        return load_queries(source)
    return _query_records_from_manifest(manifest)


def resolve_query_manifest(bundle_dir: str | Path, manifest: dict[str, Any] | None = None, *, materialize: bool = False) -> Path:
    root = Path(bundle_dir)
    current = root / QUERY_MANIFEST
    manifest = manifest or load_job_manifest(root)
    if current.exists():
        _ensure_query_manifest_matches(current, manifest)
        return current
    queries = manifest.get("queries")
    if not isinstance(queries, list) or not queries:
        if materialize:
            source = _resolve_query_manifest_source(manifest)
            if source is not None and source.exists():
                _ensure_manifest_file_fingerprint(source, manifest)
                current.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, current)
                return current
        return current
    if materialize:
        _write_query_manifest(current, queries)
    return current


def ensure_query_manifest(bundle_dir: str | Path, manifest: dict[str, Any] | None = None, *, replacement_path: str | Path | None = None) -> Path:
    root = Path(bundle_dir)
    manifest = manifest or load_job_manifest(root)
    current = root / QUERY_MANIFEST
    if replacement_path is not None:
        replacement = Path(replacement_path)
        _ensure_manifest_file_fingerprint(replacement, manifest)
        current.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(replacement, current)
    return resolve_query_manifest(root, manifest, materialize=True)


def work_dir(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir) / WORK_DIR


def result_dir(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir) / RESULT_DIR


def logs_dir(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir) / LOGS_DIR


def cleanup_job_bundle(bundle_dir: str | Path) -> dict[str, Any]:
    root = Path(bundle_dir)
    with _job_lock(root / BUNDLE_LOCK):
        return _cleanup_job_bundle_unlocked(root)


def _cleanup_job_bundle_unlocked(bundle_dir: str | Path) -> dict[str, Any]:
    root = Path(bundle_dir)
    manifest = load_job_manifest(root)
    previous_status = str(manifest.get("status") or "")
    path = work_dir(root)
    removed = path.exists()
    if removed:
        shutil.rmtree(path)
    summary = {
        "job_id": manifest.get("job_id"),
        "previous_status": previous_status,
        "removed_work_dir": removed,
        "work_dir": str(path),
        "completed_at": utc_now_iso(),
    }
    _write_json(root / CLEANUP_SUMMARY, summary)
    if previous_status in {"analyzed", "analyzed_partial"}:
        next_status = f"{previous_status}_cleaned"
    else:
        next_status = "cleaned"
    update_job_manifest(root, status=next_status)
    return {"bundle_dir": str(root), **summary}


def update_job_manifest(bundle_dir: str | Path, *, status: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(bundle_dir)
    manifest = load_job_manifest(root)
    if status:
        manifest["status"] = status
    manifest["updated_at"] = utc_now_iso()
    manifest["paths"] = _manifest_paths()
    if extra:
        manifest.update(extra)
    _validate_job_manifest(manifest)
    _write_json(root / JOB_MANIFEST, manifest)
    return manifest


def query_set_hash(manifest: dict[str, Any]) -> str:
    info = manifest.get("query_manifest")
    if isinstance(info, dict) and info.get("sha256"):
        return str(info["sha256"])[:16]
    queries = [{"query_id": str(row.get("query_id", "")), "query": str(row.get("query", ""))} for row in manifest.get("queries", [])]
    stable = json.dumps(queries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def _load_job_config(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise JobError(f"任务配置不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise JobError(f"job_config JSON 格式错误：{path}:{exc.lineno}:{exc.colno}，请检查 JSON 格式") from exc
    if not isinstance(data, dict):
        raise JobError("job_config.json 必须是 JSON 对象")
    return data


def _validate_job_config_keys(config: dict[str, Any]) -> None:
    unknown = sorted(set(config) - JOB_CONFIG_KEYS)
    if unknown:
        raise JobError(f"job_config 包含未知字段：{', '.join(unknown)}")


def _normalize_queries(value: Any) -> list[dict[str, Any]]:
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
            row = {k: v for k, v in item.items() if v not in (None, "")}
            row["query_id"] = query_id
            row["query"] = query
        else:
            raise JobError("queries 只能包含字符串或对象")
        if not query:
            raise JobError(f"queries 第 {index} 项为空")
        if not query_id:
            raise JobError(f"queries 第 {index} 项 query_id 不能为空")
        if query_id in seen:
            raise JobError(f"query_id 重复：{query_id}")
        seen.add(query_id)
        if isinstance(row.get("tags"), list):
            row["tags"] = ",".join(str(tag).strip() for tag in row["tags"] if str(tag).strip())
        rows.append(row)
    return rows


def _write_query_manifest(path: Path, queries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    preferred = ["query_id", "query", "locale", "market", "category", "tags", "stage", "persona"]
    extra = sorted({key for row in queries for key in row.keys()} - set(preferred))
    fieldnames = [key for key in preferred if any(key in row for row in queries)] + extra
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(queries)


def _query_rows_from_records(records: list[QueryRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        _validate_slug_id(record.query_id, "query_id")
        row: dict[str, Any] = {
            "query_id": record.query_id,
            "query": record.query,
        }
        if record.locale:
            row["locale"] = record.locale
        if record.market:
            row["market"] = record.market
        if record.category:
            row["category"] = record.category
        if record.tags:
            row["tags"] = ",".join(record.tags)
        row.update(record.metadata)
        for key in ["seed_id", "persona", "template_id", "variant_id"]:
            if row.get(key):
                _validate_slug_id(str(row[key]), key)
        rows.append(row)
    return rows


def _query_manifest_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise JobError(f"query manifest 不存在：{path}")
    records = load_queries(path)
    _query_rows_from_records(records)
    return {
        "source_type": "external_file",
        "source_uri": str(path),
        "source_uri_base": str(Path.cwd()),
        "schema_version": QUERY_MANIFEST_V1,
        "sha256": _file_sha256(path),
        "row_count": len(records),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_query_manifest_source(manifest: dict[str, Any]) -> Path | None:
    info = manifest.get("query_manifest")
    if not isinstance(info, dict):
        return None
    if info.get("source_type") != "external_file":
        return None
    source_uri = info.get("source_uri")
    if not source_uri:
        return None
    path = Path(str(source_uri))
    if path.is_absolute():
        return path
    base = info.get("source_uri_base")
    return (Path(str(base)) / path) if base else path


def _ensure_manifest_file_fingerprint(path: Path, manifest: dict[str, Any]) -> None:
    if not path.exists():
        raise JobError(f"query manifest 不存在：{path}")
    info = manifest.get("query_manifest")
    if not isinstance(info, dict):
        return
    expected_sha = str(info.get("sha256") or "")
    expected_count = int(info.get("row_count") or 0)
    if expected_sha and _file_sha256(path) != expected_sha:
        raise JobError(f"query manifest sha256 不匹配：{path}")
    records = load_queries(path)
    if expected_count and len(records) != expected_count:
        raise JobError(f"query manifest row_count 不匹配：{path}")
    _query_rows_from_records(records)


def _validate_slug_id(value: str, field: str) -> None:
    if not SLUG_RE.fullmatch(value):
        raise JobError(f"{field} 只能包含 [a-zA-Z0-9_-]：{value}")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _manifest_paths() -> dict[str, str]:
    return {
        "query_manifest": QUERY_MANIFEST,
        "raw_attempts": RAW_ATTEMPTS,
        "work_dir": WORK_DIR,
        "result_dir": RESULT_DIR,
        "logs_dir": LOGS_DIR,
        "run_summary": RUN_SUMMARY,
        "cleanup_summary": CLEANUP_SUMMARY,
    }


def _validate_job_manifest(data: dict[str, Any]) -> None:
    required = ["schema_version", "job_id", "status", "target_brand", "industry", "market", "repeats", "model", "web_search_limit", "concurrency", "query_count"]
    missing = [key for key in required if key not in data]
    if missing:
        raise JobError(f"job_manifest 缺少字段：{', '.join(missing)}")
    schema_version = str(data.get("schema_version") or "")
    if str(data.get("status") or "") not in ALLOWED_STATUSES:
        raise JobError(f"job_manifest status 无效：{data.get('status')}")
    if schema_version == GEO_JOB_V1:
        if not isinstance(data.get("queries"), list) or not data["queries"]:
            raise JobError("job_manifest queries 必须是非空数组")
        if _positive_int(data.get("query_count"), "query_count") != len(data["queries"]):
            raise JobError("job_manifest query_count 与 queries 数量不一致")
    else:
        if not isinstance(data.get("query_manifest"), dict):
            raise JobError("geo-job-v2 必须包含 query_manifest")
        info = data["query_manifest"]
        for key in ["source_type", "schema_version", "sha256", "row_count"]:
            if key not in info:
                raise JobError(f"query_manifest 缺少字段：{key}")
        if _positive_int(data.get("query_count"), "query_count") != _positive_int(info.get("row_count"), "query_manifest.row_count"):
            raise JobError("job_manifest query_count 与 query_manifest.row_count 不一致")
    _positive_int(data.get("repeats"), "repeats")
    _bounded_int(data.get("web_search_limit"), "web_search_limit", minimum=1, maximum=20)
    concurrency = _positive_int(data.get("concurrency"), "concurrency")
    if concurrency > 8:
        raise JobError("concurrency 必须在 1 到 8 之间")
    _non_negative_float(data.get("start_interval_seconds", 0.0), "start_interval_seconds")
    if not str(data.get("model") or "").strip():
        raise JobError("model 不能为空")
    _required_str(data, "target_brand")
    _string_list(data.get("target_aliases"), "target_aliases")
    _required_str(data, "industry")
    _optional_str(data, "market", default="未指定市场")
    if schema_version == GEO_JOB_V1:
        _validate_persisted_queries(data.get("queries"))


def _make_job_id() -> str:
    import uuid

    stamp = utc_now_iso().replace("+00:00", "Z").replace("-", "").replace(":", "")
    return f"job_{stamp}_{uuid.uuid4().hex[:6]}"


def _required_str(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise JobError(f"{key} 不能为空")
    return value


def _optional_str(data: dict[str, Any], key: str, *, default: str) -> str:
    value = str(data.get(key) or "").strip()
    return value or default


def _string_list(value: Any, key: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise JobError(f"{key} 必须是字符串数组")
    out = [str(item).strip() for item in value if str(item).strip()]
    return out


def _bounded_int(value: Any, key: str, *, minimum: int, maximum: int) -> int:
    parsed = _positive_int(value, key)
    if parsed < minimum or parsed > maximum:
        raise JobError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def _non_negative_float(value: Any, key: str) -> float:
    try:
        parsed = float(value)
    except Exception as exc:
        raise JobError(f"{key} 必须是非负数") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise JobError(f"{key} 必须是非负数")
    return parsed


def _positive_int(value: Any, key: str) -> int:
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


def _validate_persisted_queries(value: Any) -> None:
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


def _query_records_from_manifest(manifest: dict[str, Any]) -> list[QueryRecord]:
    records: list[QueryRecord] = []
    known = {"query_id", "query", "locale", "market", "category", "tags"}
    for row in manifest.get("queries", []):
        tags = _normalize_metadata_value(row.get("tags"))
        records.append(
            QueryRecord(
                query_id=str(row.get("query_id") or "").strip(),
                query=str(row.get("query") or "").strip(),
                locale=(str(row["locale"]).strip() if row.get("locale") else None),
                market=(str(row["market"]).strip() if row.get("market") else None),
                category=(str(row["category"]).strip() if row.get("category") else None),
                tags=[item.strip() for item in str(tags).split(",") if item.strip()],
                metadata={key: value for key, value in row.items() if key not in known and value not in (None, "")},
            )
        )
    return records


def _ensure_query_manifest_matches(path: Path, manifest: dict[str, Any]) -> None:
    if str(manifest.get("schema_version")) == GEO_JOB_V2:
        _ensure_manifest_file_fingerprint(path, manifest)
        return
    loaded_queries = load_queries(path)
    query_pairs = [(row.query_id, row.query) for row in loaded_queries]
    manifest_pairs = [(str(row.get("query_id")), str(row.get("query"))) for row in manifest.get("queries", [])]
    if query_pairs != manifest_pairs:
        raise JobError(f"{path.name} 与 job_manifest queries 不一致，请删除旧文件或重新 build-job")
    loaded_by_id = {row.query_id: row for row in loaded_queries}
    for manifest_row in manifest.get("queries", []):
        query_id = str(manifest_row.get("query_id"))
        loaded = loaded_by_id.get(query_id)
        if loaded is None:
            continue
        metadata = loaded.metadata_with_tags()
        for key, expected in manifest_row.items():
            if key in {"query_id", "query"}:
                continue
            actual = metadata.get(key)
            if _normalize_metadata_value(actual) != _normalize_metadata_value(expected):
                raise JobError(f"{path.name} 与 job_manifest query 元数据不一致：{query_id}.{key}")


def _run_completion_statuses(*, dry_run: bool, mock: bool) -> set[str]:
    if dry_run:
        return {"dry_run"}
    if mock:
        return {"mock"}
    return {"success"}


def _expected_units_for_queries(queries: list[Any], repeats: int) -> set[tuple[str, int]]:
    return {(str(query.query_id), repeat) for query in queries for repeat in range(1, repeats + 1)}


def _completed_unit_count(raw_path: Path, statuses: set[str], *, expected_units: set[tuple[str, int]] | None = None) -> int:
    if not raw_path.exists():
        return 0
    records = latest_records(read_jsonl(raw_path, strict=False), statuses=statuses)
    if expected_units is None:
        return len(records)
    return sum(1 for record in records if (str(record.get("query_id")), int(record.get("repeat_index") or 1)) in expected_units)


def _resume_matched_unit_count(raw_path: Path, queries: list[QueryRecord], manifest: dict[str, Any], settings: Settings) -> int:
    done_hashes = successful_result_hashes(raw_path)
    if not done_hashes:
        return 0
    count = 0
    repeats = int(manifest["repeats"])
    model = str(manifest["model"])
    web_search_limit = int(manifest["web_search_limit"])
    for query in queries:
        payload = build_responses_payload(query, settings, model=model, web_search_limit=web_search_limit)
        request_hash = compute_request_hash(payload)
        for repeat_index in range(1, repeats + 1):
            if request_hash in done_hashes.get((query.query_id, repeat_index), set()):
                count += 1
    return count


class _job_lock:
    def __init__(self, path: Path, *, timeout_seconds: float = 0.0, stale_seconds: float = 7 * 24 * 60 * 60):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self.fd: int | None = None
        self.token: str | None = None

    def __enter__(self) -> "_job_lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                self.token = f"{os.getpid()}-{time.time_ns()}"
                os.write(self.fd, json.dumps({"pid": os.getpid(), "token": self.token, "created_at": utc_now_iso()}).encode("utf-8"))
                return self
            except FileExistsError:
                stale_stat = self._stale_stat()
                if stale_stat is not None and self._unlink_if_same(stale_stat):
                    continue
                if time.monotonic() >= deadline:
                    raise JobError(f"任务正在运行，请稍后重试：{self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        if not self._owns_lock():
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _stale_stat(self) -> os.stat_result | None:
        try:
            stat = self.path.stat()
            data = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            if pid > 0 and _pid_exists(pid):
                return stat if time.time() - stat.st_mtime > self.stale_seconds else None
            if pid > 0:
                return stat
            return stat if time.time() - stat.st_mtime > self.stale_seconds else None
        except FileNotFoundError:
            return None
        except Exception:
            try:
                stat = self.path.stat()
                return stat if time.time() - stat.st_mtime > self.stale_seconds else None
            except FileNotFoundError:
                return None

    def _unlink_if_same(self, stale_stat: os.stat_result) -> bool:
        try:
            current = self.path.stat()
        except FileNotFoundError:
            return True
        if (current.st_dev, current.st_ino, current.st_mtime_ns) != (stale_stat.st_dev, stale_stat.st_ino, stale_stat.st_mtime_ns):
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True

    def _owns_lock(self) -> bool:
        if not self.token:
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return data.get("token") == self.token


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _normalize_metadata_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _assert_safe_force_target(bundle_dir: Path) -> None:
    resolved = bundle_dir.resolve()
    dangerous = {Path("/").resolve(), Path.home().resolve(), workspace_root().resolve()}
    if resolved in dangerous:
        raise JobError(f"--force 拒绝覆盖危险目录：{bundle_dir}")
    load_job_manifest(bundle_dir)


def _clear_bundle_for_force(bundle_dir: Path) -> None:
    lock_path = bundle_dir / BUNDLE_LOCK
    for child in list(bundle_dir.iterdir()):
        if child == lock_path.parent:
            if child.is_symlink():
                raise JobError(f"--force 拒绝清理 symlink 日志目录：{child}")
            for log_child in list(child.iterdir()):
                if log_child == lock_path:
                    continue
                if log_child.is_dir() and not log_child.is_symlink():
                    shutil.rmtree(log_child)
                else:
                    log_child.unlink()
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
