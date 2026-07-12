"""Rule-based, traceable opportunity tables."""

from __future__ import annotations

from typing import Any

from .common import as_number, as_ratio, clamp


def build_query_opportunities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _rank(
        rows,
        "query_gap",
        {
            "competitor_visibility": ("competitor_visibility", "competitor_visibility_rate"),
            "competitor_recommendation_strength": ("competitor_recommendation_strength", "weighted_recommendation_score"),
            "query_quality_score": ("query_quality_score", "quality_score"),
        },
        id_fields=("job_id", "query_id", "query", "competitor_brand"),
    )


def build_persona_opportunities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _rank(
        rows,
        "persona_gap",
        {
            "persona_gap": ("persona_gap", "visibility_gap"),
            "competitor_strength": ("competitor_strength", "competitor_visibility"),
            "persona_quality_score": ("persona_quality_score", "quality_score"),
        },
        id_fields=("job_id", "persona", "competitor_brand"),
    )


def build_source_opportunities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for source in rows:
        row = dict(source)
        target_coverage = _first_ratio(row, ("target_source_coverage", "target_coverage_rate"))
        if target_coverage is None and row.get("source_gap_rate") not in (None, ""):
            target_coverage = 0.0
        row["target_source_gap"] = None if target_coverage is None else 1.0 - target_coverage
        normalized.append(row)
    return _rank(
        normalized,
        "source_gap",
        {
            "competitor_source_coverage": ("competitor_source_coverage", "source_gap_rate"),
            "target_source_gap": ("target_source_gap",),
            "source_quality_score": ("source_quality_score", "quality_score"),
        },
        id_fields=("job_id", "competitor_brand", "domain", "canonical_url"),
    )


def build_messaging_opportunities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _rank(
        rows,
        "messaging_gap",
        {
            "competitor_claim_strength": ("competitor_claim_strength", "claim_strength"),
            "audience_relevance": ("audience_relevance", "persona_alignment"),
            "message_quality_score": ("message_quality_score", "quality_score"),
        },
        id_fields=("job_id", "persona", "claim_canonical", "competitor_brand"),
    )


def build_opportunity_tables(
    *,
    query_rows: list[dict[str, Any]] | None = None,
    persona_rows: list[dict[str, Any]] | None = None,
    source_rows: list[dict[str, Any]] | None = None,
    messaging_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = (
        build_query_opportunities(query_rows or [])
        + build_persona_opportunities(persona_rows or [])
        + build_source_opportunities(source_rows or [])
        + build_messaging_opportunities(messaging_rows or [])
    )
    return sorted(
        rows,
        key=lambda row: (
            -float(row["opportunity_score"]),
            str(row["opportunity_type"]),
            str(row.get("query_id") or row.get("persona") or row.get("domain") or row.get("claim_canonical") or ""),
        ),
    )


def _rank(
    rows: list[dict[str, Any]],
    opportunity_type: str,
    factors: dict[str, tuple[str, ...]],
    *,
    id_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    output = []
    for source in rows:
        values = {name: _first_ratio(source, fields) for name, fields in factors.items()}
        if any(value is None for value in values.values()):
            score = None
        else:
            raw = 1.0
            for value in values.values():
                raw *= float(value)
            weight = as_number(source.get("query_weight") or source.get("segment_weight") or source.get("weight"))
            raw *= weight if weight is not None and weight >= 0 else 1.0
            score = round(clamp(raw) * 100.0, 2)
        if score is None or score <= 0:
            continue
        result = {
            "opportunity_type": opportunity_type,
            **{field: source.get(field) or "" for field in id_fields},
            "opportunity_score": score,
            "factor_breakdown": values,
            "factor_denominator": len(values),
            "factor_observed_count": sum(value is not None for value in values.values()),
        }
        for field in ("trace_job_ids", "trace_query_ids", "trace_attempt_ids", "trace_source_ids"):
            if field in source:
                result[field] = list(source.get(field) or [])
        if source.get("attempt_id"):
            result["trace_attempt_ids"] = [source["attempt_id"]]
        if source.get("query_id"):
            result["trace_query_ids"] = [source["query_id"]]
        output.append(result)
    return sorted(output, key=lambda row: (-float(row["opportunity_score"]), *(str(row.get(field) or "") for field in id_fields)))


def _first_ratio(row: dict[str, Any], fields: tuple[str, ...]) -> float | None:
    for field in fields:
        value = as_ratio(row.get(field))
        if value is not None:
            return value
    return None
