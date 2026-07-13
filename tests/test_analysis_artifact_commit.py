from __future__ import annotations

import hashlib
import json
from pathlib import Path

from geo_monitor.analysis import history
from geo_monitor.analysis.artifact_commit import ANALYSIS_ARTIFACT_SCHEMA_VERSION, validate_analysis_commit


def _commit_entry(root: Path, path: Path, *, validation_mode: str | None = None) -> dict[str, object]:
    entry: dict[str, object] = {
        "path": str(path.relative_to(root)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }
    if validation_mode:
        entry["validation_mode"] = validation_mode
    return entry


def _committed_bundle(tmp_path: Path) -> tuple[Path, dict[str, object], dict[str, object]]:
    root = tmp_path / "run"
    raw = root / "raw" / "attempts.jsonl"
    summary_path = root / "logs" / "analysis_summary.json"
    marker_path = root / "logs" / "analysis_artifacts.json"
    raw.parent.mkdir(parents=True)
    summary_path.parent.mkdir(parents=True)
    raw.write_text("", encoding="utf-8")
    manifest: dict[str, object] = {"job_id": "job-1", "run_generation": 1}
    summary: dict[str, object] = {"job_id": "job-1", "run_generation": 1}
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    marker = {
        "schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
        "job_id": "job-1",
        "run_generation": 1,
        "inputs": {"raw_attempts": _commit_entry(root, raw, validation_mode="exact")},
        "artifacts": {"analysis_summary": _commit_entry(root, summary_path)},
    }
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    return root, manifest, summary


def test_commit_validation_lives_in_core_analysis_module() -> None:
    assert history.validate_analysis_commit is validate_analysis_commit
    assert validate_analysis_commit.__module__ == "geo_monitor.analysis.artifact_commit"


def test_validate_analysis_commit_accepts_intact_bundle_and_rejects_tampering(tmp_path: Path) -> None:
    root, manifest, summary = _committed_bundle(tmp_path)

    assert validate_analysis_commit(root, manifest, summary) == (True, [])

    (root / "logs" / "analysis_summary.json").write_text("{}", encoding="utf-8")
    valid, reasons = validate_analysis_commit(root, manifest, summary)

    assert valid is False
    assert "artifact 'analysis_summary' sha256 mismatch" in reasons
    assert "artifact 'analysis_summary' size_bytes mismatch" in reasons
