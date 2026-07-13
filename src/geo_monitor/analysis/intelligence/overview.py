"""Explainable GEO overview scores (v1)."""

from __future__ import annotations

from typing import Any

from .common import as_number, as_ratio, clamp, denominator_fields, mean, score100

VISIBILITY_WEIGHTS = {
    "mention_rate": 0.30,
    "query_coverage": 0.25,
    "prominence_score": 0.20,
    "rank_score": 0.15,
    "sov_score": 0.10,
}


def compute_overview_scores(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute five 0..100 scores while retaining every component.

    Each input row represents an already joined run/target-brand breakdown.  A
    missing source sample returns ``source_score=None`` rather than zero.
    Business scores do not include quality; quality is reported independently.
    """

    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        mention = _ratio(row, "response_mention_rate") or 0.0
        query_coverage = _ratio(row, "query_coverage_rate") or 0.0
        rank = _rank_score(row)
        top3 = _ratio(row, "top3_rate")
        prominence = _ratio(row, "prominence_score")
        if prominence is None:
            prominence = mean([top3, rank])
        prominence = prominence or 0.0
        sov = _ratio(row, "sov_response_share", "sov_score") or 0.0
        visibility_components = {
            "mention_rate": mention,
            "query_coverage": query_coverage,
            "prominence_score": prominence,
            "rank_score": rank,
            "sov_score": sov,
        }
        visibility = sum(VISIBILITY_WEIGHTS[name] * value for name, value in visibility_components.items())

        conversion = _ratio(row, "recommendation_conversion", "recommended_rate_when_mentioned")
        weighted = _ratio(row, "weighted_recommendation_score", "weighted_recommendation_strength")
        recommendation_values = [value for value in (conversion, weighted) if value is not None]
        recommendation = mean(recommendation_values)

        target_win = _ratio(row, "target_win_rate", "win_rate")
        replacement = _ratio(row, "replacement_risk")
        competitor = (
            None
            if target_win is None and replacement is None
            else mean(
                [
                    target_win,
                    None if replacement is None else 1.0 - replacement,
                ]
            )
        )

        raw_citation_count = row.get("citation_count")
        if raw_citation_count in (None, ""):
            raw_citation_count = row.get("parsed_source_occurrences")
        citation_count = as_number(raw_citation_count)
        source_observable = row.get("source_observable")
        source_components = {
            "source_coverage": _ratio(row, "source_coverage_rate", "response_coverage_rate"),
            "source_diversity": _ratio(row, "source_diversity_score"),
            "owned_source_rate": _ratio(row, "owned_source_rate"),
        }
        if source_observable is False or citation_count == 0 or all(value is None for value in source_components.values()):
            source_score = None
        else:
            source_score = mean(source_components.values())

        usable = _ratio(row, "usable_sample_rate")
        completeness = _ratio(row, "sample_completeness")
        confidence_health = _ratio(row, "confidence_health", "avg_confidence")
        extraction_error = _ratio(row, "extraction_error_rate")
        quality_components = {
            "usable_sample_rate": usable,
            "sample_completeness": completeness,
            "confidence_health": confidence_health,
            "extraction_success_rate": None if extraction_error is None else 1.0 - extraction_error,
        }
        quality_weights = {
            "usable_sample_rate": 0.45,
            "sample_completeness": 0.25,
            "confidence_health": 0.15,
            "extraction_success_rate": 0.15,
        }
        present_weight = sum(quality_weights[key] for key, value in quality_components.items() if value is not None)
        quality = (
            sum(quality_weights[key] * value for key, value in quality_components.items() if value is not None) / present_weight if present_weight else None
        )

        result = {
            "job_id": row.get("job_id") or row.get("run_id") or "",
            "brand_name_canonical": row.get("brand_name_canonical") or row.get("target_brand") or "",
            "visibility_score": score100(visibility),
            "recommendation_score": score100(recommendation),
            "competitor_score": score100(competitor),
            "source_score": score100(source_score),
            "quality_score": score100(quality),
            "visibility_breakdown": _breakdown(visibility_components, VISIBILITY_WEIGHTS),
            "recommendation_breakdown": {"recommendation_conversion": conversion, "weighted_recommendation_score": weighted},
            "competitor_breakdown": {"target_win_rate": target_win, "inverse_replacement_risk": None if replacement is None else 1.0 - replacement},
            "source_breakdown": source_components,
            "quality_breakdown": quality_components,
            **denominator_fields(row),
        }
        for field in ("trace_job_ids", "trace_query_ids", "trace_attempt_ids"):
            if field in row:
                result[field] = list(row.get(field) or [])
        output.append(result)
    return output


def build_overview_scores(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return compute_overview_scores(rows)


def _ratio(row: dict[str, Any], *fields: str) -> float | None:
    for field in fields:
        if row.get(field) not in (None, ""):
            value = as_ratio(row.get(field))
            if value is not None:
                return value
    return None


def _rank_score(row: dict[str, Any]) -> float:
    direct = _ratio(row, "rank_score")
    if direct is not None:
        return direct
    rank = as_number(row.get("avg_rank_position") or row.get("rank_position"))
    return clamp(1.0 / rank) if rank is not None and rank > 0 else 0.0


def _breakdown(components: dict[str, float], weights: dict[str, float]) -> dict[str, dict[str, float]]:
    return {key: {"value": value, "weight": weights[key], "weighted_value": round(value * weights[key], 6)} for key, value in components.items()}
