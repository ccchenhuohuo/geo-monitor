"""Macro-by-query situation intelligence for seed/persona/intent/scenario."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .common import as_number, mean, parse_metadata, positive_int, response_key, safe_div, score100, trace_fields
from .recommendation import POSITIVE_TYPES, strongest_attempt_brand_rows

DEFAULT_DIMENSIONS = ("seed_id", "persona", "intent", "scenario")


def aggregate_situations(
    brand_attempt_facts: list[dict[str, Any]],
    query_facts: list[dict[str, Any]],
    target_brand: str,
    *,
    dimensions: tuple[str, ...] = DEFAULT_DIMENSIONS,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    """Aggregate target performance with equal weight per query."""

    target_key = _brand_key(target_brand)
    core = strongest_attempt_brand_rows(brand_attempt_facts, min_confidence=min_confidence)
    target_rows_by_query: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in core:
        brand = str(row.get("brand_name_canonical") or row.get("brand_name_raw") or "")
        if _brand_key(brand) == target_key:
            target_rows_by_query[_query_key(row)].append(row)

    query_records: list[dict[str, Any]] = []
    for source in query_facts:
        row = dict(source)
        metadata = parse_metadata(row)
        if not metadata.get("scenario"):
            metadata["scenario"] = metadata.get("template_id", "")
        planned = int(as_number(row.get("planned_attempts")) or 0)
        eligible = int(as_number(row.get("stats_included_attempts") or row.get("eligible_attempts")) or 0)
        target_rows = target_rows_by_query.get(_query_key(row), [])
        mentions = len({response_key(item) for item in target_rows})
        recommendations = sum(1 for item in target_rows if str(item["recommendation_type"]) in POSITIVE_TYPES)
        top_picks = sum(1 for item in target_rows if item["recommendation_type"] == "top_pick")
        ranks = [positive_int(item.get("rank_position")) for item in target_rows]
        query_records.append(
            {
                **row,
                "_metadata": metadata,
                "_planned": planned,
                "_eligible": eligible,
                "_mentions": mentions,
                "_recommendations": recommendations,
                "_top_picks": top_picks,
                "_ranks": [rank for rank in ranks if rank is not None],
                "_target_rows": target_rows,
                "_visibility": safe_div(mentions, eligible),
            }
        )

    overall_by_job: dict[str, float | None] = {}
    for job_id in sorted({str(row.get("job_id") or row.get("run_id") or "") for row in query_records}):
        overall_by_job[job_id] = mean(row["_visibility"] for row in query_records if str(row.get("job_id") or row.get("run_id") or "") == job_id)

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in query_records:
        job_id = str(row.get("job_id") or row.get("run_id") or "")
        for dimension in dimensions:
            value = str(row["_metadata"].get(dimension) or "")
            grouped[(job_id, dimension, value)].append(row)

    output: list[dict[str, Any]] = []
    for (job_id, dimension, value), rows in sorted(grouped.items()):
        planned = sum(row["_planned"] for row in rows)
        eligible = sum(row["_eligible"] for row in rows)
        mentions = sum(row["_mentions"] for row in rows)
        recommendations = sum(row["_recommendations"] for row in rows)
        top_picks = sum(row["_top_picks"] for row in rows)
        macro_visibility = mean(row["_visibility"] for row in rows)
        overall = overall_by_job[job_id]
        quality = safe_div(eligible, planned)
        target_rows = [item for row in rows for item in row["_target_rows"]]
        ranks = [rank for row in rows for rank in row["_ranks"]]
        output.append(
            {
                "job_id": job_id,
                "target_brand": target_brand,
                "segment_dimension": dimension,
                "segment_value": value,
                "query_count": len(rows),
                "planned_attempts": planned,
                "eligible_attempts": eligible,
                "sample_completeness": safe_div(eligible, planned),
                "target_mention_attempts": mentions,
                "visibility_rate_macro_by_query": macro_visibility,
                "visibility_rate_micro": safe_div(mentions, eligible),
                "recommendation_rate_when_mentioned": safe_div(recommendations, mentions),
                "top_pick_rate_when_mentioned": safe_div(top_picks, mentions),
                "avg_rank_position": mean(ranks),
                "quality_score": score100(quality),
                "overall_visibility_rate_macro": overall,
                "persona_gap": (overall - macro_visibility) if dimension == "persona" and overall is not None and macro_visibility is not None else None,
                "persona_visibility_index": safe_div(macro_visibility, overall)
                if dimension == "persona" and macro_visibility is not None and overall is not None
                else None,
                "situation_gap_score": score100(max(0.0, overall - macro_visibility) * (quality or 0.0))
                if overall is not None and macro_visibility is not None
                else None,
                **trace_fields(target_rows),
                "trace_query_ids": sorted({str(row.get("query_id") or "") for row in rows if row.get("query_id")}),
            }
        )
    return output


def build_situation_intelligence(
    brand_attempt_facts: list[dict[str, Any]],
    query_facts: list[dict[str, Any]],
    target_brand: str,
    *,
    dimensions: tuple[str, ...] = DEFAULT_DIMENSIONS,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    return aggregate_situations(
        brand_attempt_facts,
        query_facts,
        target_brand,
        dimensions=dimensions,
        min_confidence=min_confidence,
    )


def _query_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("job_id") or row.get("run_id") or ""), str(row.get("query_id") or "")


def _brand_key(value: str) -> str:
    return "".join(value.casefold().split())
