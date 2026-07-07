from __future__ import annotations

import csv
import hashlib
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .cache import (
    JsonlCache,
    canonicalization_cache_entry,
    canonicalization_cache_key,
    extraction_cache_entry,
    extraction_cache_key,
    raw_names_hash,
    response_text_hash,
)
from ..brand_extraction import BrandCanonicalizer, BrandMentionExtractor, LLMBrandExtractor, fallback_canonicalize, normalize_brand_name
from ..config import LiveSettingsError, Settings, get_settings, redact_secret, validate_live_settings, workspace_root
from ..exporters import canonical_request_hash, latest_records, read_jsonl, read_jsonl_with_errors, safe_result_key, sanitize_csv_row, write_jsonl
from ..job import BUNDLE_LOCK, RUNS_DIR, QueryManifestIntegrityError, QueryManifestSourceError, load_job_manifest, load_job_queries, logs_dir, query_set_hash, raw_attempts_path, result_dir, update_job_manifest, work_dir, _cleanup_job_bundle_unlocked, _job_lock
from ..query_meta import query_metadata_json, tags_text
from ..request_fingerprint import REQUEST_FINGERPRINT_VERSION, legacy_payload_hash, request_fingerprint
from ..reporting import build_html, markdown_text, table_cell, try_generate_pdf


EXTRACTION_SCHEMA_VERSION = "brand-extraction-v3"


CSV_FIELD_SCHEMAS = {
    "brand_mentions_extracted": [
        "query_id",
        "repeat_index",
        "input_query",
        "brand_name_raw",
        "brand_name_canonical",
        "brand_type",
        "sov_eligible",
        "role",
        "is_recommended",
        "rank_position",
        "sentiment",
        "mention_context",
        "confidence",
        "evidence",
        "canonical_hint",
    ],
    "brand_canonical_map": ["brand_name_raw", "brand_name_canonical"],
    "brand_summary": [
        "sov_rank",
        "brand_name_canonical",
        "raw_names",
        "responses_mentioned",
        "response_mention_count",
        "mention_rate",
        "response_mention_rate",
        "query_coverage",
        "query_coverage_count",
        "query_coverage_rate",
        "query_macro_mention_rate",
        "sov_event_share",
        "sov_response_share",
        "recommended_count",
        "recommended_rate",
        "recommended_rate_when_mentioned",
        "recommended_rate_over_success",
        "rank_observed_count",
        "rank_observed_rate",
        "avg_rank_position",
        "top3_rate",
        "positive_rate",
        "neutral_rate",
        "negative_rate",
        "sentiment_unknown_rate",
        "sentiment_observed_rate",
        "avg_confidence",
        "is_target_brand",
        "target_brand_detected",
        "target_rank_by_sov",
        "target_sov_gap_to_leader",
        "target_sov_gap_to_top3_avg",
    ],
    "brand_by_query": [
        "query_id",
        "query",
        "brand_name_canonical",
        "responses_mentioned",
        "mention_rate_within_query",
        "recommended_responses",
        "recommended_rate_within_query",
        "recommended_rate_when_mentioned_within_query",
        "recommended_rate_over_success_within_query",
    ],
    "query_stability": [
        "query_id",
        "query",
        "successful_repeats",
        "expected_repeats",
        "sample_sufficient",
        "brand_set_jaccard_avg",
        "unique_brand_sets",
        "top_brands",
    ],
    "source_entity_mentions": [
        "query_id",
        "repeat_index",
        "input_query",
        "brand_name_raw",
        "brand_name_canonical",
        "brand_type",
        "sov_eligible",
        "role",
        "mention_context",
        "evidence",
    ],
    "source_domains": [
        "domain",
        "parsed_source_occurrences",
        "distinct_source_url_count",
        "response_coverage",
        "response_coverage_rate",
        "query_coverage",
        "query_coverage_rate",
        "avg_source_order",
        "best_source_order",
        "top_urls",
    ],
    "source_urls": ["url", "domain", "title", "parsed_source_occurrences"],
    "source_by_query": [
        "query_id",
        "domain",
        "repeat_coverage",
        "repeat_coverage_rate",
        "parsed_source_occurrences",
        "distinct_source_url_count",
        "avg_source_order",
        "top_urls",
    ],
    "brand_trends": [
        "job_id",
        "target_brand",
        "industry",
        "market",
        "model",
        "web_search_limit",
        "extraction_schema_version",
        "expected_queries",
        "expected_repeats",
        "sample_mode",
        "query_set_hash",
        "conclusion_strength",
        "extraction_error_rate",
        "comparability_key",
        "partial_sample",
        "brand_name_canonical",
        "is_target_brand",
        "sov_rank",
        "sov_event_share",
        "sov_response_share",
        "response_mention_rate",
        "query_coverage_rate",
        "recommended_rate",
        "recommended_rate_when_mentioned",
        "recommended_rate_over_success",
        "rank_observed_rate",
        "sentiment_unknown_rate",
        "avg_rank_position",
        "positive_rate",
        "neutral_rate",
        "negative_rate",
        "target_sov_gap_to_leader",
        "target_sov_gap_to_top3_avg",
        "success_record_count",
    ],
}


def estimate_job_analysis(bundle_dir: str | Path, *, include_mock: bool = False, refresh_extraction_cache: bool = False) -> dict[str, Any]:
    root = Path(bundle_dir)
    manifest = load_job_manifest(root)
    raw_path = raw_attempts_path(root)
    if not raw_path.exists():
        raise ValueError(f"缺少 raw attempts：{raw_path}")
    raw_records, raw_read_errors = read_jsonl_with_errors(raw_path)
    manifest = _manifest_with_queries_for_analysis(root, manifest, raw_records)
    analysis_profile = _analysis_profile(manifest)
    live_records = latest_records(raw_records, statuses={"success"})
    mock_records = latest_records(raw_records, statuses={"mock"}) if include_mock else []
    if live_records:
        analysis_records = live_records
        sample_mode = "live"
        cache_estimate = _estimate_live_cache_requests(
            logs_dir(root),
            live_records,
            extractor_model=str(analysis_profile.get("model") or manifest.get("model") or ""),
            refresh_extraction_cache=refresh_extraction_cache,
        )
        extraction_requests = cache_estimate["extraction_cache_misses"]
        canonicalization_requests = cache_estimate["canonicalization_cache_misses"]
    elif include_mock:
        analysis_records = mock_records
        sample_mode = "mock" if mock_records else "live"
        extraction_requests = 0
        canonicalization_requests = 0
        cache_estimate = _empty_cache_stats()
    else:
        analysis_records = []
        sample_mode = "live"
        extraction_requests = 0
        canonicalization_requests = 0
        cache_estimate = _empty_cache_stats()
    return {
        "job_id": manifest.get("job_id"),
        "sample_mode": sample_mode,
        "raw_record_count": len(raw_records),
        "raw_read_error_count": len(raw_read_errors),
        "analysis_record_count": len(analysis_records),
        "analysis_llm_requests_estimate": extraction_requests + canonicalization_requests,
        "extraction_requests_estimate": extraction_requests,
        "canonicalization_requests_estimate": canonicalization_requests,
        "model": manifest.get("model"),
        "analysis_profile": analysis_profile,
        "cache": cache_estimate,
    }


def analyze_job_bundle(
    bundle_dir: str | Path,
    *,
    settings: Settings | None = None,
    extractor: BrandMentionExtractor | None = None,
    canonicalizer: BrandCanonicalizer | None = None,
    keep_work: bool = False,
    include_mock: bool = False,
    confirm_cost: bool = False,
    refresh_extraction_cache: bool = False,
    write_aggregates: bool = True,
) -> dict[str, Any]:
    root = Path(bundle_dir)
    with _job_lock(root / BUNDLE_LOCK):
        update_job_manifest(root, status="analyzing")
        try:
            return _analyze_job_bundle_unlocked(
                root,
                settings=settings,
                extractor=extractor,
                canonicalizer=canonicalizer,
                keep_work=keep_work,
                include_mock=include_mock,
                confirm_cost=confirm_cost,
                refresh_extraction_cache=refresh_extraction_cache,
                write_aggregates=write_aggregates,
            )
        except Exception:
            try:
                update_job_manifest(root, status="analysis_failed")
            except Exception:
                pass
            raise


def _analyze_job_bundle_unlocked(
    bundle_dir: str | Path,
    *,
    settings: Settings | None = None,
    extractor: BrandMentionExtractor | None = None,
    canonicalizer: BrandCanonicalizer | None = None,
    keep_work: bool = False,
    include_mock: bool = False,
    confirm_cost: bool = False,
    refresh_extraction_cache: bool = False,
    write_aggregates: bool = True,
) -> dict[str, Any]:
    settings = settings or get_settings()
    root = Path(bundle_dir)
    manifest = load_job_manifest(root)
    raw_path = raw_attempts_path(root)
    if not raw_path.exists():
        raise ValueError(f"缺少 raw attempts：{raw_path}")

    work = work_dir(root)
    logs = logs_dir(root)
    result = result_dir(root)
    for path in [work, logs, result]:
        path.mkdir(parents=True, exist_ok=True)

    raw_records, raw_read_errors = read_jsonl_with_errors(raw_path)
    manifest = _manifest_with_queries_for_analysis(root, manifest, raw_records)
    analysis_profile = _analysis_profile(manifest)
    analysis_model = str(analysis_profile.get("model") or manifest["model"])
    live_records = latest_records(raw_records, statuses={"success"})
    mock_records = latest_records(raw_records, statuses={"mock"}) if include_mock else []
    if live_records:
        analysis_statuses = {"success"}
        success_records = live_records
        sample_mode = "live"
        ignored_mock_record_count = len(mock_records)
    elif include_mock:
        analysis_statuses = {"mock"}
        success_records = mock_records
        sample_mode = "mock" if mock_records else "live"
        ignored_mock_record_count = 0
    else:
        analysis_statuses = {"success"}
        success_records = []
        sample_mode = "live"
        ignored_mock_record_count = 0
    data_quality = evaluate_data_quality(raw_records, success_records, raw_read_errors, manifest, analysis_statuses=analysis_statuses)
    if sample_mode == "mock":
        data_quality["conclusion_strength"] = "observational"
    if ignored_mock_record_count:
        data_quality["ignored_mock_record_count"] = ignored_mock_record_count
    success_records_for_stats = _records_for_stats(success_records, data_quality, manifest)
    preflight_cache_stats = _empty_cache_stats()
    cache_stats = _empty_cache_stats()
    extractor_obj = None
    if sample_mode == "live" and success_records_for_stats and extractor is None:
        preflight_cache_stats = _estimate_live_cache_requests(
            logs,
            success_records_for_stats,
            extractor_model=analysis_model,
            refresh_extraction_cache=refresh_extraction_cache,
        )
        if preflight_cache_stats["analysis_llm_requests_remaining"] > 0 and not confirm_cost:
            raise ValueError("分析阶段会产生 LLM API 成本；请确认预算后显式传入 confirm_cost=True")
        if preflight_cache_stats["analysis_llm_requests_remaining"] > 0:
            try:
                validate_live_settings(settings)
            except LiveSettingsError as exc:
                raise ValueError(str(exc)) from exc
    if extractor is None and success_records_for_stats and sample_mode == "live" and preflight_cache_stats["analysis_llm_requests_remaining"] > 0:
        extractor_obj = LLMBrandExtractor(settings, model=analysis_model)
    active_extractor = extractor or (extractor_obj.extract_record if extractor_obj else None)
    if active_extractor is None and sample_mode == "mock":
        active_extractor = lambda record: demo_extract_record(record, manifest)

    mention_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    if active_extractor or (sample_mode == "live" and extractor is None):
        mention_rows, error_rows, extraction_cache_stats = _extract_mentions(
            records=success_records_for_stats,
            active_extractor=active_extractor,
            settings=settings,
            logs=logs,
            cache_enabled=sample_mode == "live" and extractor is None,
            extractor_model=analysis_model,
            refresh_extraction_cache=refresh_extraction_cache,
        )
        _merge_cache_stats(cache_stats, extraction_cache_stats)

    raw_names = [row["brand_name_raw"] for row in mention_rows]
    if canonicalizer:
        canonical_map, canonical_error = canonicalizer(raw_names)
    elif extractor_obj and raw_names:
        canonical_map, canonical_error, canonical_cache_stats = _canonicalize_with_cache(
            raw_names=raw_names,
            extractor_obj=extractor_obj,
            logs=logs,
            refresh_extraction_cache=refresh_extraction_cache,
        )
        _merge_cache_stats(cache_stats, canonical_cache_stats)
    elif sample_mode == "live" and extractor is None and raw_names:
        canonical_map, canonical_error, canonical_cache_stats = _canonicalize_from_cache_only(
            raw_names=raw_names,
            canonicalizer_model=analysis_model,
            logs=logs,
            refresh_extraction_cache=refresh_extraction_cache,
        )
        _merge_cache_stats(cache_stats, canonical_cache_stats)
    else:
        canonical_map, canonical_error = fallback_canonicalize(raw_names)
    if canonical_error:
        error_rows.append(_redacted_error(canonical_error, settings))
    canonical_map = _apply_target_alias_canonicalization(canonical_map, raw_names, manifest)
    _apply_extraction_quality(data_quality, error_rows, len(success_records_for_stats))

    enriched_mentions = []
    for row in mention_rows:
        copy = dict(row)
        copy["brand_name_canonical"] = canonical_map.get(row["brand_name_raw"], row["brand_name_raw"])
        enriched_mentions.append(copy)

    stats = compute_open_brand_stats(enriched_mentions, success_records_for_stats, manifest)
    source_stats = compute_source_stats(success_records_for_stats, manifest)
    files = write_job_analysis_files(
        root=root,
        work=work,
        logs=logs,
        result=result,
        mentions=enriched_mentions,
        errors=error_rows,
        canonical_map=canonical_map,
        stats=stats,
        source_stats=source_stats,
        data_quality=data_quality,
    )

    summary = {
        "job_id": manifest["job_id"],
        "title": f"{manifest['target_brand']} GEO 开放品牌发现报告",
        "target_brand": manifest["target_brand"],
        "target_aliases": manifest.get("target_aliases", []),
        "industry": manifest["industry"],
        "market": manifest["market"],
        "expected_queries": manifest["query_count"],
        "expected_repeats": manifest["repeats"],
        "expected_units": manifest["query_count"] * manifest["repeats"],
        "model": manifest["model"],
        "web_search_limit": manifest["web_search_limit"],
        "sampling_profile": manifest.get("sampling_profile", {}),
        "analysis_profile": analysis_profile,
        "comparability_profile": manifest.get("comparability_profile", {}),
        "job_conclusion_strength": data_quality.get("conclusion_strength", "observational"),
        "query_set_hash": query_set_hash(manifest),
        "raw_record_count": len(raw_records),
        "success_record_count": len(success_records_for_stats),
        "analysis_record_count": len(success_records),
        "sample_mode": sample_mode,
        "query_ids": [q["query_id"] for q in manifest["queries"]],
        "partial_sample": bool(data_quality["partial_sample"]),
        "data_quality": data_quality,
        "cache": cache_stats,
        "extracted_mention_count": len(enriched_mentions),
        "extraction_error_count": data_quality.get("extraction_error_count", 0),
        "extraction_error_record_count": data_quality.get("extraction_error_record_count", 0),
        "extraction_error_row_count": data_quality.get("extraction_error_row_count", len(error_rows)),
        "extraction_error_rate": data_quality.get("extraction_error_rate", "0.0%"),
        "traceability_quarantine_count": data_quality.get("traceability_quarantine_count", 0),
        "target_brand_detected": stats["target_brand_detected"],
        "brand_summary": stats["brand_summary"],
        "brand_by_query": stats["brand_by_query"],
        "sov_summary": stats["sov_summary"],
        "query_stability": stats["query_stability"],
        "source_domains": source_stats["source_domains"],
        "source_urls": source_stats["source_urls"],
        "source_by_query": source_stats["source_by_query"],
        "target_diagnosis": stats["target_diagnosis"],
        "analysis_files": {key: _rel(root, value) for key, value in files.items()},
        "method_note": "本报告基于 query 文本采样后的 LLM 开放式品牌抽取；SOV 主口径为品牌命中事件份额，不等同于市场份额。",
    }
    summary_path = logs / "analysis_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    aggregate_files = update_cross_job_aggregates(root, summary) if write_aggregates else {}
    summary["aggregate_files"] = {key: _display_path(root, value) for key, value in aggregate_files.items()}
    report_files = generate_job_report(summary, result)
    summary["report_files"] = {key: _rel(root, value) for key, value in report_files.items()}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    analysis_status = "analyzed_partial" if data_quality.get("conclusion_strength") == "observational" or error_rows else "analyzed"
    update_job_manifest(root, status=analysis_status)
    if not keep_work:
        _cleanup_job_bundle_unlocked(root)
    return {"bundle_dir": str(root), "analysis_dir": str(result), "report_dir": str(result), **summary}


def _manifest_with_queries_for_analysis(root: Path, manifest: dict[str, Any], raw_records: list[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(manifest.get("queries"), list) and manifest["queries"]:
        return manifest
    try:
        records = load_job_queries(root, manifest, materialize=False)
    except (QueryManifestIntegrityError, QueryManifestSourceError):
        raise
    except Exception:
        records = []
    if records:
        hydrated = dict(manifest)
        hydrated["queries"] = _query_rows_from_records(records)
        hydrated["query_count"] = len(hydrated["queries"])
        return hydrated
    queries_by_id: dict[str, dict[str, Any]] = {}
    for record in raw_records:
        qid = str(record.get("query_id") or "").strip()
        if not qid or qid in queries_by_id:
            continue
        query = str(record.get("query") or record.get("input_query") or "").strip()
        if not query:
            continue
        meta = record.get("query_meta") if isinstance(record.get("query_meta"), dict) else {}
        record_metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        row = {
            "query_id": qid,
            "query": query,
            "variant_id": str(meta.get("variant_id") or ""),
            "seed_id": str(meta.get("seed_id") or ""),
            "seed_query": str(meta.get("seed_query") or ""),
            "category": str(meta.get("category") or record_metadata.get("category") or ""),
            "intent": str(meta.get("intent") or ""),
            "persona": str(meta.get("persona") or record_metadata.get("persona") or ""),
            "template_id": str(meta.get("template_id") or ""),
            "locale": str(meta.get("locale") or record_metadata.get("locale") or ""),
            "market": str(meta.get("market") or record_metadata.get("market") or ""),
            "tags": tags_text(meta.get("tags") or record_metadata.get("tags")),
            "language": str(meta.get("language") or record_metadata.get("locale") or ""),
            "generation_method": str(meta.get("generation_method") or ""),
            "fanout_version": str(meta.get("fanout_version") or ""),
            "manifest_version": str(meta.get("manifest_version") or ""),
            "locked_at": str(meta.get("locked_at") or record_metadata.get("locked_at") or ""),
            "query_metadata_json": query_metadata_json(meta, record_metadata),
        }
        queries_by_id[qid] = {key: value for key, value in row.items() if value != ""}
    if not queries_by_id:
        return manifest
    hydrated = dict(manifest)
    expected_query_count = int((manifest.get("query_manifest") or {}).get("row_count") or manifest.get("query_count") or len(queries_by_id))
    hydrated["queries"] = [queries_by_id[key] for key in sorted(queries_by_id)]
    hydrated["query_count"] = expected_query_count
    if len(hydrated["queries"]) < expected_query_count:
        hydrated["_query_universe_incomplete"] = True
        hydrated["_observed_query_count"] = len(hydrated["queries"])
        hydrated["_missing_unknown_query_count"] = expected_query_count - len(hydrated["queries"])
    return hydrated


def _analysis_profile(manifest: dict[str, Any]) -> dict[str, Any]:
    profile = manifest.get("analysis_profile") if isinstance(manifest.get("analysis_profile"), dict) else {}
    if profile:
        return profile
    return {
        "provider": "openai_compatible",
        "adapter": "openai_responses_text",
        "adapter_version": "1",
        "api_family": "responses",
        "model": str(manifest.get("model") or ""),
        "base_url_fingerprint": "",
        "analysis_fingerprint": "",
    }



def _query_rows_from_records(records: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = {"query_id": record.query_id, "query": record.query}
        if record.locale:
            row["locale"] = record.locale
        if record.market:
            row["market"] = record.market
        if record.category:
            row["category"] = record.category
        if record.tags:
            row["tags"] = ",".join(record.tags)
        row.update(record.metadata)
        rows.append(row)
    return rows


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
        if not _is_brand_sov_candidate(row):
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
        if _as_bool(row.get("is_recommended")) or str(row.get("role") or "").lower() == "recommended":
            event["recommended"] = True
        rank = _as_positive_int(row.get("rank_position"))
        if rank is not None:
            event["rank_positions"].append(rank)
        sentiment = str(row.get("sentiment") or "unknown").lower()
        event["sentiments"][sentiment if sentiment in {"positive", "neutral", "negative", "unknown"} else "unknown"] += 1
        if isinstance(row.get("confidence"), (int, float)):
            event["confidences"].append(float(row["confidence"]))

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
        sentiments[canonical][_dominant_sentiment(event["sentiments"])] += 1
        if event["confidences"]:
            confidences[canonical].append(max(event["confidences"]))

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
        brand_summary.append({
            "brand_name_canonical": canonical,
            "raw_names": " | ".join(sorted(raw_names_by_brand[canonical])),
            "responses_mentioned": response_count,
            "response_mention_count": response_count,
            "mention_rate": _pct(response_count / total_success),
            "response_mention_rate": _pct(response_count / total_success),
            "query_coverage": len(queries_by_brand[canonical]),
            "query_coverage_count": len(queries_by_brand[canonical]),
            "query_coverage_rate": _pct(len(queries_by_brand[canonical]) / (len(query_ids) or 1)),
            "query_macro_mention_rate": _pct(sum(by_query_rates) / len(query_ids)) if query_ids else "0.0%",
            "sov_response_share": _pct(response_count / sov_denominator),
            "sov_event_share": _pct(response_count / sov_denominator),
            "recommended_count": len(recommended_keys_by_brand[canonical]),
            "recommended_rate": _pct(len(recommended_keys_by_brand[canonical]) / response_count) if response_count else "0.0%",
            "recommended_rate_when_mentioned": _pct(len(recommended_keys_by_brand[canonical]) / response_count) if response_count else "0.0%",
            "recommended_rate_over_success": _pct(len(recommended_keys_by_brand[canonical]) / total_success) if total_success else "0.0%",
            "rank_observed_count": rank_observed_count,
            "rank_observed_rate": _pct(rank_observed_count / response_count) if response_count else "0.0%",
            "avg_rank_position": round(statistics.mean(rank_positions[canonical]), 2) if rank_positions[canonical] else "",
            "top3_rate": _pct(len(top3_keys_by_brand[canonical]) / response_count) if response_count else "0.0%",
            "positive_rate": _pct(sentiments[canonical]["positive"] / sentiment_total),
            "neutral_rate": _pct(sentiments[canonical]["neutral"] / sentiment_total),
            "negative_rate": _pct(sentiments[canonical]["negative"] / sentiment_total),
            "sentiment_unknown_rate": _pct(sentiment_unknown / sentiment_total),
            "sentiment_observed_rate": _pct((sentiment_total - sentiment_unknown) / sentiment_total),
            "avg_confidence": round(statistics.mean(confidences[canonical]), 3) if confidences[canonical] else "",
            "target_brand_detected": int(is_target),
            "is_target_brand": int(is_target),
        })

    brand_summary.sort(key=lambda row: (-_pct_to_float(row["sov_response_share"]), -_pct_to_float(row["recommended_rate"]), _rank_sort_value(row["avg_rank_position"]), str(row["brand_name_canonical"])))
    leader_share = _pct_to_float(brand_summary[0]["sov_response_share"]) if brand_summary else 0.0
    top3_shares = [_pct_to_float(row["sov_response_share"]) for row in brand_summary[:3]]
    top3_avg = sum(top3_shares) / len(top3_shares) if top3_shares else 0.0
    target_row: dict[str, Any] | None = None
    for index, row in enumerate(brand_summary, start=1):
        row["sov_rank"] = index
        if int(row["is_target_brand"]):
            target_share = _pct_to_float(row["sov_response_share"])
            row["target_rank_by_sov"] = index
            row["target_sov_gap_to_leader"] = _pct_points(leader_share - target_share)
            row["target_sov_gap_to_top3_avg"] = _pct_points(max(0.0, top3_avg - target_share))
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
            brand_by_query.append({
                "query_id": qid,
                "query": query_text.get(qid, ""),
                "brand_name_canonical": canonical,
                "responses_mentioned": len(reps),
                "mention_rate_within_query": _pct(len(reps) / expected_repeats),
                "recommended_responses": len(recommended_by_query_brand.get((qid, canonical), set())),
                "recommended_rate_within_query": _pct(len(recommended_by_query_brand.get((qid, canonical), set())) / len(reps)) if reps else "0.0%",
                "recommended_rate_when_mentioned_within_query": _pct(len(recommended_by_query_brand.get((qid, canonical), set())) / len(reps)) if reps else "0.0%",
                "recommended_rate_over_success_within_query": _pct(len(recommended_by_query_brand.get((qid, canonical), set())) / expected_repeats) if expected_repeats else "0.0%",
            })

    brands_by_response: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in mentions:
        if not _is_brand_sov_candidate(row):
            continue
        brands_by_response[(str(row["query_id"]), int(row["repeat_index"] or 1))].add(row["brand_name_canonical"])
    query_stability = []
    for qid in query_ids:
        successful_repeats = sorted({int(record.get("repeat_index") or 1) for record in success_records if record.get("query_id") == qid})
        sets = [brands_by_response.get((qid, repeat_index), set()) for repeat_index in successful_repeats]
        query_stability.append({
            "query_id": qid,
            "query": query_text.get(qid, ""),
            "successful_repeats": len(successful_repeats),
            "expected_repeats": expected_repeats,
            "sample_sufficient": int(len(successful_repeats) >= min(2, expected_repeats)),
            "brand_set_jaccard_avg": _fmt_float(_avg_pairwise_jaccard(sets, treat_empty_as_missing=True)),
            "unique_brand_sets": len({tuple(sorted(item)) for item in sets}),
            "top_brands": _top_items([brand for item in sets for brand in item]),
        })

    return {
        "target_brand_detected": any(int(row["target_brand_detected"]) for row in brand_summary),
        "brand_summary": brand_summary,
        "brand_by_query": brand_by_query,
        "sov_summary": brand_summary,
        "query_stability": query_stability,
        "target_diagnosis": target_diagnosis,
        "source_entity_mentions": source_entity_mentions,
    }


def write_job_analysis_files(
    *,
    root: Path,
    work: Path,
    logs: Path,
    result: Path,
    mentions: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    canonical_map: dict[str, str],
    stats: dict[str, Any],
    source_stats: dict[str, Any],
    data_quality: dict[str, Any],
) -> dict[str, Path]:
    work.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    result.mkdir(parents=True, exist_ok=True)
    write_jsonl(work / "brand_mentions_raw.jsonl", mentions)
    (work / "brand_canonical_map_work.json").write_text(
        json.dumps(canonical_map, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    files = {
        "extraction_errors_jsonl": logs / "extraction_errors.jsonl",
        "raw_read_errors_jsonl": logs / "raw_read_errors.jsonl",
        "data_quality": logs / "data_quality.json",
        "brand_mentions_extracted": result / "brand_mentions_extracted.csv",
        "brand_canonical_map": result / "brand_canonical_map.csv",
        "discovered_brands": result / "discovered_brands.csv",
        "brand_summary": result / "brand_summary.csv",
        "sov_summary": result / "sov_summary.csv",
        "brand_by_query": result / "brand_by_query.csv",
        "query_stability": result / "query_stability.csv",
        "source_entity_mentions": result / "source_entity_mentions.csv",
        "source_domains": result / "source_domains.csv",
        "source_urls": result / "source_urls.csv",
        "source_by_query": result / "source_by_query.csv",
    }
    write_jsonl(files["extraction_errors_jsonl"], errors)
    write_jsonl(files["raw_read_errors_jsonl"], data_quality.get("raw_read_errors", []))
    files["data_quality"].write_text(json.dumps(data_quality, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(files["brand_mentions_extracted"], mentions, schema="brand_mentions_extracted")
    _write_csv(files["brand_canonical_map"], [{"brand_name_raw": raw, "brand_name_canonical": canonical} for raw, canonical in sorted(canonical_map.items())], schema="brand_canonical_map")
    _write_csv(files["discovered_brands"], stats["brand_summary"], schema="brand_summary")
    _write_csv(files["brand_summary"], stats["brand_summary"], schema="brand_summary")
    _write_csv(files["sov_summary"], stats["sov_summary"], schema="brand_summary")
    _write_csv(files["brand_by_query"], stats["brand_by_query"], schema="brand_by_query")
    _write_csv(files["query_stability"], stats["query_stability"], schema="query_stability")
    _write_csv(files["source_entity_mentions"], stats["source_entity_mentions"], schema="source_entity_mentions")
    _write_csv(files["source_domains"], source_stats["source_domains"], schema="source_domains")
    _write_csv(files["source_urls"], source_stats["source_urls"], schema="source_urls")
    _write_csv(files["source_by_query"], source_stats["source_by_query"], schema="source_by_query")
    return files


def generate_job_report(summary: dict[str, Any], report_dir: Path) -> dict[str, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / "report.md"
    html_path = report_dir / "report.html"
    pdf_path = report_dir / "report.pdf"
    markdown = build_job_markdown(summary)
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(build_html(markdown, summary), encoding="utf-8")
    files = {"markdown": md_path, "html": html_path}
    if try_generate_pdf(html_path, pdf_path):
        files["pdf"] = pdf_path
    return files


def build_job_markdown(summary: dict[str, Any]) -> str:
    lines = [f"# {markdown_text(summary['title'])}", "", "## 1. Executive Summary", ""]
    data_quality = summary.get("data_quality") or {}
    if summary.get("sample_mode") == "mock":
        lines.append("- 当前报告基于 mock 样本，只用于验收交付链路，不构成业务结论。")
    if data_quality.get("conclusion_strength") == "observational":
        lines.append("- 当前样本完整性或抽取质量不足，以下结果应作为观察线索，不建议作为强排名结论。")
    if int(summary.get("success_record_count") or 0) == 0:
        lines.append("- 当前没有 live success 样本，不输出品牌发现结论。")
    elif not summary.get("brand_summary"):
        lines.append("- 当前成功样本中未抽取到明确品牌、公司或机构名称。")
    else:
        top = summary["brand_summary"][0]
        diagnosis = summary.get("target_diagnosis") or {}
        lines.append(f"- 当前品牌命中事件份额最高的是 {markdown_text(top['brand_name_canonical'])}，份额为 {top['sov_event_share']}，覆盖 {top['query_coverage_rate']} 的 query。")
        if diagnosis.get("target_detected"):
            lines.append(
                f"- 目标品牌 {markdown_text(summary['target_brand'])} 的品牌命中事件份额为 {diagnosis.get('target_sov_event_share', diagnosis.get('target_sov_response_share'))}，"
                f"样本内份额排序第 {diagnosis.get('target_rank_by_sov')}，与第一名差距 {diagnosis.get('target_sov_gap_to_leader')}。"
            )
        else:
            lines.append(f"- 目标品牌 {markdown_text(summary['target_brand'])} 在当前抽取口径下未命中，需结合别名和 raw response 复核。")
        unstable = [row for row in summary.get("query_stability", []) if row.get("brand_set_jaccard_avg") not in {"", 1, 1.0}]
        if unstable:
            lines.append(f"- 有 {len(unstable)} 个 query 的品牌集合在重复采样中存在波动，建议优先人工复核这些 query 的 raw response。")
    lines.extend([
        "",
        "## 2. 任务配置",
        "",
        "| 项目 | 值 |",
        "|---|---|",
        f"| 目标品牌 | {table_cell(summary['target_brand'])} |",
        f"| 行业 | {table_cell(summary['industry'])} |",
        f"| 市场 | {table_cell(summary['market'])} |",
        f"| Query 数 | {summary['expected_queries']} |",
        f"| 每 query 重复次数 | {summary['expected_repeats']} |",
        f"| 成功回答数 | {summary['success_record_count']} |",
        f"| 抽取品牌提及数 | {summary['extracted_mention_count']} |",
        f"| 抽取错误数 | {summary['extraction_error_count']} |",
        f"| 样本模式 | {summary.get('sample_mode', 'live')} |",
        f"| SOV 主口径 | 品牌命中事件份额 |",
        "",
        "## 3. Data Quality",
        "",
        "| 项目 | 值 |",
        "|---|---:|",
        f"| 计划采样单元 | {data_quality.get('planned_units', summary.get('expected_units'))} |",
        f"| 可分析样本数 | {data_quality.get('analysis_record_count', summary.get('success_record_count'))} |",
        f"| 缺失采样单元 | {len(data_quality.get('missing_units', []))} |",
        f"| 额外采样单元 | {len(data_quality.get('extra_units', []))} |",
        f"| 重复采样单元 | {len(data_quality.get('duplicate_units', []))} |",
        f"| 请求契约不一致 | {len(data_quality.get('contract_mismatches', []))} |",
        f"| raw 读取错误 | {len(data_quality.get('raw_read_errors', []))} |",
        f"| 抽取异常回答数 | {data_quality.get('extraction_error_record_count', summary.get('extraction_error_count', 0))} |",
        f"| 抽取异常明细行数 | {data_quality.get('extraction_error_row_count', summary.get('extraction_error_row_count', 0))} |",
        f"| 追溯隔离项数 | {data_quality.get('traceability_quarantine_count', summary.get('traceability_quarantine_count', 0))} |",
        f"| 抽取错误率 | {data_quality.get('extraction_error_rate', summary.get('extraction_error_rate', '0.0%'))} |",
        f"| 结论强度 | {data_quality.get('conclusion_strength', 'strong')} |",
        "",
        "## 4. Brand Visibility / SOV",
        "",
    ])
    if summary.get("brand_summary"):
        lines.extend([
            "| 排名 | 品牌/机构 | 命中事件份额 | 回答提及率 | Query 覆盖率 | 提及后推荐率 | 全样本推荐率 | 平均排名 | 排名观测率 | 正向率 | 未知情感率 | 目标品牌 |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for row in summary["brand_summary"][:20]:
            lines.append(
                f"| {row['sov_rank']} | {table_cell(row['brand_name_canonical'])} | {row['sov_event_share']} | "
                f"{row['response_mention_rate']} | {row['query_coverage_rate']} | {row['recommended_rate_when_mentioned']} | "
                f"{row['recommended_rate_over_success']} | {row['avg_rank_position']} | {row['rank_observed_rate']} | "
                f"{row['positive_rate']} | {row['sentiment_unknown_rate']} | {row['is_target_brand']} |"
            )
    else:
        lines.append("暂无可展示的品牌发现结果。")

    diagnosis = summary.get("target_diagnosis") or {}
    lines.extend(["", "## 5. Target Brand Diagnosis", ""])
    if int(summary.get("success_record_count") or 0) == 0:
        lines.append("当前没有 live success 样本，不能判断目标品牌是否缺失或弱势。")
    elif diagnosis.get("target_detected"):
        lines.extend([
            "| 指标 | 值 |",
            "|---|---:|",
            f"| 目标品牌命中事件份额 | {diagnosis.get('target_sov_event_share', diagnosis.get('target_sov_response_share'))} |",
            f"| 样本内份额排序 | {diagnosis.get('target_rank_by_sov')} |",
            f"| 与第一名差距 | {diagnosis.get('target_sov_gap_to_leader')} |",
            f"| 与 Top3 平均差距 | {diagnosis.get('target_sov_gap_to_top3_avg')} |",
            f"| 回答提及率 | {diagnosis.get('target_response_mention_rate')} |",
            f"| 提及后推荐率 | {diagnosis.get('target_recommended_rate_when_mentioned', diagnosis.get('target_recommended_rate'))} |",
            f"| 全样本推荐率 | {diagnosis.get('target_recommended_rate_over_success', '0.0%')} |",
            f"| 排名观测率 | {diagnosis.get('target_rank_observed_rate', '0.0%')} |",
            f"| 情感未知率 | {diagnosis.get('target_sentiment_unknown_rate', '0.0%')} |",
            f"| Query 覆盖率 | {diagnosis.get('target_query_coverage_rate')} |",
            "",
        ])
    else:
        lines.append("目标品牌在当前抽取口径下未命中。建议优先检查 query 是否覆盖真实用户会触发该品牌的使用场景、target_aliases 是否完整，以及 raw response 中是否存在未被识别的别名。")
    missing = diagnosis.get("missing_queries") or []
    if missing and int(summary.get("success_record_count") or 0) > 0:
        lines.extend(["", "目标品牌缺失的 query：", ""])
        for item in missing[:10]:
            lines.append(f"- `{markdown_text(item['query_id'])}` {table_cell(item['query'])}")

    lines.extend(["", "## 6. Source & Citation Opportunities", ""])
    if summary.get("source_domains"):
        lines.extend(["| 来源域名 | 解析来源数 | 去重 URL 数 | 回答覆盖率 | Query 覆盖率 | 平均来源解析序号 | Top URLs |", "|---|---:|---:|---:|---:|---:|---|"])
        for row in summary["source_domains"][:15]:
            lines.append(
                f"| {table_cell(row['domain'])} | {row.get('parsed_source_occurrences', row.get('citation_occurrences'))} | "
                f"{row.get('distinct_source_url_count', '')} | {row['response_coverage_rate']} | {row['query_coverage_rate']} | "
                f"{row.get('avg_source_order', row.get('avg_rank'))} | {table_cell(row['top_urls'])} |"
            )
    else:
        lines.append("当前样本没有解析到来源引用。")

    lines.extend(["", "## 7. Query-Level Findings", ""])
    if summary.get("brand_by_query"):
        lines.extend([
            "| Query ID | 品牌/机构 | 提及回答数 | Query 内提及率 | 推荐回答数 | 提及后推荐率 |",
            "|---|---|---:|---:|---:|---:|",
        ])
        for row in summary["brand_by_query"][:30]:
            lines.append(
                f"| {table_cell(row['query_id'])} | {table_cell(row['brand_name_canonical'])} | "
                f"{row['responses_mentioned']} | {row['mention_rate_within_query']} | "
                f"{row['recommended_responses']} | {row.get('recommended_rate_when_mentioned_within_query', row['recommended_rate_within_query'])} |"
            )
    else:
        lines.append("暂无 query 级品牌命中数据。")

    if summary.get("query_stability"):
        lines.extend(["", "采样稳定性：", "", "| Query ID | 成功重复数 | 样本充足 | 品牌集合 Jaccard | Top Brands |", "|---|---:|---:|---:|---|"])
        for row in summary["query_stability"]:
            lines.append(f"| {table_cell(row['query_id'])} | {row['successful_repeats']} | {row['sample_sufficient']} | {row['brand_set_jaccard_avg']} | {table_cell(row['top_brands'])} |")

    lines.extend([
        "",
        "## 8. Methodology & Caveats",
        "",
        "- Runner 真实请求只发送 query 文本；目标品牌、行业和市场只用于任务记录与后处理。",
        "- 品牌发现来自 LLM 对 response_text 的开放式实体抽取，不依赖预置竞品 alias。",
        "- SOV 表示当前 LLM 回答样本内的品牌命中事件份额，不等同于真实市场份额或 App 端真实排名。",
        "- 来源表基于响应结构中的 source URL 解析，来源序号不等同于页面真实排名。",
        "- 推荐率、排名和情感来自回答文本语义抽取，低样本量或 partial sample 下应降低结论强度。",
        "- 目标品牌是否出现基于抽取与归一化结果，仍建议对关键样本人工复核。",
        "",
        "## 9. Output Files",
        "",
    ])
    for key, value in (summary.get("analysis_files") or {}).items():
        lines.append(f"- `{key}`: `{table_cell(value)}`")
    for key, value in (summary.get("report_files") or {}).items():
        lines.append(f"- 报告文件 `{key}`: `{table_cell(value)}`")
    for key, value in (summary.get("aggregate_files") or {}).items():
        lines.append(f"- 跨 job 聚合 `{key}`: `{table_cell(value)}`")
    lines.append("")
    return "\n".join(lines)


def update_cross_job_aggregates(bundle_dir: Path, summary: dict[str, Any]) -> dict[str, Path]:
    runs_root = _runs_root_for_bundle(bundle_dir)
    aggregate_dir = runs_root / "aggregate"
    runs_root.mkdir(parents=True, exist_ok=True)
    aggregate_dir.mkdir(parents=True, exist_ok=True)

    index_path = runs_root / "index.jsonl"
    brand_trends_path = aggregate_dir / "brand_trends.csv"
    target_trends_path = aggregate_dir / "target_brand_trends.csv"

    with _file_lock(runs_root / ".aggregate.lock"):
        _upsert_jsonl(index_path, _job_index_row(bundle_dir, summary), key="job_id")
        _upsert_csv_rows(
            brand_trends_path,
            [_brand_trend_row(summary, row) for row in summary.get("brand_summary", [])],
            key_fields=["job_id", "brand_name_canonical"],
            replace_fields={"job_id": _job_id(summary, bundle_dir)},
        )
        target_rows = [_brand_trend_row(summary, row, diagnosis=summary.get("target_diagnosis") or {}) for row in summary.get("brand_summary", []) if int(row.get("is_target_brand") or 0)]
        if not target_rows:
            target_rows = [_empty_target_trend_row(summary)]
        _upsert_csv_rows(target_trends_path, target_rows, key_fields=["job_id", "brand_name_canonical"], replace_fields={"job_id": _job_id(summary, bundle_dir)})

    return {
        "runs_index": index_path,
        "brand_trends": brand_trends_path,
        "target_brand_trends": target_trends_path,
    }


def evaluate_data_quality(
    raw_records: list[dict[str, Any]],
    analysis_records: list[dict[str, Any]],
    raw_read_errors: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    analysis_statuses: set[str],
) -> dict[str, Any]:
    expected_units = {(str(query["query_id"]), repeat) for query in manifest["queries"] for repeat in range(1, int(manifest["repeats"]) + 1)}
    missing_unknown_units_count = 0
    if manifest.get("_query_universe_incomplete"):
        missing_unknown_units_count = int(manifest.get("_missing_unknown_query_count") or 0) * int(manifest["repeats"])
    actual_units = {key for record in analysis_records if (key := safe_result_key(record)) is not None}
    manifest_ids = {str(query["query_id"]) for query in manifest["queries"]}
    manifest_queries = {str(query["query_id"]): str(query["query"]) for query in manifest["queries"]}
    raw_units: list[tuple[str, int, str]] = []
    invalid_records: list[dict[str, Any]] = []
    analysis_signatures = {_record_attempt_signature(record) for record in analysis_records if safe_result_key(record) is not None}
    raw_records_by_signature: dict[tuple[Any, ...], tuple[int, dict[str, Any]]] = {}
    contract_mismatches: list[dict[str, Any]] = []
    superseded_contract_mismatches: list[dict[str, Any]] = []
    for index, record in enumerate(raw_records, start=1):
        key = safe_result_key(record)
        status = str(record.get("status") or "")
        if key is None:
            invalid_records.append({"record_index": index, "reason": "invalid query_id/repeat_index", "status": status})
            continue
        qid, repeat = key
        raw_units.append((qid, repeat, status))
        signature = _record_attempt_signature(record)
        raw_records_by_signature[signature] = (index, record)
        if status in analysis_statuses and signature not in analysis_signatures:
            superseded_contract_mismatches.extend(_record_contract_mismatches(record, manifest, manifest_queries, qid, repeat, index))
    for record in analysis_records:
        key = safe_result_key(record)
        if key is None:
            continue
        qid, repeat = key
        signature = _record_attempt_signature(record)
        record_index, contract_record = raw_records_by_signature.get(signature, (0, record))
        contract_mismatches.extend(_record_contract_mismatches(contract_record, manifest, manifest_queries, qid, repeat, record_index))
    duplicate_counts = Counter((qid, repeat) for qid, repeat, status in raw_units if status in analysis_statuses)
    duplicate_units = [{"query_id": qid, "repeat_index": repeat, "count": count} for (qid, repeat), count in duplicate_counts.items() if count > 1]
    missing_units = [{"query_id": qid, "repeat_index": repeat} for qid, repeat in sorted(expected_units - actual_units)]
    extra_units = [{"query_id": qid, "repeat_index": repeat} for qid, repeat in sorted(actual_units - expected_units)]
    extra_query_ids = sorted({qid for qid, _, _ in raw_units if qid not in manifest_ids and qid != "None"})
    web_search_quality_flags = _web_search_quality_flags(analysis_records)
    source_quality_flags = _source_quality_flags(analysis_records)
    partial = bool(missing_units or missing_unknown_units_count or extra_units or duplicate_units or raw_read_errors or invalid_records or contract_mismatches or web_search_quality_flags or source_quality_flags)
    conclusion_strength = "observational" if partial else "strong"
    result = {
        "planned_units": len(expected_units) + missing_unknown_units_count,
        "analysis_record_count": len(analysis_records),
        "analysis_statuses": sorted(analysis_statuses),
        "partial_sample": partial,
        "conclusion_strength": conclusion_strength,
        "missing_units": missing_units,
        "extra_units": extra_units,
        "extra_query_ids": extra_query_ids,
        "duplicate_units": duplicate_units,
        "invalid_records": invalid_records,
        "contract_mismatches": contract_mismatches,
        "superseded_contract_mismatches": superseded_contract_mismatches,
        "web_search_quality_flags": web_search_quality_flags,
        "source_quality_flags": source_quality_flags,
        "raw_read_errors": raw_read_errors,
    }
    if missing_unknown_units_count:
        result["query_manifest_unavailable"] = True
        result["observed_query_count"] = int(manifest.get("_observed_query_count") or len(manifest["queries"]))
        result["expected_query_count"] = int(manifest.get("query_count") or len(manifest["queries"]))
        result["missing_unknown_units_count"] = missing_unknown_units_count
    return result


def _record_attempt_signature(record: dict[str, Any]) -> tuple[Any, ...]:
    key = safe_result_key(record)
    return (
        key,
        str(record.get("completed_at") or ""),
        str(record.get("attempt_id") or ""),
        str(canonical_request_hash(record) or ""),
        str(record.get("response_text") or ""),
    )


def _web_search_quality_flags(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for record in records:
        status = record.get("web_search_requirement_status")
        if status in (None, ""):
            continue
        if status in {"satisfied", "not_applicable"}:
            continue
        flags.append({
            "query_id": record.get("query_id", ""),
            "repeat_index": record.get("repeat_index", 1),
            "status": status,
            "evidence": record.get("web_search_evidence", ""),
        })
    return flags


def _source_quality_flags(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for record in records:
        status = record.get("source_parse_status")
        if status in (None, ""):
            continue
        if status in {"parsed", "provider_returned_empty", "unsupported_by_protocol", "not_applicable"}:
            continue
        flags.append({
            "query_id": record.get("query_id", ""),
            "repeat_index": record.get("repeat_index", 1),
            "status": status,
        })
    return flags


STAT_EXCLUDING_CONTRACT_FIELDS = {
    "input_query",
    "model",
    "repeat_total",
    "raw_request.model",
    "raw_request.input",
    "request_hash",
    "raw_response",
}


def _records_for_stats(records: list[dict[str, Any]], data_quality: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    expected_units = {(str(query["query_id"]), repeat) for query in manifest["queries"] for repeat in range(1, int(manifest["repeats"]) + 1)}
    excluded_units = {(str(item["query_id"]), int(item["repeat_index"])) for item in data_quality.get("extra_units", [])}
    for mismatch in data_quality.get("contract_mismatches", []):
        if mismatch.get("field") in STAT_EXCLUDING_CONTRACT_FIELDS:
            excluded_units.add((str(mismatch.get("query_id")), int(mismatch.get("repeat_index") or 1)))
    valid: list[dict[str, Any]] = []
    for record in records:
        key = safe_result_key(record)
        if key is None or key not in expected_units or key in excluded_units:
            continue
        valid.append(record)
    data_quality["stats_record_count"] = len(valid)
    data_quality["excluded_from_stats_count"] = max(0, len(records) - len(valid))
    if len(valid) != len(records):
        data_quality["partial_sample"] = True
        data_quality["conclusion_strength"] = "observational"
    return valid


def _record_contract_mismatches(
    record: dict[str, Any],
    manifest: dict[str, Any],
    manifest_queries: dict[str, str],
    qid: str,
    repeat: int,
    record_index: int,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    expected_query = manifest_queries.get(qid)
    raw_request = record.get("raw_request") if isinstance(record.get("raw_request"), dict) else {}
    raw_response = record.get("raw_response")
    if not isinstance(raw_response, dict) or not raw_response:
        mismatches.append(_contract_mismatch(record_index, qid, repeat, "raw_response", type(raw_response).__name__, "non-empty object"))
    checks = [
        ("input_query", record.get("input_query"), expected_query),
        ("model", record.get("model"), manifest.get("model")),
        ("repeat_total", record.get("repeat_total"), manifest.get("repeats")),
        ("raw_request.model", raw_request.get("model"), manifest.get("model")),
        ("raw_request.input", _raw_request_input(raw_request), expected_query),
    ]
    if raw_request:
        stored_hash = str(record.get("request_hash") or "")
        computed_hash = _record_request_hash(record, raw_request)
        if computed_hash and not stored_hash:
            mismatches.append(_contract_mismatch(record_index, qid, repeat, "request_hash", "", computed_hash))
        elif stored_hash and computed_hash and stored_hash != computed_hash:
            mismatches.append(_contract_mismatch(record_index, qid, repeat, "request_hash", stored_hash, computed_hash))
    for field, actual, expected in checks:
        if expected is None:
            continue
        if str(actual) != str(expected):
            mismatches.append(_contract_mismatch(record_index, qid, repeat, field, actual, expected))
    return mismatches


def _record_request_hash(record: dict[str, Any], raw_request: dict[str, Any]) -> str | None:
    if isinstance(record.get("request_fingerprint_basis"), dict) and record.get("request_fingerprint_version") == REQUEST_FINGERPRINT_VERSION:
        return request_fingerprint(record["request_fingerprint_basis"])
    if not raw_request:
        return None
    return legacy_payload_hash(raw_request)


def _raw_request_input(raw_request: dict[str, Any]) -> Any:
    if "input" in raw_request:
        return raw_request.get("input")
    messages = raw_request.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "user":
                return message.get("content")
    return None


def _contract_mismatch(record_index: int, qid: str, repeat: int, field: str, actual: Any, expected: Any) -> dict[str, Any]:
    return {
        "record_index": record_index,
        "query_id": qid,
        "repeat_index": repeat,
        "field": field,
        "actual": str(actual),
        "expected": str(expected),
    }


def _apply_target_alias_canonicalization(canonical_map: dict[str, str], raw_names: list[str], manifest: dict[str, Any]) -> dict[str, str]:
    target = str(manifest.get("target_brand") or "").strip()
    if not target:
        return canonical_map
    target_keys = {normalize_brand_name(target)}
    target_keys.update(normalize_brand_name(alias) for alias in manifest.get("target_aliases", []) if alias)
    merged = dict(canonical_map)
    target_canonicals = {
        canonical
        for raw, canonical in merged.items()
        if normalize_brand_name(raw) in target_keys or normalize_brand_name(canonical) in target_keys
    }
    for raw in raw_names:
        raw_key = normalize_brand_name(raw)
        canonical_key = normalize_brand_name(str(merged.get(raw) or ""))
        if raw_key in target_keys or canonical_key in target_keys or merged.get(raw) in target_canonicals:
            merged[raw] = target
    return merged


def _apply_extraction_quality(data_quality: dict[str, Any], errors: list[dict[str, Any]], analysis_record_count: int) -> None:
    error_row_count = len(errors)
    error_record_count = len(_error_record_keys(errors))
    data_quality["extraction_error_count"] = error_record_count
    data_quality["extraction_error_record_count"] = error_record_count
    data_quality["extraction_error_row_count"] = error_row_count
    data_quality["extraction_error_rate"] = _pct(error_record_count / (analysis_record_count or 1))
    quarantine_count = sum(1 for error in errors if str(error.get("reason") or error.get("type") or "") == "untraceable_extraction_item")
    if quarantine_count:
        data_quality["traceability_quarantine_count"] = quarantine_count
    if error_row_count:
        data_quality["partial_sample"] = True
        data_quality["conclusion_strength"] = "observational"


def _error_record_keys(errors: list[dict[str, Any]]) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    for index, error in enumerate(errors):
        query_id = str(error.get("query_id") or f"__unknown_{index}")
        try:
            repeat_index = int(error.get("repeat_index") or 1)
        except Exception:
            repeat_index = 1
        keys.add((query_id, repeat_index))
    return keys


def _empty_cache_stats() -> dict[str, Any]:
    return {
        "extraction_cache_hits": 0,
        "extraction_cache_misses": 0,
        "extraction_cache_writes": 0,
        "canonicalization_cache_hits": 0,
        "canonicalization_cache_misses": 0,
        "canonicalization_cache_writes": 0,
        "cache_load_error_count": 0,
        "analysis_llm_requests_remaining": 0,
    }


def _merge_cache_stats(base: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, int) and isinstance(base.get(key), int):
            base[key] += value
        else:
            base[key] = value


def _estimate_live_cache_requests(
    logs: Path,
    records: list[dict[str, Any]],
    *,
    extractor_model: str,
    refresh_extraction_cache: bool,
) -> dict[str, Any]:
    stats = _empty_cache_stats()
    if not records:
        return stats
    extraction_cache = JsonlCache(logs / "extraction_cache.jsonl")
    cached_rows: list[dict[str, Any]] = []
    for record in records:
        entry = None
        if not refresh_extraction_cache:
            entry = extraction_cache.get(
                extraction_cache_key(
                    response_text_hash_value=response_text_hash(record),
                    schema_version=EXTRACTION_SCHEMA_VERSION,
                    extractor_model=extractor_model,
                )
            )
        if entry is not None:
            rows = _cached_rows(entry, record)
        else:
            rows = None
        if rows is not None:
            stats["extraction_cache_hits"] += 1
            cached_rows.extend(rows)
        else:
            stats["extraction_cache_misses"] += 1
    stats["cache_load_error_count"] += extraction_cache.load_error_count
    if stats["extraction_cache_misses"]:
        stats["canonicalization_cache_misses"] = 1
    else:
        raw_names = [str(row.get("brand_name_raw") or "") for row in cached_rows if row.get("brand_name_raw")]
        if raw_names:
            canonical_cache = JsonlCache(logs / "canonicalization_cache.jsonl")
            entry = None
            if not refresh_extraction_cache:
                entry = canonical_cache.get(
                    canonicalization_cache_key(
                        sorted_raw_names_hash=raw_names_hash(raw_names),
                        canonicalizer_model=extractor_model,
                    )
                )
            if entry is not None:
                stats["canonicalization_cache_hits"] = 1
            else:
                stats["canonicalization_cache_misses"] = 1
            stats["cache_load_error_count"] += canonical_cache.load_error_count
    stats["analysis_llm_requests_remaining"] = stats["extraction_cache_misses"] + stats["canonicalization_cache_misses"]
    return stats


def _extract_mentions(
    *,
    records: list[dict[str, Any]],
    active_extractor: BrandMentionExtractor | None,
    settings: Settings,
    logs: Path,
    cache_enabled: bool,
    extractor_model: str,
    refresh_extraction_cache: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    rows_out: list[dict[str, Any]] = []
    errors_out: list[dict[str, Any]] = []
    stats = _empty_cache_stats()
    cache = JsonlCache(logs / "extraction_cache.jsonl") if cache_enabled else None
    for record in records:
        entry = None
        if cache is not None and not refresh_extraction_cache:
            entry = cache.get(
                extraction_cache_key(
                    response_text_hash_value=response_text_hash(record),
                    schema_version=EXTRACTION_SCHEMA_VERSION,
                    extractor_model=extractor_model,
                )
            )
        if entry is not None:
            rows = _cached_rows(entry, record)
        else:
            rows = None
        if rows is not None:
            stats["extraction_cache_hits"] += 1
            rows_out.extend(rows)
            errors_out.extend(_extraction_error_rows(entry.get("error"), settings, record=record))
            continue
        if cache is not None:
            stats["extraction_cache_misses"] += 1
        if active_extractor is None:
            raise ValueError("抽取缓存未命中，且没有可用 extractor；请使用 --confirm-cost 后重试")
        rows, error = active_extractor(record)
        rows_out.extend(rows)
        error_rows = _extraction_error_rows(error, settings, record=record)
        errors_out.extend(error_rows)
        if cache is not None and _cacheable_extraction_result(rows, error):
            cache.put(
                extraction_cache_entry(
                    record=record,
                    schema_version=EXTRACTION_SCHEMA_VERSION,
                    extractor_model=extractor_model,
                    rows=rows,
                    error=error,
                )
            )
            stats["extraction_cache_writes"] += 1
    if cache is not None:
        stats["cache_load_error_count"] += cache.load_error_count
    stats["analysis_llm_requests_remaining"] = stats["extraction_cache_misses"] + stats["canonicalization_cache_misses"]
    return rows_out, errors_out, stats


def _canonicalize_with_cache(
    *,
    raw_names: list[str],
    extractor_obj: LLMBrandExtractor,
    logs: Path,
    refresh_extraction_cache: bool,
) -> tuple[dict[str, str], dict[str, Any] | None, dict[str, Any]]:
    stats = _empty_cache_stats()
    cache = JsonlCache(logs / "canonicalization_cache.jsonl")
    key = canonicalization_cache_key(
        sorted_raw_names_hash=raw_names_hash(raw_names),
        canonicalizer_model=extractor_obj.model,
    )
    if not refresh_extraction_cache:
        entry = cache.get(key)
        if entry is not None and isinstance(entry.get("canonical_map"), dict):
            stats["canonicalization_cache_hits"] = 1
            stats["cache_load_error_count"] += cache.load_error_count
            return {str(k): str(v) for k, v in entry["canonical_map"].items()}, None, stats
    stats["canonicalization_cache_misses"] = 1
    canonical_map, canonical_error = extractor_obj.canonicalize(raw_names)
    if canonical_error is None:
        cache.put(
            canonicalization_cache_entry(
                raw_names=raw_names,
                canonicalizer_model=extractor_obj.model,
                canonical_map=canonical_map,
            )
        )
        stats["canonicalization_cache_writes"] = 1
    stats["cache_load_error_count"] += cache.load_error_count
    stats["analysis_llm_requests_remaining"] = stats["canonicalization_cache_misses"]
    return canonical_map, canonical_error, stats


def _canonicalize_from_cache_only(
    *,
    raw_names: list[str],
    canonicalizer_model: str,
    logs: Path,
    refresh_extraction_cache: bool,
) -> tuple[dict[str, str], dict[str, Any] | None, dict[str, Any]]:
    stats = _empty_cache_stats()
    cache = JsonlCache(logs / "canonicalization_cache.jsonl")
    entry = None
    if not refresh_extraction_cache:
        entry = cache.get(
            canonicalization_cache_key(
                sorted_raw_names_hash=raw_names_hash(raw_names),
                canonicalizer_model=canonicalizer_model,
            )
        )
    if entry is not None and isinstance(entry.get("canonical_map"), dict):
        stats["canonicalization_cache_hits"] = 1
        stats["cache_load_error_count"] += cache.load_error_count
        return {str(k): str(v) for k, v in entry["canonical_map"].items()}, None, stats
    stats["canonicalization_cache_misses"] = 1
    stats["analysis_llm_requests_remaining"] = 1
    raise ValueError("归一化缓存未命中；请使用 --confirm-cost 后重试")


def _cached_rows(entry: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]] | None:
    rows = entry.get("rows")
    if not isinstance(rows, list):
        return None
    rebound: list[dict[str, Any]] = []
    response_text = str(record.get("response_text") or "")
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_name = str(row.get("brand_name_raw") or "")
        if response_text and raw_name and normalize_brand_name(raw_name) not in normalize_brand_name(response_text):
            return None
        copy = dict(row)
        copy["query_id"] = str(record.get("query_id"))
        copy["repeat_index"] = int(record.get("repeat_index") or 1)
        copy["input_query"] = record.get("input_query", "")
        rebound.append(copy)
    return rebound


def _cacheable_extraction_result(rows: list[dict[str, Any]], error: dict[str, Any] | None) -> bool:
    if error is None:
        return True
    return str(error.get("type") or "") == "TraceabilityQuarantine"


def _extraction_error_rows(error: dict[str, Any] | None, settings: Settings | None, *, record: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if not error:
        return []
    redacted = _redacted_error(error, settings)
    if str(redacted.get("type") or "") == "TraceabilityQuarantine" and isinstance(redacted.get("quarantined_rows"), list):
        rows = []
        query_id = str(record.get("query_id")) if record else redacted.get("query_id")
        repeat_index = int(record.get("repeat_index") or 1) if record else redacted.get("repeat_index") or 1
        input_query = record.get("input_query", "") if record else ""
        for row in redacted["quarantined_rows"]:
            if not isinstance(row, dict):
                continue
            rows.append({
                "type": "TraceabilityQuarantine",
                "message": redacted.get("message", ""),
                "query_id": query_id,
                "repeat_index": repeat_index,
                "input_query": input_query or row.get("input_query", ""),
                "brand_name_raw": row.get("brand_name_raw", ""),
                "evidence": row.get("evidence", ""),
                "reason": row.get("reason", "untraceable_extraction_item"),
            })
        return rows
    if record:
        redacted.setdefault("query_id", record.get("query_id"))
        redacted.setdefault("repeat_index", record.get("repeat_index") or 1)
    return [redacted]


def demo_extract_record(record: dict[str, Any], manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    qid = str(record.get("query_id"))
    repeat = int(record.get("repeat_index") or 1)
    query = str(record.get("input_query") or "")
    target = str(manifest.get("target_brand") or "MockTarget")
    rows = [{
        "query_id": qid,
        "repeat_index": repeat,
        "input_query": query,
        "brand_name_raw": target,
        "brand_type": "品牌",
        "evidence": "mock demo sample",
        "role": "recommended" if repeat == 1 else "mentioned",
        "confidence": 1.0,
        "is_recommended": repeat == 1,
        "rank_position": 1 if repeat == 1 else "",
        "sentiment": "neutral",
        "mention_context": "answer",
        "sov_eligible": True,
        "canonical_hint": target,
    }]
    if repeat == 1:
        rows.append({
            "query_id": qid,
            "repeat_index": repeat,
            "input_query": query,
            "brand_name_raw": "MockPeer",
            "brand_type": "品牌",
            "evidence": "mock peer sample",
            "role": "mentioned",
            "confidence": 1.0,
            "is_recommended": False,
            "rank_position": 2,
            "sentiment": "neutral",
            "mention_context": "answer",
            "sov_eligible": True,
            "canonical_hint": "MockPeer",
        })
    return rows, None


def compute_source_stats(records: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    domain_occ: Counter = Counter()
    domain_responses: dict[str, set[str]] = defaultdict(set)
    domain_queries: dict[str, set[str]] = defaultdict(set)
    domain_ranks: dict[str, list[int]] = defaultdict(list)
    domain_urls: dict[str, Counter] = defaultdict(Counter)
    url_occ: Counter = Counter()
    url_meta: dict[str, dict[str, str]] = {}
    by_query_domain: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"repeats": set(), "occ": 0, "ranks": [], "urls": Counter()})

    for idx, record in enumerate(records):
        qid = str(record.get("query_id"))
        rep = int(record.get("repeat_index") or 1)
        response_key = f"{qid}#{rep}#{idx}"
        seen_sources: set[tuple[str, str]] = set()
        for source in record.get("sources", []) or []:
            if hasattr(source, "model_dump"):
                source = source.model_dump(mode="json")
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "")
            domain = _normalize_source_domain(str(source.get("domain") or ""), url)
            source_key = (domain, url)
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            title = str(source.get("title") or "")
            rank = _as_positive_int(source.get("rank"))
            domain_occ[domain] += 1
            domain_responses[domain].add(response_key)
            domain_queries[domain].add(qid)
            if rank is not None:
                domain_ranks[domain].append(rank)
            if url:
                domain_urls[domain][url] += 1
                url_occ[url] += 1
                url_meta[url] = {"domain": domain, "title": title}
            cell = by_query_domain[(qid, domain)]
            cell["repeats"].add(rep)
            cell["occ"] += 1
            if rank is not None:
                cell["ranks"].append(rank)
            if url:
                cell["urls"][url] += 1

    total_records = len(records) or 1
    query_count = int(manifest.get("query_count") or len({record.get("query_id") for record in records}) or 1)
    domain_rows = []
    for domain, count in domain_occ.most_common():
        domain_rows.append({
            "domain": domain,
            "citation_occurrences": count,
            "parsed_source_occurrences": count,
            "response_coverage": len(domain_responses[domain]),
            "response_coverage_rate": _pct(len(domain_responses[domain]) / total_records),
            "query_coverage": len(domain_queries[domain]),
            "query_coverage_rate": _pct(len(domain_queries[domain]) / query_count),
            "avg_rank": round(statistics.mean(domain_ranks[domain]), 2) if domain_ranks[domain] else "",
            "avg_source_order": round(statistics.mean(domain_ranks[domain]), 2) if domain_ranks[domain] else "",
            "best_rank": min(domain_ranks[domain]) if domain_ranks[domain] else "",
            "best_source_order": min(domain_ranks[domain]) if domain_ranks[domain] else "",
            "distinct_source_url_count": len(domain_urls[domain]),
            "top_urls": " | ".join(url for url, _ in domain_urls[domain].most_common(3)),
        })
    url_rows = [
        {
            "url": url,
            "domain": url_meta[url]["domain"],
                "title": url_meta[url]["title"],
                "citation_occurrences": count,
                "parsed_source_occurrences": count,
            }
        for url, count in url_occ.most_common()
    ]
    by_query_rows = []
    for (qid, domain), cell in sorted(by_query_domain.items()):
        qrecs = [record for record in records if record.get("query_id") == qid]
        by_query_rows.append({
            "query_id": qid,
            "domain": domain,
            "repeat_coverage": len(cell["repeats"]),
            "repeat_coverage_rate": _pct(len(cell["repeats"]) / (len(qrecs) or 1)),
            "citation_occurrences": cell["occ"],
            "parsed_source_occurrences": cell["occ"],
            "avg_rank": round(statistics.mean(cell["ranks"]), 2) if cell["ranks"] else "",
            "avg_source_order": round(statistics.mean(cell["ranks"]), 2) if cell["ranks"] else "",
            "distinct_source_url_count": len(cell["urls"]),
            "top_urls": " | ".join(url for url, _ in cell["urls"].most_common(3)),
        })
    return {"source_domains": domain_rows, "source_urls": url_rows, "source_by_query": by_query_rows}


def _redacted_error(error: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    copy = dict(error)
    if copy.get("message"):
        copy["message"] = redact_secret(str(copy["message"]), settings)
    return copy


def _is_brand_sov_candidate(row: dict[str, Any]) -> bool:
    eligible = _coerce_optional_bool(row.get("sov_eligible"))
    if eligible is False:
        return False
    brand_type = str(row.get("brand_type") or "").strip().lower()
    context = str(row.get("mention_context") or "").strip().lower()
    role = str(row.get("role") or "").strip().lower()
    if context == "source" or role == "source":
        return False
    if not brand_type:
        return False
    excluded_types = {
        "媒体",
        "来源",
        "协会",
        "政府",
        "平台",
        "榜单",
        "奖项",
        "其他",
        "source",
        "media",
        "publisher",
        "association",
        "government",
        "platform",
        "ranking",
        "award",
        "other",
    }
    allowed_types = {
        "品牌",
        "公司",
        "企业",
        "厂商",
        "商家",
        "机构",
        "设计机构",
        "装修公司",
        "工作室",
        "brand",
        "company",
        "business",
        "vendor",
        "institution",
        "agency",
        "studio",
    }
    if brand_type in excluded_types:
        return False
    return brand_type in allowed_types


def _coerce_optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "是", "推荐"}:
            return True
        if text in {"false", "0", "no", "n", "否", "未推荐"}:
            return False
    return None


def _write_csv(path: Path, rows: list[dict[str, Any]], *, schema: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    base_fields = CSV_FIELD_SCHEMAS.get(schema or "", [])
    row_fields = {key for row in rows for key in row.keys()}
    fieldnames = list(base_fields) if base_fields else sorted(row_fields)
    if not fieldnames:
        fieldnames = ["empty"]
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(sanitize_csv_row(row))
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _pct_points(value: float) -> str:
    return f"{max(0.0, value):.1f}pp"


def _pct_to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().rstrip("%")
    try:
        return float(text)
    except Exception:
        return 0.0


def _rank_sort_value(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 999999.0


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "是", "推荐"}
    return False


def _as_positive_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _dominant_sentiment(counter: Counter) -> str:
    if not counter:
        return "unknown"
    priority = {"positive": 0, "neutral": 1, "negative": 2, "unknown": 3}
    return sorted(counter.items(), key=lambda item: (-item[1], priority.get(item[0], 9)))[0][0]


def _fmt_float(value: float | None) -> object:
    return round(value, 3) if value is not None else ""


def _avg_pairwise_jaccard(sets: list[set], *, treat_empty_as_missing: bool = False) -> float | None:
    if len(sets) < 2:
        return None
    values = []
    for a, b in combinations(sets, 2):
        if treat_empty_as_missing and (not a or not b):
            continue
        values.append(1.0 if not a and not b else len(a & b) / len(a | b))
    return sum(values) / len(values) if values else None


def _top_items(items: list[str], limit: int = 8) -> str:
    return " | ".join(f"{item}:{count}" for item, count in Counter(items).most_common(limit))


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
            "target_sov_event_share": "0.0%",
            "target_sov_response_share": "0.0%",
            "target_rank_by_sov": "",
            "target_sov_gap_to_leader": "",
            "target_sov_gap_to_top3_avg": "",
            "target_response_mention_rate": "0.0%",
            "target_recommended_rate": "0.0%",
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
        query_details.append({
            "query_id": qid,
            "query": query_text.get(qid, ""),
            "target_mentions": len(reps),
            "target_mention_rate": _pct(len(reps) / expected_repeats) if expected_repeats else "0.0%",
            "target_recommendations": len(recs),
            "target_recommendation_rate": _pct(len(recs) / len(reps)) if reps else "0.0%",
            "target_recommendation_rate_over_success": _pct(len(recs) / expected_repeats) if expected_repeats else "0.0%",
        })
    return {
        "target_brand": manifest["target_brand"],
        "target_detected": True,
        "target_canonical_name": canonical,
        "target_sov_event_share": target_row["sov_event_share"],
        "target_sov_response_share": target_row["sov_response_share"],
        "target_rank_by_sov": target_row["target_rank_by_sov"],
        "target_sov_gap_to_leader": target_row["target_sov_gap_to_leader"],
        "target_sov_gap_to_top3_avg": target_row["target_sov_gap_to_top3_avg"],
        "target_response_mention_rate": target_row["response_mention_rate"],
        "target_recommended_rate": target_row["recommended_rate"],
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


def _runs_root_for_bundle(bundle_dir: Path) -> Path:
    if bundle_dir.parent.name in {RUNS_DIR, "runs"}:
        return bundle_dir.parent
    project_runs = workspace_root() / RUNS_DIR
    try:
        bundle_dir.resolve().relative_to(project_runs.resolve())
        return project_runs
    except Exception:
        return bundle_dir.parent / RUNS_DIR


def _job_index_row(bundle_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    diagnosis = summary.get("target_diagnosis") or {}
    top_brands = " | ".join(row["brand_name_canonical"] for row in summary.get("brand_summary", [])[:5])
    manifest = load_job_manifest(bundle_dir)
    return {
        "job_id": _job_id(summary, bundle_dir),
        "bundle_dir": str(bundle_dir),
        "target_brand": summary.get("target_brand", ""),
        "industry": summary.get("industry", ""),
        "market": summary.get("market", ""),
        "model": manifest.get("model", ""),
        "query_set_hash": query_set_hash(manifest),
        "web_search_limit": manifest.get("web_search_limit", ""),
        "sample_mode": summary.get("sample_mode", "live"),
        "expected_queries": summary.get("expected_queries", 0),
        "expected_repeats": summary.get("expected_repeats", 0),
        "conclusion_strength": (summary.get("data_quality") or {}).get("conclusion_strength", ""),
        "extraction_error_rate": summary.get("extraction_error_rate", "0.0%"),
        "comparability_key": _comparability_key(summary),
        "success_record_count": summary.get("success_record_count", 0),
        "partial_sample": summary.get("partial_sample", False),
        "target_brand_detected": summary.get("target_brand_detected", False),
        "target_sov_event_share": diagnosis.get("target_sov_event_share", diagnosis.get("target_sov_response_share", "0.0%")),
        "target_sov_response_share": diagnosis.get("target_sov_response_share", "0.0%"),
        "target_rank_by_sov": diagnosis.get("target_rank_by_sov", ""),
        "target_sov_gap_to_leader": diagnosis.get("target_sov_gap_to_leader", ""),
        "top_brands": top_brands,
    }


def _brand_trend_row(summary: dict[str, Any], row: dict[str, Any], *, diagnosis: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = {"queries": [{"query_id": qid, "query": ""} for qid in summary.get("query_ids", [])]}
    diagnosis = diagnosis or {}
    return {
        "job_id": _job_id(summary),
        "target_brand": summary.get("target_brand", ""),
        "industry": summary.get("industry", ""),
        "market": summary.get("market", ""),
        "model": summary.get("model", ""),
        "web_search_limit": summary.get("web_search_limit", ""),
        "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
        "expected_queries": summary.get("expected_queries", 0),
        "expected_repeats": summary.get("expected_repeats", 0),
        "sample_mode": summary.get("sample_mode", "live"),
        "query_set_hash": summary.get("query_set_hash", query_set_hash(manifest)),
        "conclusion_strength": (summary.get("data_quality") or {}).get("conclusion_strength", ""),
        "extraction_error_rate": summary.get("extraction_error_rate", "0.0%"),
        "comparability_key": _comparability_key(summary),
        "partial_sample": summary.get("partial_sample", False),
        "brand_name_canonical": row.get("brand_name_canonical", ""),
        "is_target_brand": row.get("is_target_brand", 0),
        "sov_rank": row.get("sov_rank", ""),
        "sov_event_share": row.get("sov_event_share", "0.0%"),
        "sov_response_share": row.get("sov_response_share", "0.0%"),
        "response_mention_rate": row.get("response_mention_rate", "0.0%"),
        "query_coverage_rate": row.get("query_coverage_rate", "0.0%"),
        "recommended_rate": row.get("recommended_rate", "0.0%"),
        "recommended_rate_when_mentioned": row.get("recommended_rate_when_mentioned", "0.0%"),
        "recommended_rate_over_success": row.get("recommended_rate_over_success", "0.0%"),
        "rank_observed_rate": row.get("rank_observed_rate", "0.0%"),
        "sentiment_unknown_rate": row.get("sentiment_unknown_rate", "0.0%"),
        "avg_rank_position": row.get("avg_rank_position", ""),
        "positive_rate": row.get("positive_rate", "0.0%"),
        "neutral_rate": row.get("neutral_rate", "0.0%"),
        "negative_rate": row.get("negative_rate", "0.0%"),
        "target_sov_gap_to_leader": diagnosis.get("target_sov_gap_to_leader", "") if int(row.get("is_target_brand") or 0) else "",
        "target_sov_gap_to_top3_avg": diagnosis.get("target_sov_gap_to_top3_avg", "") if int(row.get("is_target_brand") or 0) else "",
        "success_record_count": summary.get("success_record_count", 0),
    }


def _empty_target_trend_row(summary: dict[str, Any]) -> dict[str, Any]:
    manifest = {"queries": [{"query_id": qid, "query": ""} for qid in summary.get("query_ids", [])]}
    return {
        "job_id": _job_id(summary),
        "target_brand": summary.get("target_brand", ""),
        "industry": summary.get("industry", ""),
        "market": summary.get("market", ""),
        "model": summary.get("model", ""),
        "web_search_limit": summary.get("web_search_limit", ""),
        "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
        "expected_queries": summary.get("expected_queries", 0),
        "expected_repeats": summary.get("expected_repeats", 0),
        "sample_mode": summary.get("sample_mode", "live"),
        "query_set_hash": summary.get("query_set_hash", query_set_hash(manifest)),
        "conclusion_strength": (summary.get("data_quality") or {}).get("conclusion_strength", ""),
        "extraction_error_rate": summary.get("extraction_error_rate", "0.0%"),
        "comparability_key": _comparability_key(summary),
        "partial_sample": summary.get("partial_sample", False),
        "brand_name_canonical": summary.get("target_brand", ""),
        "is_target_brand": 1,
        "sov_rank": "",
        "sov_event_share": "0.0%",
        "sov_response_share": "0.0%",
        "response_mention_rate": "0.0%",
        "query_coverage_rate": "0.0%",
        "recommended_rate": "0.0%",
        "recommended_rate_when_mentioned": "0.0%",
        "recommended_rate_over_success": "0.0%",
        "rank_observed_rate": "0.0%",
        "sentiment_unknown_rate": "0.0%",
        "avg_rank_position": "",
        "positive_rate": "0.0%",
        "neutral_rate": "0.0%",
        "negative_rate": "0.0%",
        "target_sov_gap_to_leader": "",
        "target_sov_gap_to_top3_avg": "",
        "success_record_count": summary.get("success_record_count", 0),
    }


def _job_id(summary: dict[str, Any], bundle_dir: Path | None = None) -> str:
    if summary.get("job_id"):
        return str(summary["job_id"])
    if bundle_dir is not None:
        try:
            return str(load_job_manifest(bundle_dir).get("job_id") or bundle_dir.name)
        except Exception:
            return bundle_dir.name
    return ""


def _comparability_key(summary: dict[str, Any]) -> str:
    parts = [
        str(summary.get("model", "")),
        str(summary.get("target_brand", "")),
        ",".join(sorted(str(alias) for alias in (summary.get("target_aliases") or []))),
        str(summary.get("query_set_hash", "")),
        str(summary.get("web_search_limit", "")),
        str(summary.get("expected_queries", "")),
        str(summary.get("expected_repeats", "")),
        str(summary.get("sample_mode", "")),
        EXTRACTION_SCHEMA_VERSION,
    ]
    return "|".join(parts)


def _normalize_source_domain(domain: str, url: str = "") -> str:
    candidate = (domain or "").strip().lower()
    if not candidate and url:
        candidate = urlparse(url).netloc.lower()
    if ":" in candidate:
        host, _, port = candidate.partition(":")
        if port in {"80", "443"}:
            candidate = host
    if candidate.startswith("www."):
        candidate = candidate[4:]
    return candidate or "unknown"


def _upsert_jsonl(path: Path, row: dict[str, Any], *, key: str) -> None:
    rows = read_jsonl(path) if path.exists() else []
    keyed = {str(item.get(key)): item for item in rows if item.get(key) is not None}
    keyed[str(row[key])] = row
    _write_jsonl_atomic(path, sorted(keyed.values(), key=lambda item: str(item.get(key))))


def _upsert_csv_rows(path: Path, rows: list[dict[str, Any]], *, key_fields: list[str], replace_fields: dict[str, Any] | None = None) -> None:
    existing = _read_csv_rows(path) if path.exists() else []
    if replace_fields:
        existing = [
            row
            for row in existing
            if not all(str(row.get(field, "")) == str(value) for field, value in replace_fields.items())
        ]
    merged = {tuple(str(row.get(field, "")) for field in key_fields): row for row in existing}
    for row in rows:
        merged[tuple(str(row.get(field, "")) for field in key_fields)] = row
    output_rows = sorted(merged.values(), key=lambda row: tuple(str(row.get(field, "")) for field in key_fields))
    schema = "brand_trends" if path.stem in {"brand_trends", "target_brand_trends"} else None
    _write_csv(path, output_rows, schema=schema)


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class _file_lock:
    def __init__(self, path: Path, *, timeout_seconds: float = 10.0, stale_seconds: float = 12 * 60 * 60):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self.fd: int | None = None

    def __enter__(self) -> "_file_lock":
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("utf-8"))
                return self
            except FileExistsError:
                if self._is_stale():
                    try:
                        self.path.unlink()
                        continue
                    except FileNotFoundError:
                        continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待聚合文件锁超时：{self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _is_stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > self.stale_seconds
        except FileNotFoundError:
            return False


def _write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        write_jsonl(tmp_path, rows)
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return path.name


def _display_path(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        pass
    try:
        return str(path.resolve().relative_to(workspace_root().resolve()))
    except Exception:
        return path.name
