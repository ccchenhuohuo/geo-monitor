"""Compose the pure intelligence primitives into stable pipeline tables."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .citation import aggregate_citations, compute_source_gaps, summarize_citations
from .common import as_bool, as_ratio, mean, parse_metadata, response_key, safe_div, trace_fields
from .competitor import compute_competitor_intelligence
from .opportunities import (
    build_messaging_opportunities,
    build_persona_opportunities,
    build_query_opportunities,
    build_source_opportunities,
)
from .overview import compute_overview_scores
from .perception import aggregate_perception
from .recommendation import RECOMMENDATION_WEIGHTS, aggregate_recommendations, strongest_attempt_brand_rows
from .situation import aggregate_situations
from .trends import compute_presence_volatility, compute_run_deltas, compute_topk_drift, compute_volatility

INTELLIGENCE_TABLE_NAMES = (
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

INTELLIGENCE_BASE_FIELDS: dict[str, list[str]] = {
    "geo_overview_scores": [
        "job_id",
        "brand_name_canonical",
        "visibility_score",
        "recommendation_score",
        "competitor_score",
        "source_score",
        "quality_score",
        "completed_at",
    ],
    "visibility_summary": ["job_id", "brand_name_canonical", "response_mention_rate", "query_coverage_rate", "sov_event_share", "eligible_attempts"],
    "recommendations": ["job_id", "query_id", "repeat_index", "attempt_id", "brand_name_canonical", "recommendation_type", "evidence", "confidence"],
    "recommendation_summary": [
        "job_id",
        "brand_name_canonical",
        "eligible_attempts",
        "recommendation_denominator",
        "recommendation_conversion",
        "weighted_recommendation_score",
    ],
    "recommendation_by_persona": ["job_id", "persona", "brand_name_canonical", "eligible_attempts", "recommendation_denominator", "recommendation_conversion"],
    "competitor_edges": ["job_id", "target_brand", "competitor_brand", "eligible_attempts", "co_occurrence_count", "target_win_rate", "replacement_risk"],
    "competitor_win_loss": ["job_id", "target_brand", "competitor_brand", "target_wins", "competitor_wins", "ties", "win_loss_denominator"],
    "competitor_replacements": ["job_id", "target_brand", "competitor_brand", "replacement_count", "replacement_denominator", "replacement_risk"],
    "rank_gap": ["job_id", "target_brand", "competitor_brand", "avg_rank_gap", "rank_gap_observed_count", "rank_advantage_score"],
    "source_types": ["job_id", "source_type", "citation_occurrences", "distinct_source_url_count", "eligible_attempts", "response_count"],
    "brand_source_domains": ["job_id", "brand_name_canonical", "domain", "source_type", "citation_occurrences", "distinct_source_url_count"],
    "brand_source_urls": ["job_id", "brand_name_canonical", "canonical_url", "domain", "source_type", "source_attribution_method", "citation_occurrences"],
    "source_gaps": ["job_id", "target_brand", "competitor_brand", "canonical_url", "domain", "source_type", "source_gap_rate"],
    "visibility_by_seed": [
        "job_id",
        "target_brand",
        "segment_dimension",
        "segment_value",
        "query_count",
        "eligible_attempts",
        "visibility_rate_macro_by_query",
        "visibility_rate_micro",
    ],
    "visibility_by_persona": [
        "job_id",
        "target_brand",
        "segment_dimension",
        "segment_value",
        "query_count",
        "eligible_attempts",
        "visibility_rate_macro_by_query",
        "visibility_rate_micro",
    ],
    "visibility_by_intent": [
        "job_id",
        "target_brand",
        "segment_dimension",
        "segment_value",
        "query_count",
        "eligible_attempts",
        "visibility_rate_macro_by_query",
        "visibility_rate_micro",
    ],
    "visibility_by_scenario": [
        "job_id",
        "target_brand",
        "segment_dimension",
        "segment_value",
        "query_count",
        "eligible_attempts",
        "visibility_rate_macro_by_query",
        "visibility_rate_micro",
    ],
    "perception_claims": [
        "job_id",
        "brand_name_canonical",
        "claim_type",
        "claim_canonical",
        "representative_claim_text",
        "eligible_attempts",
        "response_rate",
        "avg_confidence",
    ],
    "perception_strengths": [
        "job_id",
        "brand_name_canonical",
        "claim_type",
        "claim_canonical",
        "representative_claim_text",
        "eligible_attempts",
        "response_rate",
        "avg_confidence",
    ],
    "perception_weaknesses": [
        "job_id",
        "brand_name_canonical",
        "claim_type",
        "claim_canonical",
        "representative_claim_text",
        "eligible_attempts",
        "response_rate",
        "avg_confidence",
    ],
    "perception_pricing": [
        "job_id",
        "brand_name_canonical",
        "claim_type",
        "claim_canonical",
        "representative_claim_text",
        "eligible_attempts",
        "response_rate",
        "avg_confidence",
    ],
    "perception_audience_fit": [
        "job_id",
        "brand_name_canonical",
        "claim_type",
        "claim_canonical",
        "representative_claim_text",
        "eligible_attempts",
        "response_rate",
        "avg_confidence",
    ],
    "trend_deltas": [
        "brand_name_canonical",
        "metric",
        "baseline_run_id",
        "current_run_id",
        "baseline_value",
        "current_value",
        "absolute_delta",
        "relative_delta",
    ],
    "trend_drift": ["drift_type", "baseline_run_id", "current_run_id", "intersection_count", "union_count", "jaccard_similarity", "jaccard_distance"],
    "trend_volatility": ["brand_name_canonical", "metric", "run_count", "observation_count", "within_run_volatility", "between_run_volatility"],
    "opportunity_query_gaps": ["opportunity_type", "job_id", "query_id", "query", "competitor_brand", "opportunity_score"],
    "opportunity_persona_gaps": ["opportunity_type", "job_id", "persona", "competitor_brand", "opportunity_score"],
    "opportunity_source_gaps": ["opportunity_type", "job_id", "competitor_brand", "domain", "canonical_url", "opportunity_score"],
    "opportunity_messaging_gaps": ["opportunity_type", "job_id", "persona", "claim_canonical", "competitor_brand", "opportunity_score"],
}


def build_intelligence_outputs(
    *,
    manifest: dict[str, Any],
    mentions: list[dict[str, Any]],
    success_records: list[dict[str, Any]],
    facts: dict[str, list[dict[str, Any]]],
    brand_summary: list[dict[str, Any]],
    history_overview: list[dict[str, Any]] | None = None,
    history_visibility: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build every Issue #2 table from the current run's traceable facts."""

    job_id = str(manifest.get("job_id") or "")
    target_brand = str(manifest.get("target_brand") or "")
    attempt_facts = [dict(row) for row in facts.get("attempt_facts", [])]
    query_facts = [dict(row) for row in facts.get("query_facts", [])]
    brand_facts = [dict(row) for row in facts.get("brand_attempt_facts", [])]
    query_metadata = {(str(row.get("job_id") or job_id), str(row.get("query_id") or "")): parse_metadata(row) for row in query_facts}

    recommendations = _with_job_id(strongest_attempt_brand_rows(brand_facts), job_id)
    recommendation_summary = _with_job_id(
        aggregate_recommendations(brand_facts, attempt_facts),
        job_id,
    )
    recommendation_by_persona = _recommendation_by_persona(
        brand_facts,
        attempt_facts,
        query_metadata,
        job_id,
    )

    competitor_edges = _with_job_id(
        compute_competitor_intelligence(brand_facts, target_brand, attempt_facts),
        job_id,
    )
    competitor_win_loss = [dict(row) for row in competitor_edges]
    competitor_replacements = [dict(row) for row in competitor_edges if int(row.get("replacement_count") or 0) > 0]
    rank_gap = [dict(row) for row in competitor_edges if int(row.get("rank_gap_observed_count") or 0) > 0]

    source_events = _flatten_sources(success_records, attempt_facts, job_id)
    owned_domains = {str(value).strip().lower() for value in manifest.get("owned_domains", []) if str(value).strip()}
    brand_source_urls = _with_job_id(
        aggregate_citations(source_events, brand_facts, owned_domains=owned_domains),
        job_id,
    )
    unattributed_source_urls = aggregate_citations(source_events, owned_domains=owned_domains)
    citation_summary = _with_job_id(
        summarize_citations(
            brand_source_urls,
            attempt_facts,
            brand_facts,
            owned_domains_configured=bool(owned_domains),
        ),
        job_id,
    )
    brand_source_domains = _aggregate_brand_source_domains(brand_source_urls, job_id)
    source_types = _aggregate_source_types(unattributed_source_urls, attempt_facts, job_id)
    source_gaps = _with_job_id(compute_source_gaps(brand_source_urls, target_brand), job_id)

    situations = aggregate_situations(brand_facts, query_facts, target_brand)
    visibility_by_dimension = {
        dimension: [dict(row) for row in situations if row.get("segment_dimension") == dimension] for dimension in ("seed_id", "persona", "intent", "scenario")
    }

    perception_records = _flatten_perception(mentions, brand_facts, job_id)
    perception_all = _with_job_id(
        aggregate_perception(perception_records, attempt_facts),
        job_id,
    )
    perception_tables = {
        "perception_claims": [row for row in perception_all if row.get("claim_type") == "claim"],
        "perception_strengths": [row for row in perception_all if row.get("claim_type") == "strength"],
        "perception_weaknesses": [row for row in perception_all if row.get("claim_type") == "weakness"],
        "perception_pricing": [row for row in perception_all if row.get("claim_type") == "pricing"],
        "perception_audience_fit": [row for row in perception_all if row.get("claim_type") in {"audience_fit", "persona_alignment"}],
    }

    visibility_summary = _visibility_rows(
        manifest,
        brand_summary,
        facts,
        recommendation_summary,
        citation_summary,
        competitor_edges,
    )
    overview_scores = compute_overview_scores(visibility_summary)
    completed_at = str(manifest.get("last_run_completed_at") or manifest.get("last_run_started_at") or manifest.get("created_at") or "")
    for row in overview_scores:
        row["completed_at"] = completed_at
    current_visibility = [dict(row, completed_at=completed_at) for row in visibility_summary]

    overview_history = [*(history_overview or []), *overview_scores]
    visibility_history_rows = [*(history_visibility or []), *current_visibility]
    trend_deltas = [row for row in compute_run_deltas(overview_history) if str(row.get("current_run_id") or "") == job_id]
    trend_drift = [row for row in compute_topk_drift(visibility_history_rows) if str(row.get("current_run_id") or "") == job_id]
    trend_volatility = []
    for metric in (
        "visibility_score",
        "recommendation_score",
        "competitor_score",
        "source_score",
        "quality_score",
    ):
        trend_volatility.extend(compute_volatility(overview_history, metric_field=metric))
    trend_volatility.extend(
        compute_presence_volatility(
            attempt_facts,
            brand_facts,
            brands={str(row.get("brand_name_canonical") or "") for row in overview_scores if str(row.get("brand_name_canonical") or "")},
        )
    )

    opportunity_query = build_query_opportunities(_query_opportunity_inputs(query_facts, brand_facts, target_brand))
    opportunity_persona = build_persona_opportunities(
        _persona_opportunity_inputs(
            visibility_by_dimension["persona"],
            brand_facts,
            query_metadata,
            target_brand,
        )
    )
    opportunity_source = build_source_opportunities([dict(row, job_id=job_id, source_quality_score=_overall_quality(facts)) for row in source_gaps])
    opportunity_messaging = build_messaging_opportunities(
        _messaging_opportunity_inputs(
            perception_all,
            target_brand,
            _overall_quality(facts),
            query_metadata,
            job_id,
        )
    )

    outputs = {
        "geo_overview_scores": overview_scores,
        "visibility_summary": visibility_summary,
        "recommendations": recommendations,
        "recommendation_summary": recommendation_summary,
        "recommendation_by_persona": recommendation_by_persona,
        "competitor_edges": competitor_edges,
        "competitor_win_loss": competitor_win_loss,
        "competitor_replacements": competitor_replacements,
        "rank_gap": rank_gap,
        "source_types": source_types,
        "brand_source_domains": brand_source_domains,
        "brand_source_urls": brand_source_urls,
        "source_gaps": source_gaps,
        "visibility_by_seed": visibility_by_dimension["seed_id"],
        "visibility_by_persona": visibility_by_dimension["persona"],
        "visibility_by_intent": visibility_by_dimension["intent"],
        "visibility_by_scenario": visibility_by_dimension["scenario"],
        **perception_tables,
        "trend_deltas": trend_deltas,
        "trend_drift": trend_drift,
        "trend_volatility": trend_volatility,
        "opportunity_query_gaps": opportunity_query,
        "opportunity_persona_gaps": opportunity_persona,
        "opportunity_source_gaps": opportunity_source,
        "opportunity_messaging_gaps": opportunity_messaging,
    }
    if set(outputs) != set(INTELLIGENCE_TABLE_NAMES):
        raise RuntimeError("intelligence output contract drift")
    return outputs


def _with_job_id(rows: list[dict[str, Any]], job_id: str) -> list[dict[str, Any]]:
    return [dict(row, job_id=str(row.get("job_id") or job_id)) for row in rows]


def _recommendation_by_persona(
    brand_facts: list[dict[str, Any]],
    attempt_facts: list[dict[str, Any]],
    query_metadata: dict[tuple[str, str], dict[str, Any]],
    job_id: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in brand_facts:
        key = (str(row.get("job_id") or job_id), str(row.get("query_id") or ""))
        persona = str(query_metadata.get(key, {}).get("persona") or "")
        grouped[persona].append(row)
    output: list[dict[str, Any]] = []
    for persona, rows in sorted(grouped.items()):
        query_ids = {str(row.get("query_id") or "") for row in rows}
        attempts = [row for row in attempt_facts if str(row.get("query_id") or "") in query_ids]
        for aggregate in aggregate_recommendations(rows, attempts):
            output.append(dict(aggregate, job_id=job_id, persona=persona, query_count=len(query_ids)))
    return output


def _flatten_sources(
    records: list[dict[str, Any]],
    attempt_facts: list[dict[str, Any]],
    job_id: str,
) -> list[dict[str, Any]]:
    attempt_by_key = {(str(row.get("query_id") or ""), int(row.get("repeat_index") or 1)): row for row in attempt_facts}
    output: list[dict[str, Any]] = []
    for record in records:
        query_id = str(record.get("query_id") or "")
        repeat_index = int(record.get("repeat_index") or 1)
        fact = attempt_by_key.get((query_id, repeat_index), {})
        for source_index, source in enumerate(record.get("sources") or [], start=1):
            if not isinstance(source, dict):
                continue
            raw = source.get("raw") if isinstance(source.get("raw"), dict) else {}
            attribution = {
                field: source.get(field) if source.get(field) not in (None, "") else raw.get(field)
                for field in ("attributed_brand", "anchor_brand", "anchor_brands", "brand_name_canonical")
                if source.get(field) not in (None, "") or raw.get(field) not in (None, "")
            }
            output.append(
                {
                    **source,
                    **attribution,
                    "job_id": job_id,
                    "query_id": query_id,
                    "repeat_index": repeat_index,
                    "attempt_id": fact.get("attempt_id") or record.get("attempt_id") or "",
                    "source_index": source_index,
                    "stats_included": fact.get("stats_included", 1),
                }
            )
    return output


def _aggregate_brand_source_domains(rows: list[dict[str, Any]], job_id: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("brand_name_canonical") or ""), str(row.get("domain") or ""))].append(row)
    output = []
    for (brand, domain), items in sorted(grouped.items()):
        output.append(
            {
                "job_id": job_id,
                "brand_name_canonical": brand,
                "domain": domain,
                "source_type": " | ".join(sorted({str(row.get("source_type") or "unknown") for row in items})),
                "citation_occurrences": sum(int(row.get("citation_occurrences") or 0) for row in items),
                "distinct_source_url_count": len({str(row.get("canonical_url") or "") for row in items}),
                "owned_source": int(any(as_bool(row.get("owned_source")) for row in items)),
                **_merged_trace_fields(items),
            }
        )
    return output


def _aggregate_source_types(
    rows: list[dict[str, Any]],
    attempt_facts: list[dict[str, Any]],
    job_id: str,
) -> list[dict[str, Any]]:
    eligible = len({response_key(row) for row in attempt_facts if as_bool(row.get("stats_included"), default=True)})
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("source_type") or "unknown")].append(row)
    return [
        {
            "job_id": job_id,
            "source_type": source_type,
            "citation_occurrences": sum(int(row.get("citation_occurrences") or 0) for row in items),
            "distinct_source_url_count": len({str(row.get("canonical_url") or "") for row in items}),
            "eligible_attempts": eligible,
            "response_count": len({tuple(key) for row in items for key in (row.get("trace_response_keys") or [])}),
            **_merged_trace_fields(items),
        }
        for source_type, items in sorted(grouped.items())
    ]


def _merged_trace_fields(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
    merged = trace_fields(rows)
    for field in ("trace_job_ids", "trace_query_ids", "trace_attempt_ids", "trace_repeat_indices"):
        nested = {value for row in rows for value in (row.get(field) or []) if value not in (None, "")}
        merged[field] = sorted(set(merged[field]) | nested, key=str)
    return merged


def _flatten_perception(
    mentions: list[dict[str, Any]],
    brand_facts: list[dict[str, Any]],
    job_id: str,
) -> list[dict[str, Any]]:
    facts = {
        (
            str(row.get("query_id") or ""),
            int(row.get("repeat_index") or 1),
            str(row.get("brand_name_canonical") or ""),
        ): row
        for row in brand_facts
    }
    output: list[dict[str, Any]] = []
    for mention in mentions:
        key = (
            str(mention.get("query_id") or ""),
            int(mention.get("repeat_index") or 1),
            str(mention.get("brand_name_canonical") or mention.get("brand_name_raw") or ""),
        )
        fact = facts.get(key, {})
        for perception in mention.get("perception") or []:
            if not isinstance(perception, dict):
                continue
            output.append(
                {
                    **perception,
                    "job_id": job_id,
                    "query_id": key[0],
                    "repeat_index": key[1],
                    "brand_name_canonical": key[2],
                    "attempt_id": fact.get("attempt_id") or "",
                    "stats_included": fact.get("stats_included", 0),
                    "traceability_status": "valid",
                }
            )
    return output


def _visibility_rows(
    manifest: dict[str, Any],
    brand_summary: list[dict[str, Any]],
    facts: dict[str, list[dict[str, Any]]],
    recommendation_summary: list[dict[str, Any]],
    citation_summary: list[dict[str, Any]],
    competitor_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    job_id = str(manifest.get("job_id") or "")
    target = str(manifest.get("target_brand") or "")
    recommendation = {str(row.get("brand_name_canonical") or ""): row for row in recommendation_summary}
    citation = {str(row.get("brand_name_canonical") or ""): row for row in citation_summary}
    queries = facts.get("query_facts", [])
    planned = sum(int(row.get("planned_attempts") or 0) for row in queries)
    terminal = sum(int(row.get("latest_terminal_attempts", row.get("completed_attempts")) or 0) for row in queries)
    completed = sum(int(row.get("completed_attempts") or 0) for row in queries)
    valid = sum(int(row.get("valid_attempts", row.get("completed_attempts")) or 0) for row in queries)
    eligible = sum(int(row.get("stats_included_attempts") or 0) for row in queries)
    extraction_error = (facts.get("quality_summary") or [{}])[0].get("extraction_error_rate")
    target_edges = [row for row in competitor_edges if str(row.get("target_brand") or "") == target]
    decisive = sum(int(row.get("win_loss_denominator") or 0) for row in target_edges)
    target_wins = sum(int(row.get("target_wins") or 0) for row in target_edges)
    replacement_denominator = sum(int(row.get("replacement_denominator") or 0) for row in target_edges)
    replacements = sum(int(row.get("replacement_count") or 0) for row in target_edges)
    output = []
    summary_rows = [dict(row) for row in brand_summary]
    if target and not any(_brand_key(str(row.get("brand_name_canonical") or "")) == _brand_key(target) for row in summary_rows):
        summary_rows.append(
            {
                "brand_name_canonical": target,
                "is_target_brand": 1,
                "target_brand_detected": 0,
                "responses_mentioned": 0,
                "response_mention_rate": "0.0%",
                "query_coverage_count": 0,
                "query_coverage_rate": "0.0%",
                "query_macro_mention_rate": "0.0%",
                "sov_response_share": "0.0%",
                "sov_event_share": "0.0%",
                "top3_rate": "0.0%",
                "avg_rank_position": "",
                "sentiment_observed_rate": None,
            }
        )
    for source in summary_rows:
        brand = str(source.get("brand_name_canonical") or "")
        recommendation_row = recommendation.get(brand, {})
        citation_row = citation.get(brand, {})
        row = {
            **source,
            "job_id": job_id,
            "planned_attempts": planned,
            "completed_attempts": completed,
            "valid_attempts": valid,
            "eligible_attempts": eligible,
            "stats_included_attempts": eligible,
            "sample_completeness": safe_div(terminal, planned),
            "usable_sample_rate": safe_div(eligible, planned),
            "extraction_error_rate": extraction_error,
            **recommendation_row,
            **citation_row,
            **_merged_trace_fields([source, recommendation_row, citation_row]),
        }
        if _brand_key(brand) == _brand_key(target):
            row["target_win_rate"] = safe_div(target_wins, decisive)
            row["replacement_risk"] = safe_div(replacements, replacement_denominator)
        output.append(row)
    return output


def _query_opportunity_inputs(
    query_facts: list[dict[str, Any]],
    brand_facts: list[dict[str, Any]],
    target_brand: str,
) -> list[dict[str, Any]]:
    target_key = _brand_key(target_brand)
    by_query_brand: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in brand_facts:
        brand = str(row.get("brand_name_canonical") or "")
        by_query_brand[(str(row.get("job_id") or ""), str(row.get("query_id") or ""), brand)].append(row)
    output = []
    for query in query_facts:
        job_id = str(query.get("job_id") or "")
        query_id = str(query.get("query_id") or "")
        eligible = int(query.get("stats_included_attempts") or 0)
        target_mentions = sum(
            len(rows)
            for (row_job, row_query, brand), rows in by_query_brand.items()
            if row_job == job_id and row_query == query_id and _brand_key(brand) == target_key
        )
        target_visibility = safe_div(target_mentions, eligible) or 0.0
        for (row_job, row_query, brand), rows in sorted(by_query_brand.items()):
            if row_job != job_id or row_query != query_id or _brand_key(brand) == target_key:
                continue
            competitor_visibility = safe_div(len(rows), eligible)
            strengths = [RECOMMENDATION_WEIGHTS.get(str(row.get("recommendation_type") or "mentioned_only"), 0.0) for row in rows]
            strength = mean([(value + 1.0) / 2.0 for value in strengths])
            output.append(
                {
                    "job_id": job_id,
                    "query_id": query_id,
                    "query": query.get("query") or "",
                    "competitor_brand": brand,
                    "competitor_visibility": max(0.0, (competitor_visibility or 0.0) - target_visibility),
                    "competitor_recommendation_strength": strength,
                    "query_quality_score": safe_div(eligible, int(query.get("planned_attempts") or 0)),
                    **trace_fields(rows),
                }
            )
    return output


def _persona_opportunity_inputs(
    persona_rows: list[dict[str, Any]],
    brand_facts: list[dict[str, Any]],
    query_metadata: dict[tuple[str, str], dict[str, Any]],
    target_brand: str,
) -> list[dict[str, Any]]:
    target_key = _brand_key(target_brand)
    output: list[dict[str, Any]] = []
    for segment in persona_rows:
        job_id = str(segment.get("job_id") or "")
        persona = str(segment.get("segment_value") or "")
        query_ids = {
            query_id for (row_job, query_id), metadata in query_metadata.items() if row_job == job_id and str(metadata.get("persona") or "") == persona
        }
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fact in brand_facts:
            brand = str(fact.get("brand_name_canonical") or "")
            if str(fact.get("job_id") or "") == job_id and str(fact.get("query_id") or "") in query_ids and _brand_key(brand) != target_key:
                grouped[brand].append(fact)
        eligible = int(segment.get("eligible_attempts") or 0)
        for competitor, rows in sorted(grouped.items()):
            visibility = safe_div(len({response_key(row) for row in rows}), eligible)
            recommendation = mean([(RECOMMENDATION_WEIGHTS.get(str(row.get("recommendation_type") or "mentioned_only"), 0.0) + 1.0) / 2.0 for row in rows])
            output.append(
                {
                    **segment,
                    "persona": persona,
                    "competitor_brand": competitor,
                    "competitor_strength": mean([visibility, recommendation]),
                    "persona_quality_score": as_ratio(segment.get("quality_score")),
                    **trace_fields(rows),
                }
            )
    return output


def _messaging_opportunity_inputs(
    perception_rows: list[dict[str, Any]],
    target_brand: str,
    quality: float | None,
    query_metadata: dict[tuple[str, str], dict[str, Any]],
    job_id: str,
) -> list[dict[str, Any]]:
    def personas_for(row: dict[str, Any]) -> set[str]:
        return {str(query_metadata.get((job_id, str(query_id)), {}).get("persona") or "") for query_id in (row.get("trace_query_ids") or [])} or {""}

    target_claims = {
        (str(row.get("claim_canonical") or ""), persona)
        for row in perception_rows
        if _brand_key(str(row.get("brand_name_canonical") or "")) == _brand_key(target_brand)
        for persona in personas_for(row)
    }
    output = []
    for row in perception_rows:
        brand = str(row.get("brand_name_canonical") or "")
        claim = str(row.get("claim_canonical") or "")
        if _brand_key(brand) == _brand_key(target_brand):
            continue
        for persona in personas_for(row):
            if (claim, persona) in target_claims:
                continue
            output.append(
                {
                    **row,
                    "competitor_brand": brand,
                    "persona": persona,
                    "competitor_claim_strength": row.get("response_rate") or row.get("avg_confidence"),
                    "audience_relevance": 1.0 if row.get("claim_type") in {"audience_fit", "persona_alignment"} else 0.7,
                    "message_quality_score": quality,
                }
            )
    return output


def _overall_quality(facts: dict[str, list[dict[str, Any]]]) -> float | None:
    rows = facts.get("query_facts", [])
    planned = sum(int(row.get("planned_attempts") or 0) for row in rows)
    eligible = sum(int(row.get("stats_included_attempts") or 0) for row in rows)
    return safe_div(eligible, planned)


def _brand_key(value: str) -> str:
    return "".join(value.casefold().split())
