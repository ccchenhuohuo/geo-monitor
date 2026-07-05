from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .dashboard import build_dashboard as build_static_dashboard
from .db import build_duckdb
from .fanout import build_query_manifest
from .job import build_job_bundle, run_job_bundle
from .job_analysis import analyze_job_bundle


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
    return StudyPaths(
        study_dir=str(study) if study is not None else None,
        runs_dir=str(Path(runs_dir) if runs_dir is not None else (study / "runs" if study is not None else "")) or None,
        db_path=str(Path(db_path) if db_path is not None else (study / "geo.duckdb" if study is not None else "")) or None,
        dashboard_out=str(Path(dashboard_out) if dashboard_out is not None else (study / "dashboard" if study is not None else "")) or None,
        query_manifest_path=str(Path(query_manifest_path)) if query_manifest_path is not None else None,
    )


def run_geo_monitor(
    *,
    config_path: str | Path,
    study_dir: str | Path | None = None,
    runs_dir: str | Path | None = None,
    seed_prompts_path: str | Path | None = None,
    query_manifest_path: str | Path | None = None,
    db_path: str | Path | None = None,
    dashboard_out: str | Path | None = None,
    mock: bool = False,
    dry_run: bool = False,
    include_mock: bool = False,
    confirm_cost: bool = False,
    fanout_force: bool = False,
    build_db: bool = True,
    build_dashboard: bool = False,
) -> GeoMonitorResult:
    paths = resolve_study_paths(
        study_dir=study_dir,
        runs_dir=runs_dir,
        db_path=db_path,
        dashboard_out=dashboard_out,
        query_manifest_path=query_manifest_path,
    )
    if not paths.runs_dir:
        raise ValueError("run_geo_monitor 需要 study_dir 或 runs_dir")
    if seed_prompts_path and not paths.query_manifest_path:
        raise ValueError("使用 seed_prompts_path 时必须显式传入 query_manifest_path")
    if seed_prompts_path and paths.query_manifest_path and not Path(paths.query_manifest_path).exists():
        build_query_manifest(seed_prompts_path, paths.query_manifest_path, force=fanout_force)
    bundle = build_job_bundle(config_path, runs_dir=paths.runs_dir, query_manifest_path=paths.query_manifest_path)
    run_result = run_job_bundle(bundle["bundle_dir"], mock=mock, dry_run=dry_run, confirm_cost=confirm_cost)
    analysis_result: dict[str, Any] = {}
    status = "ran"
    if not dry_run:
        analysis_result = analyze_job_bundle(bundle["bundle_dir"], include_mock=include_mock or mock, confirm_cost=confirm_cost)
        status = "analyzed"
    db_result = None
    dashboard_result = None
    if build_db and status in {"analyzed"} and paths.db_path:
        db_result = build_duckdb(paths.runs_dir, paths.db_path)
    if build_dashboard and db_result and paths.dashboard_out and paths.db_path:
        dashboard_result = build_static_dashboard(paths.db_path, paths.dashboard_out)
    report_md = Path(bundle["bundle_dir"]) / "result" / "report.md"
    artifact_paths = {
        "bundle_dir": str(bundle["bundle_dir"]),
        "raw_attempts": str(Path(bundle["bundle_dir"]) / "raw" / "attempts.jsonl"),
        "report_markdown": str(report_md),
    }
    return GeoMonitorResult(
        status=status,
        job_id=str(bundle.get("job_id") or ""),
        run_id=str(run_result.get("run_id") or bundle.get("job_id") or ""),
        summary_markdown=report_md.read_text(encoding="utf-8") if report_md.exists() else "",
        metrics={"run": run_result, "analysis": analysis_result, "db": db_result or {}},
        artifact_paths=artifact_paths,
        study_paths=asdict(paths),
        db_path=paths.db_path if db_result else None,
        dashboard_path=dashboard_result["dashboard_path"] if dashboard_result else None,
        quality_flags=[],
        errors=[],
    )
