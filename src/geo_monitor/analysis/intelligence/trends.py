"""Run deltas, top-k drift, and volatility."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from .common import as_bool, as_number, mean, response_key, trace_fields

DEFAULT_SCORE_FIELDS = (
    "visibility_score",
    "recommendation_score",
    "competitor_score",
    "source_score",
    "quality_score",
)


def compute_run_deltas(
    rows: list[dict[str, Any]],
    *,
    metric_fields: tuple[str, ...] = DEFAULT_SCORE_FIELDS,
    entity_fields: tuple[str, ...] = ("brand_name_canonical",),
    run_field: str = "job_id",
    order_field: str = "completed_at",
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(str(row.get(field) or "") for field in entity_fields)].append(dict(row))
    output: list[dict[str, Any]] = []
    for entity, entity_rows in sorted(grouped.items()):
        ordered = sorted(entity_rows, key=lambda row: (str(row.get(order_field) or ""), str(row.get(run_field) or "")))
        for previous, current in zip(ordered, ordered[1:]):
            for metric in metric_fields:
                before = as_number(previous.get(metric))
                after = as_number(current.get(metric))
                delta = None if before is None or after is None else after - before
                output.append(
                    {
                        **{field: value for field, value in zip(entity_fields, entity)},
                        "metric": metric,
                        "baseline_run_id": previous.get(run_field) or "",
                        "current_run_id": current.get(run_field) or "",
                        "baseline_value": before,
                        "current_value": after,
                        "absolute_delta": None if delta is None else round(delta, 6),
                        "relative_delta": None if delta is None or before == 0 else round(delta / abs(before), 6),
                        "delta_denominator": before,
                        "trace_run_ids": [previous.get(run_field) or "", current.get(run_field) or ""],
                    }
                )
    return output


def compute_topk_drift(
    rows: list[dict[str, Any]],
    *,
    item_field: str = "brand_name_canonical",
    value_field: str = "sov_response_share",
    k: int = 10,
    run_field: str = "job_id",
    order_field: str = "completed_at",
) -> list[dict[str, Any]]:
    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_run[str(row.get(run_field) or "")].append(dict(row))
    run_order = sorted(
        by_run,
        key=lambda run_id: (min(str(row.get(order_field) or "") for row in by_run[run_id]), run_id),
    )
    top_items: dict[str, list[str]] = {}
    for run_id in run_order:
        ranked = sorted(
            (row for row in by_run[run_id] if row.get(item_field) and as_number(row.get(value_field)) is not None),
            key=lambda row: (-float(as_number(row.get(value_field)) or 0.0), str(row.get(item_field))),
        )
        top_items[run_id] = [str(row[item_field]) for row in ranked[:k]]
    output = []
    for previous, current in zip(run_order, run_order[1:]):
        before = set(top_items[previous])
        after = set(top_items[current])
        union = before | after
        similarity = len(before & after) / len(union) if union else 1.0
        output.append(
            {
                "drift_type": f"top_{k}_{item_field}",
                "baseline_run_id": previous,
                "current_run_id": current,
                "top_k_baseline": top_items[previous],
                "top_k_current": top_items[current],
                "intersection_count": len(before & after),
                "union_count": len(union),
                "jaccard_similarity": similarity,
                "jaccard_distance": 1.0 - similarity,
                "trace_run_ids": [previous, current],
            }
        )
    return output


def compute_volatility(
    rows: list[dict[str, Any]],
    *,
    metric_field: str,
    entity_fields: tuple[str, ...] = ("brand_name_canonical",),
    run_field: str = "job_id",
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(str(row.get(field) or "") for field in entity_fields)].append(dict(row))
    output = []
    for entity, entity_rows in sorted(grouped.items()):
        by_run: dict[str, list[float]] = defaultdict(list)
        for row in entity_rows:
            value = as_number(row.get(metric_field))
            if value is not None:
                by_run[str(row.get(run_field) or "")].append(value)
        run_means = [float(mean(values)) for values in by_run.values() if values]
        within = [statistics.pstdev(values) for values in by_run.values() if len(values) >= 2]
        output.append(
            {
                **{field: value for field, value in zip(entity_fields, entity)},
                "metric": metric_field,
                "run_count": len(by_run),
                "observation_count": sum(len(values) for values in by_run.values()),
                "within_run_volatility": mean(within),
                "within_run_denominator": len(within),
                "between_run_volatility": statistics.pstdev(run_means) if len(run_means) >= 2 else None,
                "between_run_denominator": len(run_means),
                "trace_run_ids": sorted(by_run),
            }
        )
    return output


def compute_presence_volatility(
    attempt_rows: list[dict[str, Any]],
    brand_rows: list[dict[str, Any]],
    *,
    brands: list[str] | tuple[str, ...] | set[str] | None = None,
) -> list[dict[str, Any]]:
    """Compute within-run volatility from eligible attempt-level 0/1 presence.

    Brand facts only exist for mentions, so the eligible attempt universe is
    taken from ``attempt_rows`` and missing brand facts become explicit zeroes.
    """

    eligible_by_run: dict[str, dict[tuple[str, str, int | str], dict[str, Any]]] = defaultdict(dict)
    for row in attempt_rows:
        if not as_bool(row.get("stats_included")):
            continue
        run_id = str(row.get("job_id") or row.get("run_id") or "")
        eligible_by_run[run_id][response_key(row)] = dict(row)

    observed_brands = {str(row.get("brand_name_canonical") or "").strip() for row in brand_rows if str(row.get("brand_name_canonical") or "").strip()}
    requested_brands = {str(value).strip() for value in (brands or []) if str(value).strip()}
    brand_names = sorted(observed_brands | requested_brands)
    present_keys: dict[str, set[tuple[str, str, int | str]]] = defaultdict(set)
    confidence_by_brand: dict[str, list[float]] = defaultdict(list)
    for row in brand_rows:
        brand = str(row.get("brand_name_canonical") or "").strip()
        key = response_key(row)
        run_id = key[0]
        if not brand or key not in eligible_by_run.get(run_id, {}):
            continue
        if "sov_eligible" in row and not as_bool(row.get("sov_eligible")):
            continue
        present_keys[brand].add(key)
        confidence = as_number(row.get("confidence"))
        if confidence is not None and 0.0 <= confidence <= 1.0:
            confidence_by_brand[brand].append(confidence)

    output: list[dict[str, Any]] = []
    for brand in brand_names:
        values_by_run: dict[str, list[float]] = {}
        trace_rows: list[dict[str, Any]] = []
        for run_id, attempts in sorted(eligible_by_run.items()):
            if not attempts:
                continue
            values_by_run[run_id] = [1.0 if key in present_keys[brand] else 0.0 for key in attempts]
            trace_rows.extend(attempts.values())
        run_means = [float(mean(values)) for values in values_by_run.values() if values]
        within = [statistics.pstdev(values) for values in values_by_run.values() if len(values) >= 2]
        output.append(
            {
                "brand_name_canonical": brand,
                "metric": "response_presence",
                "run_count": len(values_by_run),
                "observation_count": sum(len(values) for values in values_by_run.values()),
                "presence_count": len(present_keys[brand]),
                "within_run_volatility": mean(within),
                "within_run_denominator": len(within),
                "within_run_observation_count": sum(len(values) for values in values_by_run.values() if len(values) >= 2),
                "between_run_volatility": statistics.pstdev(run_means) if len(run_means) >= 2 else None,
                "between_run_denominator": len(run_means),
                "evidence_type": "eligible_attempt_presence",
                "evidence": "stats_included attempt presence encoded as 1/0",
                "confidence": mean(confidence_by_brand[brand]),
                "traceability_status": "derived_from_attempt_facts",
                **trace_fields(trace_rows),
            }
        )
    return output


def build_trend_intelligence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return compute_run_deltas(rows)
