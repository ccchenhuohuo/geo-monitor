"""Stable public Python API for GEO Brand Monitor."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .dashboard import build_dashboard as build_static_dashboard
from .db import build_duckdb
from .fanout import build_query_manifest
from .job import build_job_bundle, load_job_manifest, run_job_bundle
from .job_analysis import analyze_job_bundle

__all__ = ["GeoMonitorResult", "StudyPaths", "resolve_study_paths", "run_geo_monitor"]


@dataclass
class StudyPaths:
    study_dir: str | None = None
    runs_dir: str | None = None
    db_path: str | None = None
    dashboard_out: str | None = None
    query_manifest_path: str | None = None


@dataclass
class GeoMonitorResult:
    status: str
    job_id: str | None = None
    run_id: str | None = None
    summary_markdown: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    artifact_paths: dict[str, str] = field(default_factory=dict)
    study_paths: dict[str, str | None] = field(default_factory=dict)
    db_path: str | None = None
    dashboard_path: str | None = None
    quality_flags: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def resolve_study_paths(
    *,
    study_dir: str | Path | None = None,
    runs_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    dashboard_out: str | Path | None = None,
    query_manifest_path: str | Path | None = None,
) -> StudyPaths:
    study = Path(study_dir) if study_dir is not None else None
    runs = Path(runs_dir) if runs_dir is not None else (study / "runs" if study is not None else None)
    database = (
        Path(db_path) if db_path is not None else (study / "geo.duckdb" if study is not None else runs.parent / "geo.duckdb" if runs is not None else None)
    )
    dashboard = (
        Path(dashboard_out)
        if dashboard_out is not None
        else (study / "dashboard" if study is not None else runs.parent / "dashboard" if runs is not None else None)
    )
    return StudyPaths(
        study_dir=str(study) if study is not None else None,
        runs_dir=str(runs) if runs is not None else None,
        db_path=str(database) if database is not None else None,
        dashboard_out=str(dashboard) if dashboard is not None else None,
        query_manifest_path=str(Path(query_manifest_path)) if query_manifest_path is not None else None,
    )


def run_geo_monitor(
    *,
    config_path: str | Path | None = None,
    bundle_dir: str | Path | None = None,
    study_dir: str | Path | None = None,
    runs_dir: str | Path | None = None,
    seed_prompts_path: str | Path | None = None,
    persona_template_registry_path: str | Path | None = None,
    query_manifest_path: str | Path | None = None,
    db_path: str | Path | None = None,
    dashboard_out: str | Path | None = None,
    mock: bool = False,
    dry_run: bool = False,
    include_mock: bool = False,
    confirm_cost: bool = False,
    fanout_force: bool = False,
    limit: int | None = None,
    only_query_id: str | list[str] | None = None,
    resume: bool = True,
    keep_work: bool = False,
    refresh_extraction_cache: bool = False,
    build_db: bool = True,
    build_dashboard: bool = False,
    write_aggregates: bool = True,
    settings: Settings | None = None,
) -> GeoMonitorResult:
    if bundle_dir is None and config_path is None:
        raise ValueError("run_geo_monitor 需要 config_path（新建任务）或 bundle_dir（续跑已有任务）")
    if bundle_dir is not None and config_path is not None:
        raise ValueError("config_path 与 bundle_dir 不能同时传入；续跑必须沿用已有 frozen manifest")
    if bundle_dir is not None and seed_prompts_path is not None:
        raise ValueError("续跑已有 bundle 时不能重新 fanout；请使用 bundle 内冻结的 query manifest")
    if bundle_dir is not None and runs_dir is not None and Path(runs_dir).resolve() != Path(bundle_dir).parent.resolve():
        raise ValueError("续跑已有 bundle 时 runs_dir 必须等于 bundle 的父目录，避免分析与 DuckDB 指向不同 study")
    inferred_runs = runs_dir
    if bundle_dir is not None and study_dir is None and runs_dir is None:
        inferred_runs = Path(bundle_dir).parent
    paths = resolve_study_paths(
        study_dir=study_dir,
        runs_dir=inferred_runs,
        db_path=db_path,
        dashboard_out=dashboard_out,
        query_manifest_path=query_manifest_path,
    )
    if bundle_dir is not None and (not paths.runs_dir or Path(paths.runs_dir).resolve() != Path(bundle_dir).parent.resolve()):
        raise ValueError("续跑已有 bundle 时 study/runs 路径必须指向 bundle 的父目录")
    if not paths.runs_dir:
        raise ValueError("run_geo_monitor 需要 study_dir 或 runs_dir")
    if seed_prompts_path and not paths.query_manifest_path:
        raise ValueError("使用 seed_prompts_path 时必须显式传入 query_manifest_path")
    if build_dashboard and not paths.dashboard_out:
        raise ValueError("build_dashboard=True 需要 study_dir 或 dashboard_out")
    existing_db_available = bool(paths.db_path and Path(paths.db_path).exists())
    if build_dashboard and not build_db and not existing_db_available:
        raise ValueError("build_dashboard=True 且 build_db=False 需要已有 DuckDB；请提供 db_path 或启用 build_db=True")
    fanout_result: dict[str, Any] = {}
    if seed_prompts_path and paths.query_manifest_path:
        manifest_path = Path(paths.query_manifest_path)
        existed = manifest_path.exists()
        if fanout_force or not existed:
            built = build_query_manifest(
                seed_prompts_path,
                manifest_path,
                force=fanout_force,
                persona_template_registry_path=persona_template_registry_path,
            )
            fanout_result = {**built, "action": "overwritten" if existed else "generated"}
        else:
            fanout_result = {"output": str(manifest_path), "action": "reused"}
    if bundle_dir is not None:
        existing_bundle = Path(bundle_dir)
        existing_manifest = load_job_manifest(existing_bundle)
        bundle = {**existing_manifest, "bundle_dir": str(existing_bundle)}
    else:
        assert config_path is not None
        bundle = build_job_bundle(
            config_path,
            runs_dir=paths.runs_dir,
            query_manifest_path=paths.query_manifest_path,
            settings=settings,
        )
    only_query_ids = _normalize_only_query_id(only_query_id)
    if only_query_id is not None and not only_query_ids:
        raise ValueError("only_query_id 已提供但没有有效 ID；拒绝回退为全量执行")
    run_result = run_job_bundle(
        bundle["bundle_dir"],
        mock=mock,
        dry_run=dry_run,
        confirm_cost=confirm_cost,
        limit=limit,
        only_query_ids=only_query_ids,
        query_manifest_path=paths.query_manifest_path if bundle_dir is not None else None,
        resume=resume,
        settings=settings,
    )
    analysis_result: dict[str, Any] = {}
    if not dry_run:
        analysis_result = analyze_job_bundle(
            bundle["bundle_dir"],
            include_mock=include_mock or mock,
            confirm_cost=confirm_cost,
            keep_work=keep_work,
            refresh_extraction_cache=refresh_extraction_cache,
            write_aggregates=write_aggregates,
            settings=settings,
        )
    final_manifest = load_job_manifest(bundle["bundle_dir"])
    status = str(final_manifest.get("status") or ("dry_run" if dry_run else "ran"))
    db_result = None
    dashboard_result = None
    db_available = existing_db_available
    if build_db and not dry_run and status.startswith("analyzed") and paths.db_path:
        db_result = build_duckdb(paths.runs_dir, paths.db_path)
        db_available = True
    if build_dashboard and db_available and paths.dashboard_out and paths.db_path:
        dashboard_result = build_static_dashboard(paths.db_path, paths.dashboard_out)
    report_md = Path(bundle["bundle_dir"]) / "result" / "report.md"
    artifact_paths = {
        "bundle_dir": str(bundle["bundle_dir"]),
        "raw_attempts": str(Path(bundle["bundle_dir"]) / "raw" / "attempts.jsonl"),
        "report_markdown": str(report_md),
    }
    bundle_dir = Path(bundle["bundle_dir"])
    artifact_paths.update(
        {
            key: str(Path(value) if Path(value).is_absolute() else bundle_dir / str(value))
            for key, value in (analysis_result.get("analysis_files") or {}).items()
        }
    )
    return GeoMonitorResult(
        status=status,
        job_id=str(bundle.get("job_id") or ""),
        run_id=str(run_result.get("run_id") or bundle.get("job_id") or ""),
        summary_markdown=report_md.read_text(encoding="utf-8") if report_md.exists() else "",
        metrics={"fanout": fanout_result, "run": run_result, "analysis": analysis_result, "db": db_result or {}},
        artifact_paths=artifact_paths,
        study_paths=asdict(paths),
        db_path=paths.db_path if db_available else None,
        dashboard_path=dashboard_result["dashboard_path"] if dashboard_result else None,
        quality_flags=_quality_flags_from_analysis(analysis_result),
        errors=_errors_from_results(run_result, analysis_result),
    )


def _normalize_only_query_id(value: str | list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _quality_flags_from_analysis(analysis_result: dict[str, Any]) -> list[dict[str, Any]]:
    data_quality = analysis_result.get("data_quality")
    if not isinstance(data_quality, dict):
        return []
    flags: list[dict[str, Any]] = []
    if data_quality.get("partial_sample"):
        flags.append(
            {
                "type": "partial_sample",
                "message": "样本或抽取质量不足，结论强度已降级",
                "conclusion_strength": data_quality.get("conclusion_strength", "observational"),
            }
        )
    for key in ["missing_units", "extra_units", "duplicate_units", "contract_mismatches", "raw_read_errors", "invalid_records"]:
        values = data_quality.get(key)
        if isinstance(values, list) and values:
            flags.append({"type": key, "count": len(values)})
    if data_quality.get("traceability_quarantine_count"):
        flags.append({"type": "traceability_quarantine", "count": data_quality["traceability_quarantine_count"]})
    return flags


def _errors_from_results(run_result: dict[str, Any], analysis_result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(run_result.get("errors") or 0):
        errors.append(f"run errors: {run_result['errors']}")
    if int(analysis_result.get("extraction_error_count") or 0):
        errors.append(f"extraction errors: {analysis_result['extraction_error_count']}")
    return errors
