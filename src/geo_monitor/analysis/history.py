"""Read-only comparable-run history adapter for trend analysis."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..jobs.layout import runs_root_for_bundle
from .artifact_commit import validate_analysis_commit


def load_intelligence_history(
    bundle_dir: Path,
    manifest: dict[str, Any],
    sample_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load only committed, comparable sibling analyses."""

    runs_root = runs_root_for_bundle(bundle_dir)
    overview_rows: list[dict[str, Any]] = []
    visibility_rows: list[dict[str, Any]] = []
    if not runs_root.exists():
        return overview_rows, visibility_rows
    bundle_resolved = bundle_dir.resolve()
    for sibling in sorted(runs_root.iterdir(), key=lambda path: path.name):
        if not sibling.is_dir() or sibling.is_symlink() or sibling.resolve() == bundle_resolved:
            continue
        summary_path = sibling / "logs" / "analysis_summary.json"
        manifest_path = sibling / "job_manifest.json"
        if not summary_path.is_file() or not manifest_path.is_file():
            continue
        try:
            sibling_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            sibling_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not str(sibling_manifest.get("status") or "").startswith("analyzed"):
            continue
        commit_valid, _ = validate_analysis_commit(sibling, sibling_manifest, sibling_summary)
        if not commit_valid or not _comparable_history_run(manifest, sample_mode, sibling_manifest, sibling_summary):
            continue
        overview_path = sibling / "result" / "geo_overview_scores.csv"
        visibility_path = sibling / "result" / "visibility_summary.csv"
        if overview_path.is_file() and not overview_path.is_symlink():
            overview_rows.extend(_read_csv_rows(overview_path))
        if visibility_path.is_file() and not visibility_path.is_symlink():
            visibility_rows.extend(_read_csv_rows(visibility_path))
    return overview_rows, visibility_rows


def _comparable_history_run(
    manifest: dict[str, Any],
    sample_mode: str,
    sibling_manifest: dict[str, Any],
    sibling_summary: dict[str, Any],
) -> bool:
    if str(sibling_summary.get("sample_mode") or "") != sample_mode:
        return False
    if str(sibling_summary.get("target_brand") or "") != str(manifest.get("target_brand") or ""):
        return False
    if int(sibling_summary.get("run_generation") or 0) != int(sibling_manifest.get("run_generation") or 0):
        return False
    current_profile = manifest.get("comparability_profile") or {}
    sibling_profile = sibling_summary.get("comparability_profile") or sibling_manifest.get("comparability_profile") or {}
    return all(
        str(current_profile.get(key) or "") == str(sibling_profile.get(key) or "")
        for key in ("study_fingerprint", "sampling_fingerprint", "analysis_fingerprint")
    )


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))
