"""Committed result and intelligence artifact ingestion."""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any

from ..analysis.artifact_commit import file_sha256 as _file_sha256
from .attempts import _latest_analysis_attempts
from .contracts import (
    CORE_RESULT_CSV_STEMS,
    INTELLIGENCE_CSV_STEMS,
    _compare_optional_int,
    _parse_timestamp,
    _pct,
    _quality,
    _to_bool,
    _to_float,
    _to_int,
    _to_positive_int,
)


def _ingest_csv_outputs(
    con: Any,
    run_dir: Path,
    job_id: str,
    *,
    run_generation: int,
    analysis_summary: dict[str, Any],
) -> int:
    result = run_dir / "result"
    quality_count = 0
    for name in (f"{stem}.csv" for stem in CORE_RESULT_CSV_STEMS):
        path = result / name
        if not path.exists():
            _quality(con, job_id, "missing_result_csv", f"result CSV missing: {name}", str(path))
            quality_count += 1
    for row in _read_csv(result / "brand_summary.csv"):
        con.execute(
            "insert into brand_summary values (?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("brand_name_canonical", ""),
                _pct(row.get("sov_response_share")),
                _pct(row.get("response_mention_rate")),
                _pct(row.get("query_coverage_rate")),
                _to_int(row.get("is_target_brand")),
            ],
        )
    for row in _read_csv(result / "brand_by_query.csv"):
        con.execute(
            "insert into brand_by_query values (?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("query_id", ""),
                row.get("brand_name_canonical", ""),
                _to_int(row.get("responses_mentioned")),
                _pct(row.get("mention_rate_within_query")),
            ],
        )
    for row in _read_csv(result / "query_stability.csv"):
        con.execute(
            "insert into query_stability values (?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("query_id", ""),
                _to_int(row.get("successful_repeats")),
                _to_int(row.get("expected_repeats")),
                _to_float(row.get("brand_set_jaccard_avg")),
            ],
        )
    for row in _read_csv(result / "source_domains.csv"):
        con.execute(
            "insert into source_domains values (?, ?, ?, ?)",
            [job_id, row.get("domain", ""), _pct(row.get("response_coverage_rate")), _pct(row.get("query_coverage_rate"))],
        )
    for row in _read_csv(result / "source_urls.csv"):
        con.execute(
            "insert into source_urls values (?, ?, ?, ?, ?)",
            [job_id, row.get("url", ""), row.get("domain", ""), row.get("title", ""), _to_int(row.get("parsed_source_occurrences"))],
        )
    for row in _read_csv(result / "quality_summary.csv"):
        con.execute(
            "insert into quality_summary values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("sample_mode", ""),
                row.get("conclusion_strength", ""),
                _to_bool(row.get("partial_sample")),
                _to_int(row.get("planned_units")),
                _to_int(row.get("analysis_record_count")),
                _to_int(row.get("stats_record_count")),
                _to_int(row.get("missing_unit_count")),
                _to_int(row.get("latest_failed_unit_count")),
                _to_int(row.get("web_search_quality_flag_count")),
                _to_int(row.get("source_quality_flag_count")),
                _to_int(row.get("extraction_error_record_count")),
                _pct(row.get("extraction_error_rate")),
            ],
        )
    for row in _read_csv(result / "attempt_facts.csv"):
        con.execute(
            "insert into attempt_facts values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                run_generation,
                row.get("query_id", ""),
                _to_int(row.get("repeat_index")),
                row.get("latest_status", ""),
                row.get("completed_at", ""),
                _parse_timestamp(row.get("completed_at")),
                _to_int(row.get("valid_attempt")),
                _to_int(row.get("stats_included")),
                row.get("web_search_requirement_status", ""),
                row.get("web_search_evidence", ""),
                row.get("source_parse_status", ""),
                row.get("request_hash", ""),
                row.get("attempt_id", ""),
            ],
        )
    for row in _read_csv(result / "query_facts.csv"):
        con.execute(
            "insert into query_facts values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("query_id", ""),
                row.get("query", ""),
                _to_int(row.get("planned_attempts")),
                _to_int(row.get("latest_terminal_attempts")),
                _to_int(row.get("completed_attempts")),
                _to_int(row.get("valid_attempts")),
                _to_int(row.get("stats_included_attempts")),
                _to_int(row.get("latest_failed_attempts")),
                _pct(row.get("sample_completeness")),
                _pct(row.get("usable_sample_rate")),
                row.get("query_metadata_json", ""),
            ],
        )
    for row in _read_csv(result / "brand_attempt_facts.csv"):
        con.execute(
            "insert into brand_attempt_facts values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("query_id", ""),
                _to_int(row.get("repeat_index")),
                row.get("brand_name_canonical", ""),
                row.get("brand_name_raw", ""),
                _to_int(row.get("is_target_brand")),
                _to_bool(row.get("sov_eligible")),
                _to_int(row.get("is_recommended")),
                _to_int(row.get("rank_position")),
                row.get("sentiment", ""),
                _to_float(row.get("confidence")),
                row.get("evidence", ""),
                _to_int(row.get("stats_included")),
            ],
        )
    quality_count += _ingest_intelligence_outputs(
        con,
        run_dir,
        job_id,
        run_generation=run_generation,
        analysis_summary=analysis_summary,
    )
    return quality_count


def _has_result_csvs(run_dir: Path, summary: dict[str, Any]) -> bool:
    result = run_dir / "result"
    if result.exists() and any(path.is_file() and path.suffix.lower() == ".csv" for path in result.iterdir()):
        return True
    for field in ("analysis_files", "intelligence_files"):
        mapping = summary.get(field)
        if isinstance(mapping, dict) and any(stem in mapping for stem in INTELLIGENCE_CSV_STEMS):
            return True
    return False


def _intelligence_artifact_paths(run_dir: Path, summary: dict[str, Any]) -> tuple[dict[str, Path], list[str]]:
    result_root = (run_dir / "result").resolve()
    declared: dict[str, Any] = {}
    for field in ("analysis_files", "intelligence_files"):
        mapping = summary.get(field)
        if isinstance(mapping, dict):
            declared.update({str(key): value for key, value in mapping.items() if str(key) in INTELLIGENCE_CSV_STEMS})
    artifacts = summary.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            stem = str(item.get("stem") or item.get("artifact_stem") or "")
            if stem in INTELLIGENCE_CSV_STEMS and item.get("path"):
                declared[stem] = item["path"]

    paths: dict[str, Path] = {}
    reasons: list[str] = []
    resolved_sources: dict[Path, str] = {}
    for stem in INTELLIGENCE_CSV_STEMS:
        declared_value = declared.get(stem)
        fallback = run_dir / "result" / f"{stem}.csv"
        if isinstance(declared_value, dict):
            declared_value = declared_value.get("path") or declared_value.get("csv")
        candidate = Path(str(declared_value)) if declared_value not in (None, "") else fallback
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        candidate_was_symlink = candidate.is_symlink()
        try:
            resolved = candidate.resolve()
            resolved.relative_to(result_root)
        except (OSError, ValueError):
            if declared_value not in (None, "") or candidate.exists() or candidate_was_symlink:
                reasons.append(f"declared intelligence artifact {stem!r} escapes result directory")
            continue
        if candidate_was_symlink:
            reasons.append(f"intelligence artifact {stem!r} must not be a symlink")
            continue
        if resolved.suffix.lower() != ".csv":
            if declared_value not in (None, ""):
                reasons.append(f"declared intelligence artifact {stem!r} is not a CSV")
            continue
        if not resolved.exists():
            if declared_value not in (None, ""):
                reasons.append(f"declared intelligence artifact {stem!r} is missing")
            continue
        previous_stem = resolved_sources.get(resolved)
        if previous_stem is not None:
            reasons.append(f"intelligence artifacts {previous_stem!r} and {stem!r} resolve to the same file")
            continue
        resolved_sources[resolved] = stem
        paths[stem] = resolved
    return paths, reasons


def _ingest_intelligence_outputs(
    con: Any,
    run_dir: Path,
    job_id: str,
    *,
    run_generation: int,
    analysis_summary: dict[str, Any],
) -> int:
    paths, reasons = _intelligence_artifact_paths(run_dir, analysis_summary)
    for reason in reasons:
        _quality(con, job_id, "invalid_intelligence_artifact", reason, str(run_dir / "result"))
    for stem, path in paths.items():
        headers, rows = _read_csv_document(path)
        column_types = {header: _infer_intelligence_column_type([row.get(header, "") for row in rows]) for header in headers}
        con.execute(
            "insert into intelligence_artifacts values (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                run_generation,
                stem,
                str(path),
                _file_sha256(path),
                len(rows),
                json.dumps(headers, ensure_ascii=False, separators=(",", ":")),
                json.dumps(column_types, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ],
        )
        for row_number, row in enumerate(rows, start=1):
            con.execute(
                "insert into intelligence_rows values (?, ?, ?, ?, ?)",
                [
                    job_id,
                    run_generation,
                    stem,
                    row_number,
                    json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ],
            )
    return len(reasons)


def _infer_intelligence_column_type(values: list[str]) -> str:
    materialized = [str(value).strip() for value in values if str(value).strip()]
    if not materialized:
        return "VARCHAR"
    lowered = {value.lower() for value in materialized}
    if lowered <= {"true", "false", "yes", "no"}:
        return "BOOLEAN"
    if all(re.fullmatch(r"[+-]?\d+", value) for value in materialized):
        return "BIGINT"
    if all(value.endswith("%") and _finite_float(value[:-1]) is not None for value in materialized):
        return "PERCENT"
    if all(_finite_float(value) is not None for value in materialized):
        return "DOUBLE"
    if all(_parse_timestamp(value) is not None for value in materialized):
        return "TIMESTAMPTZ"
    return "VARCHAR"


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _read_csv(path: Path) -> list[dict[str, str]]:
    return _read_csv_document(path)[1]


def _read_csv_document(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [str(item) for item in (reader.fieldnames or []) if item is not None]
        rows = [{str(key): "" if value is None else str(value) for key, value in row.items() if key is not None} for row in reader]
    return headers, rows


def _validate_analysis_artifacts(
    run_dir: Path,
    job_id: str,
    manifest: dict[str, Any],
    summary: dict[str, Any],
    attempts: list[dict[str, Any]],
    planned_query_rows: dict[str, dict[str, str]],
) -> list[str]:
    reasons: list[str] = []
    result = run_dir / "result"
    attempt_path = result / "attempt_facts.csv"
    csv_paths = [result / f"{stem}.csv" for stem in CORE_RESULT_CSV_STEMS]
    intelligence_paths, path_reasons = _intelligence_artifact_paths(run_dir, summary)
    reasons.extend(path_reasons)
    present_paths = [path for path in csv_paths if path.exists()] + list(intelligence_paths.values())
    if present_paths and not attempt_path.exists():
        reasons.append("analysis CSVs exist but attempt_facts.csv is missing; artifacts are not generation-verifiable")
        return reasons

    _, fact_rows = _read_csv_document(attempt_path)
    expected_attempts = _latest_analysis_attempts(attempts, sample_mode=str(summary.get("sample_mode") or ""))
    expected_by_key = {(str(row.get("query_id") or ""), int(row.get("repeat_index") or 1)): row for row in expected_attempts}
    actual_by_key: dict[tuple[str, int], dict[str, str]] = {}
    for row in fact_rows:
        row_job_id = str(row.get("job_id") or "")
        if row_job_id and row_job_id != job_id:
            reasons.append("attempt_facts.csv contains a different job_id")
        repeat_index = _to_positive_int(row.get("repeat_index"))
        query_id = str(row.get("query_id") or "")
        if not query_id or repeat_index is None:
            reasons.append("attempt_facts.csv contains an invalid logical unit key")
            continue
        key = (query_id, repeat_index)
        if key in actual_by_key:
            reasons.append("attempt_facts.csv contains duplicate logical unit keys")
            continue
        actual_by_key[key] = row
        expected = expected_by_key.get(key)
        if expected is None:
            reasons.append(f"attempt_facts.csv contains stale or unknown unit {query_id}#{repeat_index}")
            continue
        if str(row.get("latest_status") or "") != str(expected.get("status") or ""):
            reasons.append(f"attempt_facts.csv status is stale for {query_id}#{repeat_index}")
        expected_attempt_id = str(expected.get("attempt_id") or "") if expected.get("explicit_attempt_id") else ""
        actual_attempt_id = str(row.get("attempt_id") or "")
        if expected_attempt_id and actual_attempt_id != expected_attempt_id:
            reasons.append(f"attempt_facts.csv attempt_id is stale for {query_id}#{repeat_index}")
        actual_completed = _parse_timestamp(row.get("completed_at"))
        expected_completed = expected.get("completed_at_ts")
        if actual_completed is not None and expected_completed is not None and actual_completed != expected_completed:
            reasons.append(f"attempt_facts.csv completed_at is stale for {query_id}#{repeat_index}")

    if set(actual_by_key) != set(expected_by_key):
        reasons.append("attempt_facts.csv logical-unit universe does not match latest terminal raw attempts")
    stats_count = sum(1 for row in fact_rows if (_to_int(row.get("stats_included")) or 0) != 0)
    _compare_optional_int(summary, "stats_record_count", stats_count, reasons)
    analysis_count = sum(1 for row in fact_rows if (_to_int(row.get("valid_attempt")) or 0) != 0)
    _compare_optional_int(summary, "analysis_record_count", analysis_count, reasons)

    _, query_fact_rows = _read_csv_document(result / "query_facts.csv")
    if query_fact_rows:
        query_fact_ids = [str(row.get("query_id") or "") for row in query_fact_rows]
        expected_query_ids = (
            {str(value) for value in summary.get("query_ids", []) if str(value)} if isinstance(summary.get("query_ids"), list) else set(planned_query_rows)
        )
        if len(set(query_fact_ids)) != len(query_fact_ids) or "" in query_fact_ids:
            reasons.append("query_facts.csv contains empty or duplicate query_id values")
        if expected_query_ids and set(query_fact_ids) != expected_query_ids:
            reasons.append("query_facts.csv query universe does not match analysis input")

    _, quality_rows = _read_csv_document(result / "quality_summary.csv")
    expected_units = (_to_int(manifest.get("query_count")) or 0) * (_to_int(manifest.get("repeats")) or 0)
    for row in quality_rows:
        if row.get("job_id") not in (None, "", job_id):
            reasons.append("quality_summary.csv contains a different job_id")
        planned_units = _to_int(row.get("planned_units"))
        if expected_units and planned_units is not None and planned_units != expected_units:
            reasons.append("quality_summary.csv planned_units does not match manifest")

    summary_generation = _to_int(summary.get("run_generation")) or 0
    for stem, path in intelligence_paths.items():
        headers, rows = _read_csv_document(path)
        if len(headers) != len(set(headers)):
            reasons.append(f"{stem}.csv contains duplicate column names")
        for row in rows:
            if row.get("job_id") not in (None, "", job_id):
                reasons.append(f"{stem}.csv contains a different job_id")
                break
            generation_value = row.get("run_generation") or row.get("analysis_generation")
            if generation_value not in (None, "") and _to_int(generation_value) != summary_generation:
                reasons.append(f"{stem}.csv contains a stale run_generation")
                break
    return list(dict.fromkeys(reasons))
