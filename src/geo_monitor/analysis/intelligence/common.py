"""Shared, side-effect-free helpers for the intelligence layer.

The intelligence package intentionally accepts and returns ``list[dict]`` so it
can sit on top of CSV rows, DuckDB results, or in-memory facts without coupling
to the current analysis pipeline.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

DENOMINATOR_FIELDS = (
    "planned_attempts",
    "completed_attempts",
    "valid_attempts",
    "eligible_attempts",
    "stats_included_attempts",
    "sample_completeness",
    "usable_sample_rate",
)


def as_number(value: Any) -> float | None:
    """Return a finite float, preserving N/A as ``None``."""

    if value is None or value == "" or isinstance(value, bool):
        return None
    text = str(value).strip()
    if text.endswith("%"):
        text = text[:-1]
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def as_ratio(value: Any) -> float | None:
    """Normalize percentages, 0..100 scores, or 0..1 ratios to 0..1."""

    if value is None or value == "":
        return None
    text = str(value).strip()
    is_percent = text.endswith("%")
    number = as_number(value)
    if number is None:
        return None
    if is_percent or abs(number) > 1:
        number /= 100.0
    return clamp(number, 0.0, 1.0)


def as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "是"}:
            return True
        if text in {"false", "0", "no", "n", "否"}:
            return False
    return default


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    number = as_number(value)
    if number is None or not number.is_integer() or number <= 0:
        return None
    return int(number)


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return min(upper, max(lower, value))


def safe_div(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def mean(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return sum(clean) / len(clean) if clean else None


def score100(value: float | None, *, digits: int = 2) -> float | None:
    return round(clamp(value) * 100.0, digits) if value is not None else None


def response_key(row: Mapping[str, Any]) -> tuple[str, str, int | str]:
    """Stable response/attempt key shared by brand, source, and query facts."""

    job_id = str(row.get("job_id") or row.get("run_id") or "")
    query_id = str(row.get("query_id") or "")
    repeat = positive_int(row.get("repeat_index"))
    if query_id:
        return job_id, query_id, repeat or 1
    return job_id, "__attempt__", str(row.get("attempt_id") or "")


def trace_fields(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[Any]]:
    materialized = list(rows)
    return {
        "trace_job_ids": _distinct(row.get("job_id") or row.get("run_id") for row in materialized),
        "trace_query_ids": _distinct(row.get("query_id") for row in materialized),
        "trace_attempt_ids": _distinct(row.get("attempt_id") for row in materialized),
        "trace_repeat_indices": _distinct(positive_int(row.get("repeat_index")) for row in materialized),
    }


def denominator_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in DENOMINATOR_FIELDS if field in row}


def parse_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """Merge explicit query dimensions with optional JSON metadata."""

    raw = row.get("query_metadata_json")
    metadata: dict[str, Any] = {}
    if isinstance(raw, Mapping):
        metadata.update(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            decoded = {}
        if isinstance(decoded, dict):
            metadata.update(decoded)
    for field in ("seed_id", "persona", "intent", "template_id", "scenario", "category", "query_weight"):
        if row.get(field) not in (None, ""):
            metadata[field] = row.get(field)
    return metadata


def core_record_eligible(row: Mapping[str, Any], *, min_confidence: float, require_evidence: bool = True) -> bool:
    """Conservative eligibility gate for LLM-derived intelligence records."""

    if "stats_included" in row and not as_bool(row.get("stats_included")):
        return False
    if row.get("is_traceable") is False or str(row.get("traceability_status") or "").lower() in {
        "invalid",
        "untraceable",
        "quarantined",
    }:
        return False
    if require_evidence and not str(row.get("evidence") or "").strip():
        return False
    confidence = as_number(row.get("confidence"))
    return confidence is not None and 0.0 <= confidence <= 1.0 and confidence >= min_confidence


def _distinct(values: Iterable[Any]) -> list[Any]:
    filtered = {value for value in values if value not in (None, "")}
    return sorted(filtered, key=lambda value: (str(type(value)), str(value)))
