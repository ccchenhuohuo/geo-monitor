from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Iterable

from .schemas import MonitorResult


RESUME_SUCCESS_STATUSES = {"success"}

CSV_FIELDS = [
    "run_id",
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        if hasattr(result, "model_dump_json"):
            f.write(result.model_dump_json(exclude_none=True))
        else:
            f.write(result.json(exclude_none=True, ensure_ascii=False))
        f.write("\n")


def write_jsonl(path: str | Path, records: Iterable[dict]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
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
                errors.append({
                    "path": str(source),
                    "line_no": line_no,
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                    "raw_line": line[:500],
                })
                continue
            if isinstance(data, dict):
                records.append(data)
            else:
                errors.append({
                    "path": str(source),
                    "line_no": line_no,
                    "type": "InvalidJsonlRecord",
                    "message": "JSONL 行必须是对象",
                    "raw_line": line[:500],
                })
    return records, errors


def canonical_request_hash(record: dict) -> str | None:
    existing = record.get("request_hash")
    if existing:
        return str(existing)
    raw_request = record.get("raw_request")
    if not isinstance(raw_request, dict) or not raw_request:
        return None
    stable = json.dumps(raw_request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


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
    for record in read_jsonl(output_path, strict=False):
        if record.get("status") in RESUME_SUCCESS_STATUSES:
            key = safe_result_key(record)
            if key is not None:
                keys.add(key)
    return keys


def successful_result_hashes(path: str | Path) -> dict[tuple[str, int], set[str]]:
    output_path = Path(path)
    if not output_path.exists():
        return {}
    hashes: dict[tuple[str, int], set[str]] = {}
    for record in read_jsonl(output_path, strict=False):
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
    ids: set[str] = set()
    for record in read_jsonl(output_path, strict=False):
        if record.get("status") in RESUME_SUCCESS_STATUSES:
            ids.add(str(record.get("query_id")))
    return ids


def latest_success_records(records: Iterable[dict]) -> list[dict]:
    return latest_records(records, statuses={"success"})


def latest_records(records: Iterable[dict], *, statuses: set[str]) -> list[dict]:
    latest: dict[tuple[str, int], dict] = {}
    for record in records:
        if record.get("status") not in statuses:
            continue
        key = safe_result_key(record)
        if key is None:
            continue
        normalized = dict(record)
        normalized["request_hash"] = canonical_request_hash(record)
        previous = latest.get(key)
        if previous is None or str(normalized.get("completed_at", "")) >= str(previous.get("completed_at", "")):
            latest[key] = normalized
    return sorted(latest.values(), key=lambda r: safe_result_key(r) or ("", 0))


def export_csv(records: Iterable[dict], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(sanitize_csv_row(_flatten_record(record)))


def sanitize_csv_row(row: dict[str, object]) -> dict[str, object]:
    return {key: sanitize_csv_cell(value) for key, value in row.items()}


def sanitize_csv_cell(value: object) -> object:
    if value is None:
        return value
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
        "run_id": record.get("run_id"),
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
