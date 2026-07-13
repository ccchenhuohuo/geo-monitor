"""Build attempt, query, brand-attempt, and quality denominator facts."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from ..brand_extraction import RECOMMENDATION_STRENGTH, normalize_brand_name
from ..exporters import canonical_request_hash, safe_result_key
from .fact_utils import (
    add_nonempty,
    as_bool,
    as_positive_int,
    dominant_sentiment,
    is_brand_sov_candidate,
    normalized_recommendation_type,
    pct,
)


def build_fact_rows(
    *,
    manifest: dict[str, Any],
    terminal_records: list[dict[str, Any]],
    stats_records: list[dict[str, Any]],
    mentions: list[dict[str, Any]],
    data_quality: dict[str, Any],
    sample_mode: str,
) -> dict[str, list[dict[str, Any]]]:
    job_id = str(manifest.get("job_id") or "")
    expected_repeats = int(manifest.get("repeats") or 1)
    stats_keys = {key for record in stats_records if (key := safe_result_key(record)) is not None}
    terminal_by_key = {key: record for record in terminal_records if (key := safe_result_key(record)) is not None}
    valid_statuses = {"mock"} if sample_mode == "mock" else {"success"}

    attempt_facts = []
    for key, record in sorted(terminal_by_key.items()):
        status = str(record.get("status") or "")
        attempt_facts.append(
            {
                "job_id": job_id,
                "query_id": key[0],
                "repeat_index": key[1],
                "latest_status": status,
                "completed_at": record.get("completed_at", ""),
                "valid_attempt": int(status in valid_statuses),
                "stats_included": int(key in stats_keys),
                "web_search_requirement_status": record.get("web_search_requirement_status", ""),
                "web_search_evidence": record.get("web_search_evidence", ""),
                "source_parse_status": record.get("source_parse_status", ""),
                "request_hash": canonical_request_hash(record) or "",
                "attempt_id": record.get("attempt_id", ""),
            }
        )

    query_facts = []
    for query in manifest.get("queries", []):
        qid = str(query.get("query_id") or "")
        query_keys = [(qid, repeat) for repeat in range(1, expected_repeats + 1)]
        latest_terminal_attempts = sum(1 for key in query_keys if key in terminal_by_key)
        completed_attempts = sum(1 for key in query_keys if str((terminal_by_key.get(key) or {}).get("status") or "") in valid_statuses)
        stats_included_attempts = sum(1 for key in query_keys if key in stats_keys)
        latest_failed_attempts = sum(1 for key in query_keys if str((terminal_by_key.get(key) or {}).get("status") or "") == "error")
        meta = {key: value for key, value in query.items() if key not in {"query_id", "query"} and value not in (None, "")}
        query_facts.append(
            {
                "job_id": job_id,
                "query_id": qid,
                "query": query.get("query", ""),
                "planned_attempts": expected_repeats,
                "latest_terminal_attempts": latest_terminal_attempts,
                "completed_attempts": completed_attempts,
                "valid_attempts": completed_attempts,
                "stats_included_attempts": stats_included_attempts,
                "latest_failed_attempts": latest_failed_attempts,
                "sample_completeness": pct(latest_terminal_attempts / (expected_repeats or 1)),
                "usable_sample_rate": pct(stats_included_attempts / (expected_repeats or 1)),
                "query_metadata_json": json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            }
        )

    target_keys = {normalize_brand_name(str(manifest.get("target_brand") or ""))}
    target_keys.update(normalize_brand_name(alias) for alias in manifest.get("target_aliases", []) if alias)
    brand_events: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in mentions:
        if not is_brand_sov_candidate(row):
            continue
        qid = str(row.get("query_id") or "")
        repeat_index = int(row.get("repeat_index") or 1)
        canonical = str(row.get("brand_name_canonical") or row.get("brand_name_raw") or "")
        key = (canonical, qid, repeat_index)
        event = brand_events.setdefault(
            key,
            {
                "job_id": job_id,
                "query_id": qid,
                "repeat_index": repeat_index,
                "brand_name_canonical": canonical,
                "raw_names": set(),
                "recommended": False,
                "recommendation_types": [],
                "rank_positions": [],
                "sentiments": Counter(),
                "confidences": [],
                "evidence": "",
                "roles": set(),
                "conditions": set(),
                "audiences": set(),
                "use_cases": set(),
                "budget_levels": set(),
                "tradeoffs": set(),
                "strongest_row": None,
                "strongest_priority": None,
                "attempt_id": str((terminal_by_key.get((qid, repeat_index)) or {}).get("attempt_id") or ""),
                "stats_included": int((qid, repeat_index) in stats_keys),
            },
        )
        event["raw_names"].add(str(row.get("brand_name_raw") or canonical))
        if as_bool(row.get("is_recommended")) or str(row.get("role") or "").lower() == "recommended":
            event["recommended"] = True
        recommendation_type = normalized_recommendation_type(row)
        event["recommendation_types"].append(recommendation_type)
        if RECOMMENDATION_STRENGTH[recommendation_type] > 0:
            event["recommended"] = True
        candidate_rank = as_positive_int(row.get("rank_position"))
        candidate_confidence = float(row["confidence"]) if isinstance(row.get("confidence"), (int, float)) else 0.0
        candidate_priority = (
            RECOMMENDATION_STRENGTH[recommendation_type],
            -(candidate_rank or 999999),
            candidate_confidence,
        )
        if event["strongest_priority"] is None or candidate_priority > event["strongest_priority"]:
            event["strongest_priority"] = candidate_priority
            event["strongest_row"] = {**row, "recommendation_type": recommendation_type}
        rank = as_positive_int(row.get("rank_position"))
        if rank is not None:
            event["rank_positions"].append(rank)
        sentiment = str(row.get("sentiment") or "unknown").lower()
        event["sentiments"][sentiment if sentiment in {"positive", "neutral", "negative", "unknown"} else "unknown"] += 1
        if isinstance(row.get("confidence"), (int, float)):
            event["confidences"].append(float(row["confidence"]))
        if not event["evidence"] and row.get("evidence"):
            event["evidence"] = str(row.get("evidence") or "")
        add_nonempty(event["roles"], row.get("role"))
        add_nonempty(event["conditions"], row.get("condition"))
        add_nonempty(event["audiences"], row.get("audience"))
        add_nonempty(event["use_cases"], row.get("use_case"))
        add_nonempty(event["budget_levels"], row.get("budget_level"))
        add_nonempty(event["tradeoffs"], row.get("tradeoff"))

    brand_attempt_facts = []
    for event in brand_events.values():
        raw_names = sorted(event["raw_names"])
        brand_keys = {normalize_brand_name(event["brand_name_canonical"]), *{normalize_brand_name(name) for name in raw_names}}
        strongest = event["strongest_row"] or {}
        recommendation_type = str(strongest.get("recommendation_type") or "mentioned_only")
        strongest_rank = as_positive_int(strongest.get("rank_position"))
        strongest_confidence = strongest.get("confidence") if isinstance(strongest.get("confidence"), (int, float)) else ""
        brand_attempt_facts.append(
            {
                "job_id": job_id,
                "query_id": event["query_id"],
                "repeat_index": event["repeat_index"],
                "attempt_id": event["attempt_id"],
                "brand_name_canonical": event["brand_name_canonical"],
                "brand_name_raw": " | ".join(raw_names),
                "is_target_brand": int(bool(target_keys & brand_keys)),
                "sov_eligible": True,
                "is_recommended": int(RECOMMENDATION_STRENGTH[recommendation_type] > 0),
                "recommendation_type": recommendation_type,
                "recommendation_strength": RECOMMENDATION_STRENGTH[recommendation_type],
                "rank_position": strongest_rank or "",
                "sentiment": dominant_sentiment(event["sentiments"]),
                "confidence": strongest_confidence,
                "evidence": str(strongest.get("evidence") or ""),
                "role": str(strongest.get("role") or ""),
                "condition": str(strongest.get("condition") or ""),
                "audience": str(strongest.get("audience") or ""),
                "use_case": str(strongest.get("use_case") or ""),
                "budget_level": str(strongest.get("budget_level") or ""),
                "tradeoff": str(strongest.get("tradeoff") or ""),
                "traceability_status": "valid",
                "stats_included": event["stats_included"],
            }
        )

    quality_summary = [
        {
            "job_id": job_id,
            "sample_mode": sample_mode,
            "conclusion_strength": data_quality.get("conclusion_strength", ""),
            "partial_sample": bool(data_quality.get("partial_sample")),
            "planned_units": data_quality.get("planned_units", 0),
            "analysis_record_count": data_quality.get("analysis_record_count", 0),
            "stats_record_count": data_quality.get("stats_record_count", 0),
            "missing_unit_count": len(data_quality.get("missing_units", [])),
            "latest_failed_unit_count": len(data_quality.get("latest_failed_units", [])),
            "web_search_quality_flag_count": len(data_quality.get("web_search_quality_flags", [])),
            "source_quality_flag_count": len(data_quality.get("source_quality_flags", [])),
            "extraction_error_record_count": data_quality.get("extraction_error_record_count", 0),
            "extraction_error_rate": data_quality.get("extraction_error_rate", "0.0%"),
        }
    ]

    return {
        "quality_summary": quality_summary,
        "attempt_facts": attempt_facts,
        "query_facts": query_facts,
        "brand_attempt_facts": sorted(
            brand_attempt_facts,
            key=lambda row: (str(row["query_id"]), int(row["repeat_index"]), str(row["brand_name_canonical"])),
        ),
    }
