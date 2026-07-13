"""Compute deterministic brand, query, stability, and target metrics."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Any

from ..brand_extraction import normalize_brand_name
from .fact_utils import (
    as_bool,
    as_positive_int,
    avg_pairwise_jaccard,
    dominant_sentiment,
    fmt_float,
    is_brand_sov_candidate,
    pct,
    pct_points,
    pct_to_float,
    rank_sort_value,
    top_items,
)


def compute_open_brand_stats(mentions: list[dict[str, Any]], success_records: list[dict], manifest: dict[str, Any]) -> dict[str, Any]:
    query_ids = [q["query_id"] for q in manifest["queries"]]
    query_text = {q["query_id"]: q["query"] for q in manifest["queries"]}
    expected_repeats = int(manifest["repeats"])
    total_success = len(success_records) or 1
    response_keys_by_brand: dict[str, set[tuple[str, int]]] = defaultdict(set)
    recommended_keys_by_brand: dict[str, set[tuple[str, int]]] = defaultdict(set)
    top3_keys_by_brand: dict[str, set[tuple[str, int]]] = defaultdict(set)
    queries_by_brand: dict[str, set[str]] = defaultdict(set)
    confidences: dict[str, list[float]] = defaultdict(list)
    rank_positions: dict[str, list[int]] = defaultdict(list)
    sentiments: dict[str, Counter] = defaultdict(Counter)
    raw_names_by_brand: dict[str, set[str]] = defaultdict(set)
    mentions_by_query_brand: dict[tuple[str, str], set[int]] = defaultdict(set)
    recommended_by_query_brand: dict[tuple[str, str], set[int]] = defaultdict(set)
    source_entity_mentions: list[dict[str, Any]] = []
    sampled_query_ids = {str(record.get("query_id")) for record in success_records if record.get("query_id")}

    brand_events: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in mentions:
        if not is_brand_sov_candidate(row):
            source_entity_mentions.append(row)
            continue
        canonical = row["brand_name_canonical"]
        qid = str(row["query_id"])
        rep = int(row["repeat_index"] or 1)
        key = (canonical, qid, rep)
        event = brand_events.setdefault(
            key,
            {
                "canonical": canonical,
                "qid": qid,
                "rep": rep,
                "raw_names": set(),
                "recommended": False,
                "rank_positions": [],
                "sentiments": Counter(),
                "confidences": [],
            },
        )
        event["raw_names"].add(row["brand_name_raw"])
        if as_bool(row.get("is_recommended")) or str(row.get("role") or "").lower() == "recommended":
            event["recommended"] = True
        rank = as_positive_int(row.get("rank_position"))
        if rank is not None:
            event["rank_positions"].append(rank)
        sentiment = str(row.get("sentiment") or "unknown").lower()
        event["sentiments"][sentiment if sentiment in {"positive", "neutral", "negative", "unknown"} else "unknown"] += 1
        if isinstance(row.get("confidence"), (int, float)):
            confidence = float(row["confidence"])
            event["confidences"].append(confidence)
            confidences[canonical].append(confidence)

    for event in brand_events.values():
        canonical = event["canonical"]
        qid = event["qid"]
        rep = event["rep"]
        key = (qid, rep)
        response_keys_by_brand[canonical].add(key)
        queries_by_brand[canonical].add(qid)
        raw_names_by_brand[canonical].update(event["raw_names"])
        mentions_by_query_brand[(qid, canonical)].add(rep)
        if event["recommended"]:
            recommended_keys_by_brand[canonical].add(key)
            recommended_by_query_brand[(qid, canonical)].add(rep)
        if event["rank_positions"]:
            rank = min(event["rank_positions"])
            rank_positions[canonical].append(rank)
            if rank <= 3:
                top3_keys_by_brand[canonical].add(key)
        sentiments[canonical][dominant_sentiment(event["sentiments"])] += 1

    target_keys = {normalize_brand_name(str(manifest["target_brand"]))}
    target_keys.update(normalize_brand_name(alias) for alias in manifest.get("target_aliases", []) if alias)
    sov_denominator = sum(len(keys) for keys in response_keys_by_brand.values()) or 1
    brand_summary = []
    for canonical, response_keys in response_keys_by_brand.items():
        by_query_rates = [len(mentions_by_query_brand.get((qid, canonical), set())) / expected_repeats for qid in query_ids]
        brand_keys = {normalize_brand_name(canonical), *{normalize_brand_name(name) for name in raw_names_by_brand[canonical]}}
        is_target = bool(target_keys & brand_keys)
        response_count = len(response_keys)
        sentiment_total = sum(sentiments[canonical].values()) or 1
        rank_observed_count = len(rank_positions[canonical])
        sentiment_unknown = sentiments[canonical]["unknown"]
        recommended_rate = pct(len(recommended_keys_by_brand[canonical]) / response_count) if response_count else "0.0%"
        brand_summary.append(
            {
                "brand_name_canonical": canonical,
                "raw_names": " | ".join(sorted(raw_names_by_brand[canonical])),
                "responses_mentioned": response_count,
                "response_mention_rate": pct(response_count / total_success),
                "query_coverage_count": len(queries_by_brand[canonical]),
                "query_coverage_rate": pct(len(queries_by_brand[canonical]) / (len(query_ids) or 1)),
                "query_macro_mention_rate": pct(sum(by_query_rates) / len(query_ids)) if query_ids else "0.0%",
                "sov_response_share": pct(response_count / sov_denominator),
                "recommended_count": len(recommended_keys_by_brand[canonical]),
                "recommended_rate_when_mentioned": recommended_rate,
                "recommended_rate_over_success": pct(len(recommended_keys_by_brand[canonical]) / total_success) if total_success else "0.0%",
                "rank_observed_count": rank_observed_count,
                "rank_observed_rate": pct(rank_observed_count / response_count) if response_count else "0.0%",
                "avg_rank_position": round(statistics.mean(rank_positions[canonical]), 2) if rank_positions[canonical] else "",
                "top3_rate": pct(len(top3_keys_by_brand[canonical]) / response_count) if response_count else "0.0%",
                "positive_rate": pct(sentiments[canonical]["positive"] / sentiment_total),
                "neutral_rate": pct(sentiments[canonical]["neutral"] / sentiment_total),
                "negative_rate": pct(sentiments[canonical]["negative"] / sentiment_total),
                "sentiment_unknown_rate": pct(sentiment_unknown / sentiment_total),
                "sentiment_observed_rate": pct((sentiment_total - sentiment_unknown) / sentiment_total),
                "avg_confidence": round(statistics.mean(confidences[canonical]), 3) if confidences[canonical] else "",
                "is_target_brand": int(is_target),
            }
        )

    brand_summary.sort(
        key=lambda row: (
            -pct_to_float(row["sov_response_share"]),
            -pct_to_float(row["recommended_rate_when_mentioned"]),
            rank_sort_value(row["avg_rank_position"]),
            str(row["brand_name_canonical"]),
        )
    )
    leader_share = pct_to_float(brand_summary[0]["sov_response_share"]) if brand_summary else 0.0
    top3_shares = [pct_to_float(row["sov_response_share"]) for row in brand_summary[:3]]
    top3_avg = sum(top3_shares) / len(top3_shares) if top3_shares else 0.0
    target_row: dict[str, Any] | None = None
    for index, row in enumerate(brand_summary, start=1):
        row["sov_rank"] = index
        if int(row["is_target_brand"]):
            target_share = pct_to_float(row["sov_response_share"])
            row["target_rank_by_sov"] = index
            row["target_sov_gap_to_leader"] = pct_points(leader_share - target_share)
            row["target_sov_gap_to_top3_avg"] = pct_points(max(0.0, top3_avg - target_share))
            target_row = row
        else:
            row["target_rank_by_sov"] = ""
            row["target_sov_gap_to_leader"] = ""
            row["target_sov_gap_to_top3_avg"] = ""

    target_diagnosis = _build_target_diagnosis(
        manifest=manifest,
        target_row=target_row,
        brand_summary=brand_summary,
        query_ids=query_ids,
        sampled_query_ids=sampled_query_ids,
        query_text=query_text,
        mentions_by_query_brand=mentions_by_query_brand,
        recommended_by_query_brand=recommended_by_query_brand,
        expected_repeats=expected_repeats,
        total_success=len(success_records),
    )
    brand_by_query = []
    for qid in query_ids:
        for row in brand_summary:
            canonical = row["brand_name_canonical"]
            reps = mentions_by_query_brand.get((qid, canonical), set())
            if not reps:
                continue
            brand_by_query.append(
                {
                    "query_id": qid,
                    "query": query_text.get(qid, ""),
                    "brand_name_canonical": canonical,
                    "responses_mentioned": len(reps),
                    "mention_rate_within_query": pct(len(reps) / expected_repeats),
                    "recommended_responses": len(recommended_by_query_brand.get((qid, canonical), set())),
                    "recommended_rate_when_mentioned_within_query": pct(len(recommended_by_query_brand.get((qid, canonical), set())) / len(reps))
                    if reps
                    else "0.0%",
                    "recommended_rate_over_success_within_query": pct(len(recommended_by_query_brand.get((qid, canonical), set())) / expected_repeats)
                    if expected_repeats
                    else "0.0%",
                }
            )

    brands_by_response: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in mentions:
        if not is_brand_sov_candidate(row):
            continue
        brands_by_response[(str(row["query_id"]), int(row["repeat_index"] or 1))].add(row["brand_name_canonical"])
    query_stability = []
    for qid in query_ids:
        successful_repeats = sorted({int(record.get("repeat_index") or 1) for record in success_records if record.get("query_id") == qid})
        sets = [brands_by_response.get((qid, repeat_index), set()) for repeat_index in successful_repeats]
        query_stability.append(
            {
                "query_id": qid,
                "query": query_text.get(qid, ""),
                "successful_repeats": len(successful_repeats),
                "expected_repeats": expected_repeats,
                "sample_sufficient": int(len(successful_repeats) >= min(2, expected_repeats)),
                "brand_set_jaccard_avg": fmt_float(avg_pairwise_jaccard(sets)),
                "unique_brand_sets": len({tuple(sorted(item)) for item in sets}),
                "top_brands": top_items([brand for item in sets for brand in item]),
            }
        )

    return {
        "target_detected": any(int(row["is_target_brand"]) for row in brand_summary),
        "brand_summary": brand_summary,
        "brand_by_query": brand_by_query,
        "query_stability": query_stability,
        "target_diagnosis": target_diagnosis,
        "source_entity_mentions": source_entity_mentions,
    }


def _build_target_diagnosis(
    *,
    manifest: dict[str, Any],
    target_row: dict[str, Any] | None,
    brand_summary: list[dict[str, Any]],
    query_ids: list[str],
    sampled_query_ids: set[str],
    query_text: dict[str, str],
    mentions_by_query_brand: dict[tuple[str, str], set[int]],
    recommended_by_query_brand: dict[tuple[str, str], set[int]],
    expected_repeats: int,
    total_success: int,
) -> dict[str, Any]:
    if not target_row:
        return {
            "target_brand": manifest["target_brand"],
            "target_detected": False,
            "target_sov_response_share": "0.0%",
            "target_rank_by_sov": "",
            "target_sov_gap_to_leader": "",
            "target_sov_gap_to_top3_avg": "",
            "target_response_mention_rate": "0.0%",
            "target_recommended_rate_when_mentioned": "0.0%",
            "target_recommended_rate_over_success": "0.0%",
            "target_rank_observed_rate": "0.0%",
            "target_sentiment_unknown_rate": "0.0%",
            "target_query_coverage_rate": "0.0%",
            "missing_queries": [{"query_id": qid, "query": query_text.get(qid, "")} for qid in query_ids if qid in sampled_query_ids],
            "unsampled_queries": [{"query_id": qid, "query": query_text.get(qid, "")} for qid in query_ids if qid not in sampled_query_ids],
            "leader_brand": brand_summary[0]["brand_name_canonical"] if brand_summary else "",
        }

    canonical = str(target_row["brand_name_canonical"])
    missing_queries = [
        {"query_id": qid, "query": query_text.get(qid, "")}
        for qid in query_ids
        if qid in sampled_query_ids and not mentions_by_query_brand.get((qid, canonical))
    ]
    unsampled_queries = [{"query_id": qid, "query": query_text.get(qid, "")} for qid in query_ids if qid not in sampled_query_ids]
    query_details = []
    for qid in query_ids:
        reps = mentions_by_query_brand.get((qid, canonical), set())
        recs = recommended_by_query_brand.get((qid, canonical), set())
        query_details.append(
            {
                "query_id": qid,
                "query": query_text.get(qid, ""),
                "target_mentions": len(reps),
                "target_mention_rate": pct(len(reps) / expected_repeats) if expected_repeats else "0.0%",
                "target_recommendations": len(recs),
                "target_recommendation_rate": pct(len(recs) / len(reps)) if reps else "0.0%",
                "target_recommendation_rate_over_success": pct(len(recs) / expected_repeats) if expected_repeats else "0.0%",
            }
        )
    return {
        "target_brand": manifest["target_brand"],
        "target_detected": True,
        "target_canonical_name": canonical,
        "target_sov_response_share": target_row["sov_response_share"],
        "target_rank_by_sov": target_row["target_rank_by_sov"],
        "target_sov_gap_to_leader": target_row["target_sov_gap_to_leader"],
        "target_sov_gap_to_top3_avg": target_row["target_sov_gap_to_top3_avg"],
        "target_response_mention_rate": target_row["response_mention_rate"],
        "target_recommended_rate_when_mentioned": target_row["recommended_rate_when_mentioned"],
        "target_recommended_rate_over_success": target_row["recommended_rate_over_success"],
        "target_rank_observed_rate": target_row["rank_observed_rate"],
        "target_sentiment_unknown_rate": target_row["sentiment_unknown_rate"],
        "target_query_coverage_rate": target_row["query_coverage_rate"],
        "target_success_sample_count": total_success,
        "missing_queries": missing_queries,
        "unsampled_queries": unsampled_queries,
        "query_details": query_details,
        "leader_brand": brand_summary[0]["brand_name_canonical"] if brand_summary else "",
    }
