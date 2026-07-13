"""Orchestrate atomic DuckDB builds from committed run bundles."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..analysis.artifact_commit import validate_analysis_artifact_manifest as _validate_analysis_artifact_manifest
from ..filesystem import UnsafeOutputPathError, ensure_private_directory, prepare_private_output, secure_private_file
from ..jobs.contracts import JOB_MANIFEST, RAW_ATTEMPTS
from ..jobs.manifest import load_job_manifest
from ..jobs.query_manifest import load_job_queries
from ..query_meta import query_metadata_json, tags_text
from .attempts import _read_attempts
from .contracts import (
    CORE_RESULT_CSV_STEMS,
    DUCKDB_SCHEMA_VERSION,
    DuckDBError,
    _compare_optional_int,
    _parse_timestamp,
    _quality,
    _to_int,
)
from .query import _duckdb
from .results import _has_result_csvs, _ingest_csv_outputs, _read_csv, _validate_analysis_artifacts
from .schema import _create_schema, _create_views


def build_duckdb(runs_dir: str | Path, output_path: str | Path, *, query_manifest: str | Path | None = None) -> dict[str, Any]:
    duckdb = _duckdb()
    runs_root = Path(runs_dir)
    if runs_root.is_symlink():
        raise DuckDBError(f"runs_dir 不能是 symlink：{runs_root}")
    output = Path(output_path)
    try:
        ensure_private_directory(output.parent)
        prepare_private_output(output)
    except UnsafeOutputPathError as exc:
        raise DuckDBError(str(exc)) from exc
    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    if tmp.is_symlink():
        raise DuckDBError(f"临时 DuckDB 路径不能是 symlink：{tmp}")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(str(tmp))
    try:
        _create_schema(con)
        fallback_rows = _load_fallback_manifest(query_manifest)
        counts = {"runs": 0, "queries": 0, "attempts": 0, "quality_flags": 0}
        for run_dir in _iter_run_dirs(runs_root):
            run_counts = _ingest_run(con, run_dir, fallback_rows)
            for key, value in run_counts.items():
                counts[key] = counts.get(key, 0) + value
        _create_views(con)
        con.close()
        secure_private_file(tmp)
        os.replace(tmp, output)
        secure_private_file(output)
    except Exception:
        con.close()
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return {"db_path": str(output), **counts, "schema_version": DUCKDB_SCHEMA_VERSION}


def _iter_run_dirs(runs_root: Path) -> list[Path]:
    if not runs_root.exists():
        return []
    candidates: list[Path] = []
    for path in sorted(item for item in runs_root.iterdir() if item.is_dir() and not item.is_symlink()):
        if path.name.startswith("."):
            continue
        if (path / JOB_MANIFEST).exists() or (path / RAW_ATTEMPTS).exists():
            candidates.append(path)
    return candidates


def _ingest_run(con: Any, run_dir: Path, fallback_rows: dict[str, dict[str, str]]) -> dict[str, int]:
    counts = {"runs": 0, "queries": 0, "attempts": 0, "quality_flags": 0}
    manifest_path = run_dir / JOB_MANIFEST
    if not manifest_path.exists():
        _quality(con, run_dir.name, "missing_job_manifest", "job_manifest.json missing", str(manifest_path))
        counts["quality_flags"] += 1
        return counts
    try:
        manifest = load_job_manifest(run_dir)
    except Exception as exc:
        _quality(con, run_dir.name, "bad_job_manifest", str(exc), str(manifest_path))
        counts["quality_flags"] += 1
        return counts
    job_id = str(manifest.get("job_id") or run_dir.name)
    raw_path = run_dir / RAW_ATTEMPTS
    attempts, query_rows, flags = _read_attempts(raw_path, job_id, fallback_rows)
    for flag in flags:
        _quality(con, job_id, **flag)
    counts["quality_flags"] += len(flags)
    if not raw_path.exists():
        _quality(con, job_id, "missing_raw_attempts", "raw/attempts.jsonl missing", str(raw_path))
        counts["quality_flags"] += 1
    info = manifest.get("query_manifest") if isinstance(manifest.get("query_manifest"), dict) else {}
    sampling_profile = manifest.get("sampling_profile") if isinstance(manifest.get("sampling_profile"), dict) else {}
    analysis_profile = manifest.get("analysis_profile") if isinstance(manifest.get("analysis_profile"), dict) else {}
    comparability_profile = manifest.get("comparability_profile") if isinstance(manifest.get("comparability_profile"), dict) else {}
    planned_query_rows = _planned_query_rows(run_dir, manifest)
    for qid, row in query_rows.items():
        if qid in planned_query_rows:
            planned_query_rows[qid].update({key: value for key, value in row.items() if value not in (None, "")})
        else:
            planned_query_rows[qid] = row
    analysis_summary = _read_analysis_summary(run_dir)
    artifact_manifest_present, artifact_manifest_reasons = _validate_analysis_artifact_manifest(run_dir, manifest, analysis_summary)
    artifact_manifest_required = str(manifest.get("schema_version") or "") == "geo-job-v3" and str(manifest.get("status") or "").startswith("analyzed")
    if artifact_manifest_required and not artifact_manifest_present:
        artifact_manifest_reasons = ["commit marker is required for analyzed geo-job-v3 bundles"]
    if artifact_manifest_reasons:
        summary_current = False
        summary_reasons = [f"analysis_artifacts.json: {reason}" for reason in artifact_manifest_reasons]
    else:
        summary_current, summary_reasons = _analysis_summary_is_current(
            manifest,
            analysis_summary,
            attempts=attempts,
            planned_query_rows=planned_query_rows,
        )
    artifact_reasons: list[str] = []
    if summary_current and _has_result_csvs(run_dir, analysis_summary):
        artifact_reasons = _validate_analysis_artifacts(
            run_dir,
            job_id,
            manifest,
            analysis_summary,
            attempts,
            planned_query_rows,
        )
    analysis_artifacts_current = summary_current and not artifact_reasons
    if not summary_current and str(manifest.get("status") or "").startswith("analyzed"):
        _quality(
            con,
            job_id,
            "invalid_analysis_artifact_manifest" if artifact_manifest_present or artifact_manifest_required else "stale_or_missing_analysis_summary",
            "; ".join(summary_reasons) or "analysis_summary.json is missing or stale",
            str(run_dir / "logs" / "analysis_summary.json"),
        )
        counts["quality_flags"] += 1
        if artifact_manifest_present:
            for stem in CORE_RESULT_CSV_STEMS:
                missing_path = run_dir / "result" / f"{stem}.csv"
                if not missing_path.exists():
                    _quality(con, job_id, "missing_result_csv", f"result CSV missing: {stem}.csv", str(missing_path))
                    counts["quality_flags"] += 1
    elif artifact_reasons:
        _quality(
            con,
            job_id,
            "stale_analysis_artifacts_ignored",
            "; ".join(artifact_reasons),
            str(run_dir / "result"),
        )
        counts["quality_flags"] += 1
    pdf_report = run_dir / "result" / "report.pdf"
    markdown_report = run_dir / "result" / "report.md"
    report_path = pdf_report if pdf_report.exists() else markdown_report
    completed_at = _completed_at(run_dir, attempts)
    inferred_from_legacy = bool(
        sampling_profile.get("inferred_from_legacy") or analysis_profile.get("inferred_from_legacy") or comparability_profile.get("inferred_from_legacy")
    )
    data_quality = analysis_summary.get("data_quality") if summary_current and isinstance(analysis_summary.get("data_quality"), dict) else {}
    con.execute(
        """
        insert into runs(
            job_id, status, created_at, completed_at, created_at_ts, completed_at_ts,
            target_brand, model, provider, adapter, adapter_version, api_family, repeats,
            source_grain, analysis_model, analysis_adapter, analysis_fingerprint,
            study_fingerprint, sampling_fingerprint, sample_mode, partial_sample,
            success_record_count, stats_record_count, run_generation, inferred_from_legacy,
            job_conclusion_strength, sample_count, query_manifest_sha256,
            query_manifest_source_type, query_manifest_source_uri, report_path
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            job_id,
            manifest.get("status"),
            manifest.get("created_at"),
            completed_at,
            _parse_timestamp(manifest.get("created_at")),
            _parse_timestamp(completed_at),
            manifest.get("target_brand"),
            manifest.get("model"),
            sampling_profile.get("provider", ""),
            sampling_profile.get("adapter", manifest.get("adapter", "")),
            sampling_profile.get("adapter_version", ""),
            sampling_profile.get("api_family", ""),
            _to_int(manifest.get("repeats")),
            sampling_profile.get("source_grain", ""),
            analysis_profile.get("model", ""),
            analysis_profile.get("adapter", ""),
            analysis_profile.get("analysis_fingerprint", ""),
            comparability_profile.get("study_fingerprint", ""),
            comparability_profile.get("sampling_fingerprint", ""),
            analysis_summary.get("sample_mode", "") if summary_current else "",
            bool(analysis_summary.get("partial_sample") or data_quality.get("partial_sample")) if summary_current else None,
            _to_int(analysis_summary.get("success_record_count")) if summary_current else None,
            _to_int(analysis_summary.get("stats_record_count")) if summary_current else None,
            _to_int(manifest.get("run_generation")),
            inferred_from_legacy,
            (analysis_summary.get("job_conclusion_strength") or data_quality.get("conclusion_strength", "")) if summary_current else "",
            len(attempts),
            comparability_profile.get("query_manifest_sha256") or info.get("sha256", ""),
            info.get("source_type", ""),
            info.get("source_uri", ""),
            str(report_path) if analysis_artifacts_current and report_path.exists() else "",
        ],
    )
    counts["runs"] += 1
    seen_attempts: set[str] = set()
    for attempt in attempts:
        if attempt["attempt_id"] in seen_attempts:
            _quality(con, job_id, "duplicate_attempt_id", "duplicate attempt_id retained", attempt["raw_path"], attempt["raw_line_number"], attempt["query_id"])
            counts["quality_flags"] += 1
        else:
            seen_attempts.add(attempt["attempt_id"])
        con.execute(
            """
            insert into attempts(
                job_id, attempt_id, query_id, repeat_index, run_generation, diagnostic_generation,
                execution_mode, status, latency_ms,
                error, model, provider, adapter, adapter_version, api_family,
                request_fingerprint_version, web_search_performed, web_search_evidence,
                web_search_requirement_status, source_parse_status, created_at, completed_at,
                created_at_ts, completed_at_ts, request_hash, response_preview, response_length,
                raw_path, raw_line_number
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                job_id,
                attempt["attempt_id"],
                attempt["query_id"],
                attempt["repeat_index"],
                attempt["run_generation"],
                attempt["diagnostic_generation"],
                attempt["execution_mode"],
                attempt["status"],
                attempt["latency_ms"],
                attempt["error"],
                attempt["model"],
                attempt["provider"],
                attempt["adapter"],
                attempt["adapter_version"],
                attempt["api_family"],
                attempt["request_fingerprint_version"],
                attempt["web_search_performed"],
                attempt["web_search_evidence"],
                attempt["web_search_requirement_status"],
                attempt["source_parse_status"],
                attempt["created_at"],
                attempt["completed_at"],
                attempt["created_at_ts"],
                attempt["completed_at_ts"],
                attempt["request_hash"],
                attempt["response_preview"],
                attempt["response_length"],
                attempt["raw_path"],
                attempt["raw_line_number"],
            ],
        )
        counts["attempts"] += 1
    for row in planned_query_rows.values():
        con.execute(
            "insert into queries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row["query_id"],
                row["variant_id"],
                row["seed_id"],
                row["seed_query"],
                row["category"],
                row["intent"],
                row["persona"],
                row["template_id"],
                row["query"],
                row["locale"],
                row["market"],
                row["tags"],
                row["language"],
                row["generation_method"],
                row["fanout_version"],
                row["manifest_version"],
                row["locked_at"],
                row["query_metadata_json"],
            ],
        )
        counts["queries"] += 1
    if analysis_artifacts_current:
        counts["quality_flags"] += _ingest_csv_outputs(
            con,
            run_dir,
            job_id,
            run_generation=_to_int(analysis_summary.get("run_generation")) or 0,
            analysis_summary=analysis_summary,
        )
    elif (run_dir / "result").exists() and any((run_dir / "result").iterdir()):
        _quality(con, job_id, "stale_result_csv_ignored", "result CSV ignored because analysis_summary is stale or missing", str(run_dir / "result"))
        counts["quality_flags"] += 1
    return counts


def _load_fallback_manifest(path: str | Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    rows: dict[str, dict[str, str]] = {}
    for row in _read_csv(Path(path)):
        qid = str(row.get("query_id") or "")
        if qid:
            rows[qid] = {key: str(value or "") for key, value in row.items()}
    return rows


def _completed_at(run_dir: Path, attempts: list[dict[str, Any]]) -> str:
    summary = run_dir / "logs" / "run_summary.json"
    candidates: list[str] = []
    if summary.exists():
        try:
            candidates.append(str(json.loads(summary.read_text(encoding="utf-8")).get("completed_at") or ""))
        except Exception:
            pass
    candidates.extend(str(row.get("completed_at") or "") for row in attempts if row.get("completed_at"))
    parsed = [(value, _parse_timestamp(value)) for value in candidates if value]
    valid = [(value, timestamp) for value, timestamp in parsed if timestamp is not None]
    if valid:
        return max(valid, key=lambda item: item[1])[0]
    return candidates[-1] if candidates else ""


def _read_analysis_summary(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "logs" / "analysis_summary.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _analysis_summary_is_current(
    manifest: dict[str, Any],
    summary: dict[str, Any],
    *,
    attempts: list[dict[str, Any]],
    planned_query_rows: dict[str, dict[str, str]],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not str(manifest.get("status") or "").startswith("analyzed"):
        return False, ["job manifest status is not analyzed"]
    if not summary:
        return False, ["analysis_summary.json is missing or unreadable"]
    manifest_generation_raw = manifest.get("run_generation")
    summary_generation_raw = summary.get("run_generation")
    manifest_generation = _to_int(manifest_generation_raw)
    summary_generation = _to_int(summary_generation_raw)
    if manifest_generation_raw not in (None, "") and summary_generation_raw in (None, ""):
        reasons.append("analysis summary has no run_generation")
    elif (manifest_generation or 0) != (summary_generation or 0):
        reasons.append(f"run_generation mismatch: manifest={manifest_generation or 0}, summary={summary_generation or 0}")

    job_id = str(manifest.get("job_id") or "")
    if summary.get("job_id") not in (None, "") and str(summary.get("job_id")) != job_id:
        reasons.append("analysis summary job_id does not match manifest")
    if any(row.get("record_job_id") and row.get("record_job_id") != job_id for row in attempts):
        reasons.append("raw attempts contain a different job_id")
    if manifest_generation is not None and any(row.get("run_generation") is not None and int(row["run_generation"]) > manifest_generation for row in attempts):
        reasons.append("raw attempts contain a generation newer than the manifest")

    expected_queries = _to_int(manifest.get("query_count"))
    expected_repeats = _to_int(manifest.get("repeats"))
    _compare_optional_int(summary, "expected_queries", expected_queries, reasons)
    _compare_optional_int(summary, "expected_repeats", expected_repeats, reasons)
    expected_units = expected_queries * expected_repeats if expected_queries is not None and expected_repeats is not None else None
    _compare_optional_int(summary, "expected_units", expected_units, reasons)

    if summary.get("model") not in (None, "") and str(summary.get("model")) != str(manifest.get("model") or ""):
        reasons.append("analysis summary model does not match manifest")
    expected_query_hash = _manifest_query_hash(manifest)
    if expected_query_hash and summary.get("query_set_hash") not in (None, "") and str(summary.get("query_set_hash")) != expected_query_hash[:16]:
        reasons.append("analysis summary query_set_hash does not match manifest")

    summary_query_ids = summary.get("query_ids")
    planned_ids = set(planned_query_rows)
    if isinstance(summary_query_ids, list):
        normalized_summary_ids = {str(value) for value in summary_query_ids if str(value)}
        if len(normalized_summary_ids) != len(summary_query_ids):
            reasons.append("analysis summary query_ids are empty or duplicated")
        if expected_queries is not None and len(normalized_summary_ids) != expected_queries:
            reasons.append("analysis summary query_ids count does not match manifest query_count")
        if planned_ids and (expected_queries is None or len(planned_ids) == expected_queries) and normalized_summary_ids != planned_ids:
            reasons.append("analysis summary query_ids do not match the planned query universe")

    for profile_name, fingerprint_fields in {
        "analysis_profile": ("analysis_fingerprint",),
        "comparability_profile": ("query_manifest_sha256", "study_fingerprint", "sampling_fingerprint"),
        "sampling_profile": ("provider", "adapter", "adapter_version", "api_family"),
    }.items():
        expected_profile = manifest.get(profile_name)
        actual_profile = summary.get(profile_name)
        if not isinstance(expected_profile, dict) or not isinstance(actual_profile, dict):
            continue
        for field in fingerprint_fields:
            expected = expected_profile.get(field)
            actual = actual_profile.get(field)
            if expected not in (None, "") and actual not in (None, "") and str(expected) != str(actual):
                reasons.append(f"analysis summary {profile_name}.{field} does not match manifest")
    return not reasons, list(dict.fromkeys(reasons))


def _manifest_query_hash(manifest: dict[str, Any]) -> str:
    comparability = manifest.get("comparability_profile")
    if isinstance(comparability, dict) and comparability.get("query_manifest_sha256"):
        return str(comparability["query_manifest_sha256"])
    query_manifest = manifest.get("query_manifest")
    if isinstance(query_manifest, dict) and query_manifest.get("sha256"):
        return str(query_manifest["sha256"])
    return ""


def _planned_query_rows(run_dir: Path, manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    try:
        records = load_job_queries(run_dir, manifest, materialize=False)
    except Exception:
        records = []
    for record in records:
        meta = dict(record.metadata)
        if record.locale:
            meta["locale"] = record.locale
        if record.market:
            meta["market"] = record.market
        if record.category:
            meta["category"] = record.category
        if record.tags:
            meta["tags"] = ",".join(record.tags)
        row: dict[str, str] = {
            "query_id": str(record.query_id),
            "variant_id": str(meta.get("variant_id") or ""),
            "seed_id": str(meta.get("seed_id") or ""),
            "seed_query": str(meta.get("seed_query") or ""),
            "category": str(meta.get("category") or ""),
            "intent": str(meta.get("intent") or ""),
            "persona": str(meta.get("persona") or ""),
            "template_id": str(meta.get("template_id") or ""),
            "query": str(record.query),
            "locale": str(meta.get("locale") or ""),
            "market": str(meta.get("market") or ""),
            "tags": tags_text(meta.get("tags")),
            "language": str(meta.get("language") or ""),
            "generation_method": str(meta.get("generation_method") or ""),
            "fanout_version": str(meta.get("fanout_version") or ""),
            "manifest_version": str(meta.get("manifest_version") or ""),
            "locked_at": str(meta.get("locked_at") or ""),
            "query_metadata_json": query_metadata_json(meta),
        }
        rows[str(record.query_id)] = row
    return rows
