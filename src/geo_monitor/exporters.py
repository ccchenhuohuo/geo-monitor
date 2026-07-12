from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .filesystem import open_private_text
from .request_fingerprint import REQUEST_FINGERPRINT_VERSION, legacy_payload_hash, request_fingerprint
from .schemas import MonitorResult

RESUME_SUCCESS_STATUSES = {"success"}
TERMINAL_STATUSES = {"success", "mock", "error", "dry_run", "interrupted"}
LIVE_TERMINAL_STATUSES = {"success", "error", "interrupted"}

CSV_FIELDS = [
    "job_id",
    "run_id",
    "run_execution_id",
    "run_generation",
    "diagnostic_generation",
    "execution_mode",
    "logical_unit_id",
    "attempt_id",
    "query_id",
    "repeat_index",
    "repeat_total",
    "request_hash",
    "model",
    "status",
    "latency_ms",
    "input_query",
    "response_text",
    "source_count",
    "source_domains",
    "source_urls",
    "usage_input_tokens",
    "usage_output_tokens",
    "usage_total_tokens",
    "usage_web_search",
    "error_type",
    "error_message",
    "started_at",
    "completed_at",
]


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def append_jsonl(path: str | Path, result: MonitorResult) -> None:
    output_path = Path(path)
    with open_private_text(output_path, append=True) as f:
        if hasattr(result, "model_dump_json"):
            f.write(result.model_dump_json(exclude_none=True))
        else:
            f.write(result.json(exclude_none=True, ensure_ascii=False))
        f.write("\n")


def write_jsonl(path: str | Path, records: Iterable[dict]) -> None:
    output_path = Path(path)
    with open_private_text(output_path) as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


def read_jsonl(path: str | Path, *, strict: bool = True) -> list[dict]:
    records, errors = read_jsonl_with_errors(path)
    if strict and errors:
        first = errors[0]
        raise json.JSONDecodeError(first["message"], first.get("raw_line", ""), 0)
    return records


def read_jsonl_with_errors(path: str | Path) -> tuple[list[dict], list[dict]]:
    records: list[dict] = []
    errors: list[dict] = []
    source = Path(path)
    with source.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(
                    {
                        "path": str(source),
                        "line_no": line_no,
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                        "raw_line": line[:500],
                    }
                )
                continue
            if isinstance(data, dict):
                records.append(data)
            else:
                errors.append(
                    {
                        "path": str(source),
                        "line_no": line_no,
                        "type": "InvalidJsonlRecord",
                        "message": "JSONL 行必须是对象",
                        "raw_line": line[:500],
                    }
                )
    return records, errors


def canonical_request_hash(record: dict) -> str | None:
    basis = record.get("request_fingerprint_basis")
    if isinstance(basis, dict) and record.get("request_fingerprint_version") == REQUEST_FINGERPRINT_VERSION:
        return request_fingerprint(basis)
    raw_request = record.get("raw_request")
    if isinstance(raw_request, dict) and raw_request:
        return legacy_payload_hash(raw_request)
    existing = record.get("request_hash")
    if existing:
        return str(existing)
    return None


def result_key(record: dict) -> tuple[str, int]:
    key = safe_result_key(record)
    if key is None:
        raise ValueError("记录缺少有效的 query_id 或 repeat_index")
    return key


def safe_result_key(record: dict) -> tuple[str, int] | None:
    query_id = str(record.get("query_id") or "").strip()
    if not query_id:
        return None
    raw_repeat = record.get("repeat_index", 1)
    if raw_repeat in (None, ""):
        raw_repeat = 1
    repeat_index = _safe_positive_int(raw_repeat)
    if repeat_index is None:
        return None
    return query_id, repeat_index


def _safe_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        parsed = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        parsed = int(text)
    else:
        return None
    return parsed if parsed >= 1 else None


def successful_result_keys(path: str | Path) -> set[tuple[str, int]]:
    output_path = Path(path)
    if not output_path.exists():
        return set()
    keys: set[tuple[str, int]] = set()
    for record in latest_live_terminal_records(read_jsonl(output_path, strict=False)):
        if record.get("status") not in RESUME_SUCCESS_STATUSES:
            continue
        key = safe_result_key(record)
        if key is not None:
            keys.add(key)
    return keys


def successful_result_hashes(path: str | Path) -> dict[tuple[str, int], set[str]]:
    output_path = Path(path)
    if not output_path.exists():
        return {}
    hashes: dict[tuple[str, int], set[str]] = {}
    for record in latest_live_terminal_records(read_jsonl(output_path, strict=False)):
        if record.get("status") not in RESUME_SUCCESS_STATUSES:
            continue
        request_hash = canonical_request_hash(record)
        if not request_hash:
            continue
        key = safe_result_key(record)
        if key is None:
            continue
        hashes.setdefault(key, set()).add(request_hash)
    return hashes


def successful_query_ids(path: str | Path) -> set[str]:
    output_path = Path(path)
    if not output_path.exists():
        return set()
    return {
        str(record.get("query_id"))
        for record in latest_live_terminal_records(read_jsonl(output_path, strict=False))
        if record.get("status") in RESUME_SUCCESS_STATUSES
    }


def latest_success_records(records: Iterable[dict]) -> list[dict]:
    return latest_records(records, statuses={"success"})


def latest_terminal_records(records: Iterable[dict]) -> list[dict]:
    return latest_records(records, statuses=TERMINAL_STATUSES)


def latest_live_terminal_records(records: Iterable[dict]) -> list[dict]:
    live_records = (record for record in records if str(record.get("execution_mode") or "live") == "live")
    return latest_records(live_records, statuses=LIVE_TERMINAL_STATUSES)


def latest_records(records: Iterable[dict], *, statuses: set[str]) -> list[dict]:
    latest: dict[tuple[str, int], tuple[tuple[int, datetime, int], dict]] = {}
    for position, record in enumerate(records):
        if record.get("status") not in statuses:
            continue
        key = safe_result_key(record)
        if key is None:
            continue
        normalized = dict(record)
        normalized["request_hash"] = canonical_request_hash(record)
        ordering = _record_time_key(normalized, position)
        previous = latest.get(key)
        if previous is None or ordering >= previous[0]:
            latest[key] = (ordering, normalized)
    return sorted((item[1] for item in latest.values()), key=lambda r: safe_result_key(r) or ("", 0))


def _record_time_key(record: dict, position: int) -> tuple[int, datetime, int]:
    for field in ("completed_at", "started_at"):
        parsed = _parse_timestamp(record.get(field))
        if parsed is not None:
            return 1, parsed, position
    return 0, datetime.min.replace(tzinfo=timezone.utc), position


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def export_csv(records: Iterable[dict], path: str | Path) -> None:
    output_path = Path(path)
    with open_private_text(output_path, encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(sanitize_csv_row(_flatten_record(record)))


def sanitize_csv_row(row: dict[str, object]) -> dict[str, object]:
    return {key: sanitize_csv_cell(value) for key, value in row.items()}


def sanitize_csv_cell(value: object) -> object:
    if value is None:
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not isinstance(value, str):
        return value
    stripped = value.lstrip(" \t\r\n")
    if value.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _flatten_record(record: dict) -> dict[str, object]:
    usage = record.get("usage") or {}
    tool_usage = usage.get("tool_usage") or {}
    error = record.get("error") or {}
    sources = record.get("sources") or []
    urls = [source.get("url") for source in sources if isinstance(source, dict) and source.get("url")]
    domains = sorted({source.get("domain") for source in sources if isinstance(source, dict) and source.get("domain")})
    return {
        "job_id": record.get("job_id"),
        "run_id": record.get("run_id"),
        "run_execution_id": record.get("run_execution_id"),
        "run_generation": record.get("run_generation"),
        "diagnostic_generation": record.get("diagnostic_generation"),
        "execution_mode": record.get("execution_mode"),
        "logical_unit_id": record.get("logical_unit_id"),
        "attempt_id": record.get("attempt_id"),
        "query_id": record.get("query_id"),
        "repeat_index": record.get("repeat_index") or 1,
        "repeat_total": record.get("repeat_total") or 1,
        "request_hash": canonical_request_hash(record),
        "model": record.get("model"),
        "status": record.get("status"),
        "latency_ms": record.get("latency_ms"),
        "input_query": record.get("input_query"),
        "response_text": record.get("response_text"),
        "source_count": len(sources),
        "source_domains": " ".join(domains),
        "source_urls": " ".join(urls),
        "usage_input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
        "usage_output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
        "usage_total_tokens": usage.get("total_tokens"),
        "usage_web_search": tool_usage.get("web_search"),
        "error_type": error.get("type"),
        "error_message": error.get("message"),
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
    }
