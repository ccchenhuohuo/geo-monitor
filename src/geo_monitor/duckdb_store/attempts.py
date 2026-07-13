"""Raw attempt ingestion and latest-attempt selection."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..query_meta import query_metadata_json, tags_text
from .contracts import TERMINAL_ATTEMPT_STATUSES, _parse_timestamp, _to_bool, _to_int, _to_positive_int


def _read_attempts(
    raw_path: Path, job_id: str, fallback_rows: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    queries: dict[str, dict[str, str]] = {}
    query_field_priority: dict[str, dict[str, int]] = {}
    flags: list[dict[str, Any]] = []
    if not raw_path.exists():
        return attempts, queries, flags
    if raw_path.is_symlink() or not raw_path.is_file():
        flags.append(
            {
                "type": "unsafe_raw_attempts",
                "message": "raw attempts must be a regular non-symlink file",
                "path": str(raw_path),
                "raw_line_number": 0,
                "query_id": "",
            }
        )
        return attempts, queries, flags
    with raw_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                flags.append({"type": "malformed_jsonl", "message": str(exc), "path": str(raw_path), "raw_line_number": line_no, "query_id": ""})
                continue
            qid = str(record.get("query_id") or "")
            query = str(record.get("query") or record.get("input_query") or "")
            meta = record.get("query_meta") if isinstance(record.get("query_meta"), dict) else {}
            record_metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            if record_metadata:
                meta = _merge_record_metadata(meta, record_metadata)
            fallback = fallback_rows.get(qid)
            if not meta and fallback:
                meta = fallback
                flags.append(
                    {
                        "type": "query_meta_fallback_used",
                        "message": "query_meta filled from fallback manifest",
                        "path": str(raw_path),
                        "raw_line_number": line_no,
                        "query_id": qid,
                    }
                )
            elif not meta:
                flags.append(
                    {"type": "query_meta_missing", "message": "query_meta missing", "path": str(raw_path), "raw_line_number": line_no, "query_id": qid}
                )
            elif fallback:
                conflicts = [
                    key for key, value in fallback.items() if key in meta and str(meta.get(key) or "") and str(value or "") and str(meta.get(key)) != str(value)
                ]
                if conflicts:
                    flags.append(
                        {"type": "query_meta_conflict", "message": ",".join(conflicts), "path": str(raw_path), "raw_line_number": line_no, "query_id": qid}
                    )
            error = record.get("error") if isinstance(record.get("error"), dict) else {}
            sampling_profile = record.get("sampling_profile") if isinstance(record.get("sampling_profile"), dict) else {}
            response_text = str(record.get("response_text") or "")
            attempt_id = str(record.get("attempt_id") or f"{job_id}__{qid}__r{record.get('repeat_index', 1)}__{record.get('request_hash', '')}")
            created_at = str(record.get("started_at") or "")
            completed_at = str(record.get("completed_at") or "")
            repeat_index = _to_positive_int(record.get("repeat_index", 1))
            # Legacy attempts used an execution-scoped run_id that was not the
            # bundle job_id, so only an explicit job_id participates in this check.
            record_job_id = str(record.get("job_id") or "")
            if repeat_index is None:
                flags.append(
                    {
                        "type": "invalid_repeat_index",
                        "message": "repeat_index must be a positive integer",
                        "path": str(raw_path),
                        "raw_line_number": line_no,
                        "query_id": qid,
                    }
                )
            if record_job_id and record_job_id != job_id:
                flags.append(
                    {
                        "type": "attempt_job_id_mismatch",
                        "message": f"attempt job_id={record_job_id!r} differs from manifest job_id",
                        "path": str(raw_path),
                        "raw_line_number": line_no,
                        "query_id": qid,
                    }
                )
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "explicit_attempt_id": bool(record.get("attempt_id")),
                    "query_id": qid,
                    "repeat_index": repeat_index,
                    "run_generation": _to_int(record.get("run_generation")),
                    "diagnostic_generation": _to_int(record.get("diagnostic_generation")),
                    "execution_mode": str(record.get("execution_mode") or "live"),
                    "record_job_id": record_job_id,
                    "status": str(record.get("status") or ""),
                    "latency_ms": _to_int(record.get("latency_ms")),
                    "error": str(error.get("message") or error.get("type") or ""),
                    "model": str(record.get("model") or ""),
                    "provider": str(sampling_profile.get("provider") or ""),
                    "adapter": str(sampling_profile.get("adapter") or ""),
                    "adapter_version": str(sampling_profile.get("adapter_version") or ""),
                    "api_family": str(sampling_profile.get("api_family") or ""),
                    "request_fingerprint_version": str(record.get("request_fingerprint_version") or ""),
                    "web_search_performed": _to_bool(record.get("web_search_performed")),
                    "web_search_evidence": str(record.get("web_search_evidence") or ""),
                    "web_search_requirement_status": str(record.get("web_search_requirement_status") or ""),
                    "source_parse_status": str(record.get("source_parse_status") or ""),
                    "created_at": created_at,
                    "completed_at": completed_at,
                    "created_at_ts": _parse_timestamp(created_at),
                    "completed_at_ts": _parse_timestamp(completed_at),
                    "request_hash": str(record.get("request_hash") or ""),
                    "response_preview": response_text[:500],
                    "response_length": len(response_text),
                    "raw_path": str(raw_path),
                    "raw_line_number": line_no,
                }
            )
            if qid:
                priority = (
                    3 if record.get("status") in {"success", "mock"} and record.get("query_meta") else 2 if record.get("query_meta") else 1 if fallback else 0
                )
                _merge_query_row(queries, query_field_priority, qid, query, meta, priority)
    return attempts, queries, flags


def _merge_query_row(
    queries: dict[str, dict[str, str]],
    priorities: dict[str, dict[str, int]],
    qid: str,
    query: str,
    meta: dict[str, Any],
    priority: int,
) -> None:
    row = queries.setdefault(
        qid,
        {
            "query_id": qid,
            "variant_id": "",
            "seed_id": "",
            "seed_query": "",
            "category": "",
            "intent": "",
            "persona": "",
            "template_id": "",
            "query": "",
            "locale": "",
            "market": "",
            "tags": "",
            "language": "",
            "generation_method": "",
            "fanout_version": "",
            "manifest_version": "",
            "locked_at": "",
            "query_metadata_json": "{}",
        },
    )
    field_priorities = priorities.setdefault(qid, {})
    values = {
        "query": query,
        "variant_id": str(meta.get("variant_id") or ""),
        "seed_id": str(meta.get("seed_id") or ""),
        "seed_query": str(meta.get("seed_query") or ""),
        "category": str(meta.get("category") or ""),
        "intent": str(meta.get("intent") or ""),
        "persona": str(meta.get("persona") or ""),
        "template_id": str(meta.get("template_id") or ""),
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
    for key, value in values.items():
        if not value:
            continue
        current_priority = field_priorities.get(key, -1)
        if not row.get(key) or priority >= current_priority:
            row[key] = value
            field_priorities[key] = priority


def _merge_record_metadata(meta: dict[str, Any], record_metadata: dict[str, Any]) -> dict[str, Any]:
    merged = dict(record_metadata)
    merged.update({key: value for key, value in meta.items() if value not in (None, "")})
    return merged


def _latest_analysis_attempts(attempts: list[dict[str, Any]], *, sample_mode: str) -> list[dict[str, Any]]:
    if sample_mode == "live":
        statuses = {"success", "error"}
    elif sample_mode == "mock":
        statuses = {"mock", "error"}
    else:
        statuses = TERMINAL_ATTEMPT_STATUSES
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for row in attempts:
        repeat_index = _to_positive_int(row.get("repeat_index"))
        query_id = str(row.get("query_id") or "")
        if not query_id or repeat_index is None or row.get("status") not in statuses:
            continue
        key = (query_id, repeat_index)
        previous = latest.get(key)
        if previous is None or _attempt_order_key(row) >= _attempt_order_key(previous):
            latest[key] = row
    return [latest[key] for key in sorted(latest)]


def _attempt_order_key(row: dict[str, Any]) -> tuple[int, datetime, int]:
    timestamp = row.get("completed_at_ts") or row.get("created_at_ts")
    return (
        int(timestamp is not None),
        timestamp or datetime.min.replace(tzinfo=timezone.utc),
        int(row.get("raw_line_number") or 0),
    )
