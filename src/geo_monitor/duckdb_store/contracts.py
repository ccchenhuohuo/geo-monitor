"""Shared contracts and scalar conversions for the optional DuckDB store."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DUCKDB_SCHEMA_VERSION = "duckdb-schema-v6"
TERMINAL_ATTEMPT_STATUSES = {"success", "mock", "error", "dry_run", "interrupted"}
CORE_RESULT_CSV_STEMS = (
    "brand_summary",
    "brand_by_query",
    "query_stability",
    "source_domains",
    "source_urls",
    "quality_summary",
    "attempt_facts",
    "query_facts",
    "brand_attempt_facts",
)
INTELLIGENCE_CSV_STEMS = (
    "geo_overview_scores",
    "visibility_summary",
    "recommendations",
    "recommendation_summary",
    "recommendation_by_persona",
    "competitor_edges",
    "competitor_win_loss",
    "competitor_replacements",
    "rank_gap",
    "source_types",
    "brand_source_domains",
    "brand_source_urls",
    "source_gaps",
    "visibility_by_seed",
    "visibility_by_persona",
    "visibility_by_intent",
    "visibility_by_scenario",
    "perception_claims",
    "perception_strengths",
    "perception_weaknesses",
    "perception_pricing",
    "perception_audience_fit",
    "trend_deltas",
    "trend_drift",
    "trend_volatility",
    "opportunity_query_gaps",
    "opportunity_persona_gaps",
    "opportunity_source_gaps",
    "opportunity_messaging_gaps",
)


class DuckDBError(ValueError):
    """A safe public error raised by the optional DuckDB store."""


def _quality(con: Any, job_id: str, type: str, message: str, path: str, raw_line_number: int | None = None, query_id: str = "") -> None:
    con.execute("insert into quality_flags values (?, ?, ?, ?, ?, ?)", [job_id, type, message, path, raw_line_number, query_id])


def _compare_optional_int(source: dict[str, Any], field: str, expected: int | None, reasons: list[str]) -> None:
    if expected is None or source.get(field) in (None, ""):
        return
    actual = _to_int(source.get(field))
    if actual != expected:
        reasons.append(f"analysis summary {field}={actual!r} does not match expected {expected}")


def _pct(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("%"):
        return _to_float(text[:-1], scale=100.0)
    return _to_float(text)


def _to_float(value: Any, *, scale: float = 1.0) -> float | None:
    try:
        return float(value) / scale
    except Exception:
        return None


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


def _to_positive_int(value: Any) -> int | None:
    parsed = _to_int(value)
    return parsed if parsed is not None and parsed >= 1 else None


def _to_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _parse_timestamp(value: Any) -> datetime | None:
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
