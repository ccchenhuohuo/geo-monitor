"""Validate committed analysis artifacts without depending on optional stores."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..jobs.contracts import RAW_ATTEMPTS
from ..jobs.manifest import load_job_manifest

ANALYSIS_ARTIFACT_SCHEMA_VERSION = "analysis-artifacts-v1"


def validate_analysis_artifact_manifest(
    run_dir: Path,
    manifest: dict[str, Any],
    summary: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Validate an analysis commit marker and all paths and hashes it commits."""

    marker_path = run_dir / "logs" / "analysis_artifacts.json"
    if not marker_path.exists():
        return False, []
    if marker_path.is_symlink() or not marker_path.is_file():
        return True, ["commit marker must be a regular non-symlink file"]
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return True, [f"commit marker is unreadable: {exc}"]
    if not isinstance(marker, dict):
        return True, ["commit marker must contain a JSON object"]

    reasons: list[str] = []
    if marker.get("schema_version") != ANALYSIS_ARTIFACT_SCHEMA_VERSION:
        reasons.append("unsupported schema_version")
    job_id = str(manifest.get("job_id") or "")
    if str(marker.get("job_id") or "") != job_id:
        reasons.append("job_id does not match job manifest")
    marker_generation = _to_int(marker.get("run_generation"))
    manifest_generation = _to_int(manifest.get("run_generation")) or 0
    if marker_generation != manifest_generation:
        reasons.append("run_generation does not match job manifest")
    summary_intelligence_schema = summary.get("intelligence_schema_version")
    if summary_intelligence_schema not in (None, "") and marker.get("intelligence_schema_version") not in (None, summary_intelligence_schema):
        reasons.append("intelligence_schema_version does not match analysis summary")

    inputs = marker.get("inputs")
    if not isinstance(inputs, dict):
        reasons.append("inputs must be an object")
        inputs = {}
    if "raw_attempts" not in inputs:
        reasons.append("raw_attempts input is not committed")
    expected_input_paths = {
        "raw_attempts": (run_dir / RAW_ATTEMPTS).resolve(),
        "query_manifest": (run_dir / "work" / "query_manifest.csv").resolve(),
    }
    for name, entry in inputs.items():
        if not isinstance(entry, dict):
            reasons.append(f"input {name!r} metadata must be an object")
            continue
        resolved, path_reason = _resolve_committed_path(run_dir, entry.get("path"))
        if path_reason:
            reasons.append(f"input {name!r}: {path_reason}")
            continue
        if name in expected_input_paths and resolved != expected_input_paths[name]:
            reasons.append(f"input {name!r} path is not the expected bundle path")
            continue
        expected_hash = _normalized_sha256(entry.get("sha256"))
        if expected_hash is None:
            reasons.append(f"input {name!r} has an invalid sha256")
            continue
        validation_mode = str(entry.get("validation_mode") or "")
        expected_size = _to_int(entry.get("size_bytes"))
        if validation_mode not in {"exact", "append_only_prefix"}:
            reasons.append(f"input {name!r} has an unsupported validation_mode")
            continue
        if expected_size is None or expected_size < 0:
            reasons.append(f"input {name!r} has an invalid size_bytes")
            continue
        if resolved.exists():
            if resolved.is_symlink() or not resolved.is_file():
                reasons.append(f"input {name!r} must be a regular non-symlink file")
            elif validation_mode == "exact":
                if resolved.stat().st_size != expected_size:
                    reasons.append(f"input {name!r} size_bytes mismatch")
                elif file_sha256(resolved) != expected_hash:
                    reasons.append(f"input {name!r} sha256 mismatch")
            elif resolved.stat().st_size < expected_size:
                reasons.append(f"append-only input {name!r} is shorter than its committed prefix")
            elif _file_prefix_sha256(resolved, expected_size) != expected_hash:
                reasons.append(f"append-only input {name!r} committed-prefix sha256 mismatch")
            elif resolved.stat().st_size > expected_size:
                suffix_reason = _validate_diagnostic_jsonl_suffix(resolved, expected_size, manifest_generation)
                if suffix_reason:
                    reasons.append(f"append-only input {name!r} suffix is invalid: {suffix_reason}")
        elif bool(entry.get("required_after_cleanup", True)):
            reasons.append(f"required input {name!r} is missing")
        elif name == "query_manifest":
            query_manifest_info = manifest.get("query_manifest")
            declared_manifest_hash = query_manifest_info.get("sha256") if isinstance(query_manifest_info, dict) else None
            manifest_query_hash = _normalized_sha256(declared_manifest_hash)
            if manifest_query_hash is not None and manifest_query_hash != expected_hash:
                reasons.append("cleaned query_manifest hash does not match job manifest")

    artifacts = marker.get("artifacts")
    if not isinstance(artifacts, dict):
        reasons.append("artifacts must be an object")
        artifacts = {}
    if "analysis_summary" not in artifacts:
        reasons.append("analysis_summary artifact is not committed")
    committed_paths: dict[Path, str] = {}
    for name, entry in artifacts.items():
        if not isinstance(entry, dict):
            reasons.append(f"artifact {name!r} metadata must be an object")
            continue
        resolved, path_reason = _resolve_committed_path(run_dir, entry.get("path"))
        if path_reason:
            reasons.append(f"artifact {name!r}: {path_reason}")
            continue
        previous = committed_paths.get(resolved)
        if previous is not None:
            reasons.append(f"artifacts {previous!r} and {name!r} resolve to the same file")
            continue
        committed_paths[resolved] = str(name)
        if resolved.is_symlink() or not resolved.is_file():
            reasons.append(f"artifact {name!r} is missing or is not a regular file")
            continue
        expected_hash = _normalized_sha256(entry.get("sha256"))
        if expected_hash is None:
            reasons.append(f"artifact {name!r} has an invalid sha256")
        elif file_sha256(resolved) != expected_hash:
            reasons.append(f"artifact {name!r} sha256 mismatch")
        expected_size = _to_int(entry.get("size_bytes"))
        if expected_size is None or expected_size != resolved.stat().st_size:
            reasons.append(f"artifact {name!r} size_bytes mismatch")

    summary_path = (run_dir / "logs" / "analysis_summary.json").resolve()
    if committed_paths.get(summary_path) != "analysis_summary":
        reasons.append("analysis_summary artifact path is incorrect")
    result_dir = run_dir / "result"
    if result_dir.exists():
        for path in result_dir.iterdir():
            if path.is_file() and path.suffix.lower() in {".csv", ".md", ".html", ".pdf"}:
                if path.resolve() not in committed_paths:
                    reasons.append(f"uncommitted analysis artifact exists: result/{path.name}")
    return True, list(dict.fromkeys(reasons))


def validate_analysis_commit(
    run_dir: str | Path,
    manifest: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Validate the analysis commit marker for trusted cross-run consumers."""

    root = Path(run_dir)
    manifest = manifest or load_job_manifest(root)
    if summary is None:
        summary_path = root / "logs" / "analysis_summary.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return False, [f"analysis summary is unreadable: {exc}"]
    present, reasons = validate_analysis_artifact_manifest(root, manifest, summary)
    if not present:
        return False, ["analysis commit marker is missing"]
    return not reasons, reasons


def _resolve_committed_path(run_dir: Path, value: Any) -> tuple[Path, str]:
    if value in (None, ""):
        return run_dir.resolve(), "path is missing"
    candidate = Path(str(value))
    if not candidate.is_absolute():
        candidate = run_dir / candidate
    if candidate.is_symlink():
        return candidate, "path must not be a symlink"
    try:
        resolved = candidate.resolve()
        resolved.relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return candidate, "path escapes the job bundle"
    return resolved, ""


def _normalized_sha256(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


def file_sha256(path: Path) -> str:
    """Return the SHA-256 digest used by analysis artifact manifests."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_prefix_sha256(path: Path, size_bytes: int) -> str:
    digest = hashlib.sha256()
    remaining = size_bytes
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest() if remaining == 0 else ""


def _validate_diagnostic_jsonl_suffix(path: Path, offset: int, run_generation: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            suffix = handle.read().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return f"unreadable suffix: {exc}"
    for line_number, line in enumerate(suffix.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            return f"line {line_number} is not JSON: {exc}"
        if not isinstance(record, dict):
            return f"line {line_number} is not an object"
        execution_mode = str(record.get("execution_mode") or "")
        if execution_mode not in {"mock", "dry_run"}:
            return f"line {line_number} is not a diagnostic execution"
        if _to_int(record.get("run_generation")) != run_generation:
            return f"line {line_number} changes run_generation"
        if _to_positive_int(record.get("diagnostic_generation")) is None:
            return f"line {line_number} lacks diagnostic_generation"
        allowed_statuses = {"mock", "error", "interrupted"} if execution_mode == "mock" else {"dry_run", "error", "interrupted"}
        if str(record.get("status") or "") not in allowed_statuses:
            return f"line {line_number} has invalid diagnostic status"
    return ""


def _to_int(value: Any) -> int | None:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed


def _to_positive_int(value: Any) -> int | None:
    parsed = _to_int(value)
    return parsed if parsed is not None and parsed > 0 else None
