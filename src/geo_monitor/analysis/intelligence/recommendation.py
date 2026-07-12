"""Recommendation type normalization and auditable aggregation."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .common import as_bool, core_record_eligible, mean, positive_int, response_key, safe_div, score100, trace_fields

RECOMMENDATION_WEIGHTS: dict[str, float] = {
    "top_pick": 1.0,
    "recommended": 0.8,
    "best_for_use_case": 0.75,
    "strong_alternative": 0.65,
    "budget_pick": 0.65,
    "premium_pick": 0.65,
    "conditional": 0.35,
    "mentioned_only": 0.0,
    "warning": -0.5,
    "discouraged": -1.0,
    "not_mentioned": 0.0,
}

RECOMMENDATION_PRIORITY = {
    "top_pick": 100,
    "recommended": 90,
    "best_for_use_case": 85,
    "strong_alternative": 80,
    "budget_pick": 75,
    "premium_pick": 75,
    "conditional": 60,
    "discouraged": 55,
    "warning": 50,
    "mentioned_only": 10,
    "not_mentioned": 0,
}

POSITIVE_TYPES = {
    "top_pick",
    "recommended",
    "best_for_use_case",
    "strong_alternative",
    "budget_pick",
    "premium_pick",
    "conditional",
}


def classify_recommendation_type(row: dict[str, Any]) -> str:
    explicit = str(row.get("recommendation_type") or "").strip().lower()
    if explicit in RECOMMENDATION_WEIGHTS:
        return explicit
    role = str(row.get("role") or "").strip().lower()
    if role in {"discouraged", "not_recommended", "avoid"}:
        return "discouraged"
    if role in {"warning", "caution"}:
        return "warning"
    recommended = as_bool(row.get("is_recommended")) or role == "recommended"
    if recommended and positive_int(row.get("rank_position")) == 1:
        return "top_pick"
    if recommended:
        return "recommended"
    return "mentioned_only"


def recommendation_weight(row: dict[str, Any]) -> float:
    return RECOMMENDATION_WEIGHTS[classify_recommendation_type(row)]


def strongest_attempt_brand_rows(
    rows: list[dict[str, Any]],
    *,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    """Return at most one core-eligible row per response and brand."""

    selected: dict[tuple[str, tuple[str, str, int | str]], dict[str, Any]] = {}
    for source in rows:
        if not core_record_eligible(source, min_confidence=min_confidence):
            continue
        row = dict(source)
        brand = str(row.get("brand_name_canonical") or row.get("brand_name_raw") or "").strip()
        if not brand:
            continue
        recommendation_type = classify_recommendation_type(row)
        row["recommendation_type"] = recommendation_type
        key = (brand, response_key(row))
        previous = selected.get(key)
        if previous is None or _row_priority(row) > _row_priority(previous):
            selected[key] = row
    return sorted(selected.values(), key=lambda row: (str(row.get("brand_name_canonical") or row.get("brand_name_raw")), response_key(row)))


def aggregate_recommendations(
    rows: list[dict[str, Any]],
    attempt_facts: list[dict[str, Any]] | None = None,
    *,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    core_rows = strongest_attempt_brand_rows(rows, min_confidence=min_confidence)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    input_counts: Counter[str] = Counter()
    for row in rows:
        brand = str(row.get("brand_name_canonical") or row.get("brand_name_raw") or "").strip()
        if brand:
            input_counts[brand] += 1
    for row in core_rows:
        grouped[str(row.get("brand_name_canonical") or row.get("brand_name_raw"))].append(row)

    if attempt_facts is not None:
        eligible_attempts = len({response_key(row) for row in attempt_facts if as_bool(row.get("stats_included"), default=True)})
    else:
        eligible_attempts = len({response_key(row) for row in rows})
    total_top_picks = sum(1 for row in core_rows if row["recommendation_type"] == "top_pick")

    output: list[dict[str, Any]] = []
    for brand in sorted(input_counts):
        brand_rows = grouped.get(brand, [])
        distribution = Counter(str(row["recommendation_type"]) for row in brand_rows)
        mentioned_attempts = len(brand_rows)
        positive = sum(distribution[item] for item in POSITIVE_TYPES)
        raw_strength = mean(RECOMMENDATION_WEIGHTS[row["recommendation_type"]] for row in brand_rows)
        result = {
            "brand_name_canonical": brand,
            "eligible_attempts": eligible_attempts,
            "brand_observed_attempts": mentioned_attempts,
            "recommendation_denominator": mentioned_attempts,
            "input_record_count": input_counts[brand],
            "core_record_count": len(brand_rows),
            "rejected_record_count": input_counts[brand] - len(brand_rows),
            "recommendation_type_distribution": {key: distribution.get(key, 0) for key in RECOMMENDATION_WEIGHTS},
            "top_pick_count": distribution["top_pick"],
            "top_pick_rate": safe_div(distribution["top_pick"], mentioned_attempts),
            "top_pick_share": safe_div(distribution["top_pick"], total_top_picks),
            "conditional_recommendation_rate": safe_div(distribution["conditional"], mentioned_attempts),
            "warning_rate": safe_div(distribution["warning"], mentioned_attempts),
            "discouraged_rate": safe_div(distribution["discouraged"], mentioned_attempts),
            "recommendation_conversion": safe_div(positive, mentioned_attempts),
            "weighted_recommendation_strength": None if raw_strength is None else round(raw_strength * 100.0, 2),
            "weighted_recommendation_score": score100(None if raw_strength is None else (raw_strength + 1.0) / 2.0),
            **trace_fields(brand_rows),
        }
        output.append(result)
    return output


def build_recommendation_intelligence(
    rows: list[dict[str, Any]],
    attempt_facts: list[dict[str, Any]] | None = None,
    *,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    return aggregate_recommendations(rows, attempt_facts, min_confidence=min_confidence)


def _row_priority(row: dict[str, Any]) -> tuple[int, int, float]:
    recommendation_type = str(row.get("recommendation_type") or classify_recommendation_type(row))
    rank = positive_int(row.get("rank_position"))
    confidence = float(row.get("confidence") or 0.0)
    return RECOMMENDATION_PRIORITY[recommendation_type], -(rank or 999999), confidence
