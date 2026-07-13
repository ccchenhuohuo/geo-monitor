"""Job lifecycle orchestration and stable public facade."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .config import LiveSettingsError, Settings, get_settings, validate_live_settings, validate_provider_settings, workspace_root
from .dataset import load_queries, select_queries
from .filesystem import UnsafeOutputPathError, ensure_private_directory, prepare_private_output, secure_private_file
from .jobs.cleanup import cleanup_job_bundle, cleanup_job_work_dir_unlocked
from .jobs.config import (
    ensure_unit_limit,
    load_validated_job_config,
    make_job_id,
    normalize_queries,
    validate_job_spec,
)
from .jobs.contracts import (
    BUNDLE_LOCK,
    DIAGNOSTIC_RUN_SUMMARY,
    GEO_JOB_V3,
    JOB_MANIFEST,
    LOGS_DIR,
    QUERY_MANIFEST,
    QUERY_MANIFEST_V1,
    RAW_ATTEMPTS,
    RAW_DIR,
    RESULT_DIR,
    RUN_SUMMARY,
    WORK_DIR,
    JobError,
    QueryManifestIntegrityError,
    QueryManifestSourceError,
)
from .jobs.layout import logs_dir, raw_attempts_path, result_dir, work_dir
from .jobs.locking import JobBundleLock, job_bundle_lock
from .jobs.manifest import (
    load_job_manifest,
    manifest_paths,
    query_set_hash,
    update_job_manifest,
    validate_job_manifest,
    write_json,
)
from .jobs.profiles import validate_runtime_profile
from .jobs.query_manifest import (
    ensure_manifest_file_fingerprint,
    ensure_query_manifest,
    load_job_queries,
    query_rows_from_records,
    resolve_query_manifest_source,
    write_query_manifest,
)
from .jobs.query_manifest import (
    query_manifest_info as inspect_query_manifest,
)
from .jobs.runtime import (
    completed_unit_count,
    expected_units_for_queries,
    resume_matched_unit_count,
    run_completion_statuses,
)
from .runner import MonitorRunner, make_run_id
from .schemas import utc_now_iso

__all__ = [
    "BUNDLE_LOCK",
    "JOB_MANIFEST",
    "RAW_ATTEMPTS",
    "JobError",
    "QueryManifestIntegrityError",
    "QueryManifestSourceError",
    "build_job_bundle",
    "cleanup_job_bundle",
    "cleanup_job_work_dir_unlocked",
    "estimate_job_run",
    "job_bundle_lock",
    "load_job_manifest",
    "load_job_queries",
    "logs_dir",
    "query_set_hash",
    "raw_attempts_path",
    "result_dir",
    "run_job_bundle",
    "update_job_manifest",
    "validate_job_config",
    "work_dir",
]


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
    config = load_validated_job_config(config_path)
    if out_dir is not None and runs_dir is not None:
        raise JobError("--out-dir 和 --runs-dir 不能同时使用")
    if out_dir is None and runs_dir is None:
        raise JobError("必须显式指定 --out-dir 或 --runs-dir")
    job_id = make_job_id()
    bundle_dir = Path(out_dir) if out_dir is not None else Path(runs_dir) / job_id

    external_query_manifest: Path | None = Path(query_manifest_path) if query_manifest_path else None
    if external_query_manifest is not None:
        queries = query_rows_from_records(load_queries(external_query_manifest))
        query_manifest_info = inspect_query_manifest(external_query_manifest)
    else:
        queries = normalize_queries(config.get("queries"))
        query_manifest_info = {
            "source_type": "config_inline",
            "source_uri": str(config_path),
            "source_uri_base": str(Path.cwd()),
            "schema_version": QUERY_MANIFEST_V1,
            "sha256": "",
            "row_count": len(queries),
        }
    spec = validate_job_spec(
        config,
        queries,
        settings,
        context="构建 job",
        query_manifest_info=query_manifest_info,
    )
    schema_version = GEO_JOB_V3

    manifest = {
        "schema_version": schema_version,
        "job_id": job_id,
        "status": "built",
        "target_brand": spec.target_brand,
        "target_aliases": spec.target_aliases,
        "owned_domains": spec.owned_domains,
        "industry": spec.industry,
        "market": spec.market,
        "repeats": spec.repeats,
        "model": spec.model,
        "web_search_limit": spec.web_search_limit,
        "adapter": spec.adapter,
        "adapter_options": spec.adapter_options,
        "sampling_profile": spec.sampling_profile,
        "analysis_profile": spec.analysis_profile,
        "concurrency": spec.concurrency,
        "start_interval_seconds": spec.start_interval_seconds,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "query_count": len(queries),
        "query_manifest": query_manifest_info,
        "comparability_profile": spec.comparability_profile,
        "paths": manifest_paths(),
    }
    if external_query_manifest is None:
        manifest["queries"] = queries
    validate_job_manifest(manifest)

    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        if not force:
            raise JobError(f"任务目录已存在且非空：{bundle_dir}。如需覆盖，请使用 --force")
        _assert_safe_force_target(bundle_dir)
        with JobBundleLock(bundle_dir / BUNDLE_LOCK):
            _assert_safe_force_target(bundle_dir)
            _clear_bundle_for_force(bundle_dir)
            return _materialize_job_bundle(bundle_dir, manifest, queries)
    return _materialize_job_bundle(bundle_dir, manifest, queries)


def _materialize_job_bundle(bundle_dir: Path, manifest: dict[str, Any], queries: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        ensure_private_directory(bundle_dir)
        for name in [WORK_DIR, RAW_DIR, RESULT_DIR, LOGS_DIR]:
            ensure_private_directory(bundle_dir / name)
    except UnsafeOutputPathError as exc:
        raise JobError(str(exc)) from exc
    write_json(bundle_dir / JOB_MANIFEST, manifest)
    query_manifest = bundle_dir / QUERY_MANIFEST
    source_path = resolve_query_manifest_source(manifest, bundle_dir=bundle_dir)
    if source_path is not None and source_path.exists():
        try:
            prepare_private_output(query_manifest)
            shutil.copyfile(source_path, query_manifest)
            secure_private_file(query_manifest)
        except UnsafeOutputPathError as exc:
            raise JobError(str(exc)) from exc
    else:
        write_query_manifest(query_manifest, queries)
    return {
        **manifest,
        "bundle_dir": str(bundle_dir),
        "job_manifest": str(bundle_dir / JOB_MANIFEST),
        "query_manifest": str(query_manifest),
    }


def validate_job_config(
    config_path: str | Path,
    settings: Settings | None = None,
    *,
    query_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    config = load_validated_job_config(config_path)
    query_manifest_info: dict[str, Any] | None = None
    if query_manifest_path is not None:
        manifest_path = Path(query_manifest_path)
        records = load_queries(manifest_path)
        queries = query_rows_from_records(records)
        query_manifest_info = inspect_query_manifest(manifest_path)
    else:
        queries = normalize_queries(config.get("queries"))
    return validate_job_spec(
        config,
        queries,
        settings,
        context="校验 job",
        query_manifest_info=query_manifest_info,
    ).validation_result()


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
    manifest = load_job_manifest(root, settings=settings)
    raw_path = root / RAW_ATTEMPTS
    started_at = utc_now_iso()
    run_execution_id = make_run_id()
    with JobBundleLock(root / BUNDLE_LOCK):
        query_manifest = ensure_query_manifest(root, manifest, replacement_path=query_manifest_path)
        if not query_manifest.exists():
            raise JobError(f"缺少 query_manifest.csv：{query_manifest}")
        all_queries = load_queries(query_manifest)
        queries = select_queries(all_queries, limit=limit, only_query_ids=only_query_ids)
        selected_units = expected_units_for_queries(queries, int(manifest["repeats"]))
        ensure_unit_limit(len(selected_units), settings, context="运行 job")
        analysis_statuses = run_completion_statuses(dry_run=dry_run, mock=mock)
        resume_matched_before = resume_matched_unit_count(raw_path, queries, manifest, settings) if resume else 0
        live_remaining = 0 if dry_run or mock else max(0, len(selected_units) - resume_matched_before)
        will_execute = bool(selected_units) and (not resume or resume_matched_before < len(selected_units))
        validate_runtime_profile(
            manifest,
            settings,
            require_request_match=will_execute,
            require_endpoint_match=live_remaining > 0,
        )
        if live_remaining and not confirm_cost:
            raise JobError("真实 live 调用会产生 API 成本；请确认预算后显式传入 confirm_cost=True")
        if live_remaining:
            try:
                provider_name = str(manifest["sampling_profile"].get("provider") or "openai_compatible")
                if provider_name == "openai_compatible":
                    validate_live_settings(settings)
                else:
                    validate_provider_settings(settings, provider_name)
            except LiveSettingsError as exc:
                raise JobError(str(exc)) from exc
        runner = MonitorRunner(settings)
        diagnostic_mode = dry_run or mock
        previous_status = str(manifest.get("status") or "")
        run_generation = int(manifest.get("run_generation") or 0)
        diagnostic_generation = int(manifest.get("diagnostic_generation") or 0)
        if will_execute:
            if diagnostic_mode:
                diagnostic_generation += 1
                manifest = update_job_manifest(
                    root,
                    settings=settings,
                    extra={
                        "diagnostic_generation": diagnostic_generation,
                        "last_diagnostic_execution_id": run_execution_id,
                        "last_diagnostic_mode": "dry_run" if dry_run else "mock",
                        "last_diagnostic_started_at": started_at,
                    },
                )
            else:
                run_generation += 1
                _invalidate_analysis_artifacts(root)
                manifest = update_job_manifest(
                    root,
                    status="running",
                    settings=settings,
                    extra={
                        "run_generation": run_generation,
                        "last_run_execution_id": run_execution_id,
                        "last_run_started_at": started_at,
                    },
                )
            try:
                results = runner.run(
                    queries,
                    output_path=raw_path,
                    job_id=str(manifest["job_id"]),
                    run_id=str(manifest["job_id"]),
                    run_execution_id=run_execution_id,
                    run_generation=run_generation,
                    diagnostic_generation=diagnostic_generation if diagnostic_mode else None,
                    dry_run=dry_run,
                    mock=mock,
                    resume=resume,
                    model=str(manifest["model"]),
                    web_search_limit=int(manifest["web_search_limit"]),
                    sampling_profile=dict(manifest["sampling_profile"]),
                    adapter_options=dict(manifest.get("adapter_options") or {}),
                    repeats=int(manifest["repeats"]),
                    repeat_order="round-robin",
                    sleep_seconds=sleep_seconds,
                    start_interval_seconds=float(start_interval_seconds if start_interval_seconds is not None else manifest.get("start_interval_seconds", 0.0)),
                    concurrency=int(manifest["concurrency"]),
                )
            except KeyboardInterrupt:
                if diagnostic_mode and previous_status.startswith("analyzed"):
                    update_job_manifest(
                        root,
                        settings=settings,
                        extra={
                            "last_diagnostic_execution_id": run_execution_id,
                            "last_diagnostic_interrupted_at": utc_now_iso(),
                        },
                    )
                else:
                    update_job_manifest(
                        root,
                        status="interrupted",
                        settings=settings,
                        extra={
                            "run_generation": run_generation,
                            "last_run_execution_id": run_execution_id,
                            "last_run_interrupted_at": utc_now_iso(),
                        },
                    )
                raise
            except Exception:
                if diagnostic_mode and previous_status.startswith("analyzed"):
                    update_job_manifest(
                        root,
                        settings=settings,
                        extra={
                            "last_diagnostic_execution_id": run_execution_id,
                            "last_diagnostic_failed_at": utc_now_iso(),
                        },
                    )
                else:
                    update_job_manifest(
                        root,
                        status="run_failed",
                        settings=settings,
                        extra={"last_run_execution_id": run_execution_id, "last_run_failed_at": utc_now_iso()},
                    )
                raise
        else:
            results = []
        runner_info = dict(runner.last_run_info)
        completed_at = utc_now_iso()
        all_units = expected_units_for_queries(all_queries, int(manifest["repeats"]))
        planned_units = len(selected_units)
        completed_units = completed_unit_count(raw_path, analysis_statuses, expected_units=selected_units)
        job_completed_units = completed_unit_count(raw_path, analysis_statuses, expected_units=all_units)
        summary = {
            "job_id": manifest.get("job_id"),
            "run_id": manifest.get("job_id"),
            "run_execution_id": run_execution_id,
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
            "run_generation": run_generation,
            "diagnostic_generation": diagnostic_generation if diagnostic_mode else None,
            "affects_analysis_generation": not diagnostic_mode,
            "circuit_breaker": bool(runner_info.get("circuit_breaker")),
            "circuit_breaker_details": runner_info if runner_info.get("circuit_breaker") else None,
        }
        if diagnostic_mode:
            write_json(root / DIAGNOSTIC_RUN_SUMMARY, summary)
            if not previous_status.startswith("analyzed"):
                write_json(root / RUN_SUMMARY, summary)
        else:
            write_json(root / RUN_SUMMARY, summary)
        status = "ran" if not summary["circuit_breaker"] and summary["errors"] == 0 and job_completed_units == len(all_units) else "ran_partial"
        if diagnostic_mode and previous_status.startswith("analyzed"):
            update_job_manifest(
                root,
                settings=settings,
                extra={
                    "diagnostic_generation": diagnostic_generation,
                    "last_diagnostic_execution_id": run_execution_id,
                    "last_diagnostic_completed_at": completed_at,
                },
            )
        elif diagnostic_mode:
            update_job_manifest(
                root,
                status=status,
                settings=settings,
                extra={
                    "diagnostic_generation": diagnostic_generation,
                    "last_diagnostic_execution_id": run_execution_id,
                    "last_diagnostic_completed_at": completed_at,
                },
            )
        elif will_execute or not previous_status.startswith("analyzed"):
            update_job_manifest(
                root,
                status=status,
                settings=settings,
                extra={
                    "run_generation": run_generation,
                    "last_run_completed_at": completed_at,
                    "last_run_circuit_breaker": runner_info if runner_info.get("circuit_breaker") else None,
                },
            )
    return {
        "bundle_dir": str(root),
        "raw_jsonl": str(raw_path),
        "run_id": str(manifest.get("job_id")),
        "run_execution_id": run_execution_id,
        "executed": len(results),
        "errors": summary["errors"],
        "completed_units": summary["completed_units"],
        "job_completed_units": summary["job_completed_units"],
        "skipped": summary["skipped"],
        "circuit_breaker": summary["circuit_breaker"],
        "circuit_breaker_details": summary["circuit_breaker_details"],
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
    manifest = load_job_manifest(root, settings=settings)
    if query_manifest_path is not None:
        ensure_manifest_file_fingerprint(Path(query_manifest_path), manifest)
        all_queries = load_queries(query_manifest_path)
    else:
        all_queries = load_job_queries(root, manifest, materialize=False)
    queries = select_queries(all_queries, limit=limit, only_query_ids=only_query_ids)
    planned_units = len(queries) * int(manifest["repeats"])
    statuses = run_completion_statuses(dry_run=dry_run, mock=mock)
    completed_units = completed_unit_count(root / RAW_ATTEMPTS, statuses, expected_units=expected_units_for_queries(queries, int(manifest["repeats"])))
    resume_matched_units = resume_matched_unit_count(root / RAW_ATTEMPTS, queries, manifest, settings) if resume else 0
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
        "adapter": manifest.get("adapter"),
        "sampling_profile": manifest.get("sampling_profile"),
        "analysis_profile": manifest.get("analysis_profile"),
    }


def _invalidate_analysis_artifacts(bundle_dir: Path) -> None:
    result = result_dir(bundle_dir)
    if result.is_symlink():
        raise JobError(f"拒绝清理 symlink result 目录：{result}")
    if result.exists():
        shutil.rmtree(result)
    try:
        ensure_private_directory(result)
    except UnsafeOutputPathError as exc:
        raise JobError(str(exc)) from exc
    logs = logs_dir(bundle_dir)
    if logs.is_symlink():
        raise JobError(f"拒绝写入 symlink logs 目录：{logs}")
    for name in [
        "analysis_summary.json",
        "analysis_summary.json.cache",
        "analysis_artifacts.json",
        "data_quality.json",
        "extraction_errors.jsonl",
        "raw_read_errors.jsonl",
    ]:
        try:
            (logs / name).unlink()
        except FileNotFoundError:
            pass


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
