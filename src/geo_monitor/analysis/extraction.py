"""Extract and canonicalize brand mentions with auditable local caches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..brand_extraction import BrandMentionExtractor, LLMBrandExtractor, is_traceable_text, is_valid_canonical_map, normalize_brand_name
from ..config import Settings, redact_secret
from .cache import (
    JsonlCache,
    canonicalization_cache_entry,
    canonicalization_cache_key,
    extraction_cache_entry,
    extraction_cache_key,
    raw_names_hash,
    response_text_hash,
)
from .contracts import EXTRACTION_SCHEMA_VERSION


def apply_target_alias_canonicalization(canonical_map: dict[str, str], raw_names: list[str], manifest: dict[str, Any]) -> dict[str, str]:
    target = str(manifest.get("target_brand") or "").strip()
    if not target:
        return canonical_map
    target_keys = {normalize_brand_name(target)}
    target_keys.update(normalize_brand_name(alias) for alias in manifest.get("target_aliases", []) if alias)
    merged = dict(canonical_map)
    target_canonicals = {
        canonical for raw, canonical in merged.items() if normalize_brand_name(raw) in target_keys or normalize_brand_name(canonical) in target_keys
    }
    for raw in raw_names:
        raw_key = normalize_brand_name(raw)
        canonical_key = normalize_brand_name(str(merged.get(raw) or ""))
        if raw_key in target_keys or canonical_key in target_keys or merged.get(raw) in target_canonicals:
            merged[raw] = target
    return merged


def empty_cache_stats() -> dict[str, Any]:
    return {
        "extraction_cache_hits": 0,
        "extraction_cache_misses": 0,
        "extraction_cache_writes": 0,
        "canonicalization_cache_hits": 0,
        "canonicalization_cache_misses": 0,
        "canonicalization_cache_writes": 0,
        "cache_load_error_count": 0,
        "cache_validation_error_count": 0,
        "analysis_llm_requests_remaining": 0,
        "analysis_circuit_breaker": False,
        "analysis_circuit_breaker_reason": "",
        "analysis_not_started_count": 0,
    }


def merge_cache_stats(base: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, bool):
            base[key] = bool(base.get(key)) or value
        elif isinstance(value, int) and isinstance(base.get(key), int) and not isinstance(base.get(key), bool):
            base[key] += value
        else:
            base[key] = value


def estimate_live_cache_requests(
    logs: Path,
    records: list[dict[str, Any]],
    *,
    extractor_model: str,
    refresh_extraction_cache: bool,
) -> dict[str, Any]:
    stats = empty_cache_stats()
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
            if entry is not None:
                stats["cache_validation_error_count"] += 1
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
            if entry is not None and is_valid_canonical_map(entry.get("canonical_map"), raw_names):
                stats["canonicalization_cache_hits"] = 1
            else:
                stats["canonicalization_cache_misses"] = 1
                if entry is not None:
                    stats["cache_validation_error_count"] += 1
            stats["cache_load_error_count"] += canonical_cache.load_error_count
    stats["analysis_llm_requests_remaining"] = stats["extraction_cache_misses"] + stats["canonicalization_cache_misses"]
    return stats


def extract_mentions(
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
    stats = empty_cache_stats()
    cache = JsonlCache(logs / "extraction_cache.jsonl") if cache_enabled else None
    observed_calls = 0
    failed_calls = 0
    consecutive_failures = 0
    for record_index, record in enumerate(records):
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
            if entry is not None:
                stats["cache_validation_error_count"] += 1
        if active_extractor is None:
            raise ValueError("抽取缓存未命中，且没有可用 extractor；请使用 --confirm-cost 后重试")
        rows, error = active_extractor(record)
        observed_calls += 1
        hard_failure = bool(error) and str((error or {}).get("type") or "") != "TraceabilityQuarantine"
        if hard_failure:
            failed_calls += 1
            consecutive_failures += 1
        else:
            consecutive_failures = 0
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
        breaker_reason = ""
        if consecutive_failures >= settings.max_consecutive_errors:
            breaker_reason = "consecutive_errors"
        elif observed_calls >= 5 and failed_calls / observed_calls >= settings.max_error_rate:
            breaker_reason = "error_rate"
        if breaker_reason:
            remaining = records[record_index + 1 :]
            stats["analysis_circuit_breaker"] = True
            stats["analysis_circuit_breaker_reason"] = breaker_reason
            stats["analysis_not_started_count"] = len(remaining)
            for skipped in remaining:
                errors_out.append(
                    {
                        "type": "AnalysisCircuitBreaker",
                        "message": f"分析抽取熔断：{breaker_reason}",
                        "query_id": skipped.get("query_id"),
                        "repeat_index": skipped.get("repeat_index") or 1,
                        "reason": "analysis_not_started",
                    }
                )
            break
    if cache is not None:
        stats["cache_load_error_count"] += cache.load_error_count
    stats["analysis_llm_requests_remaining"] = stats["extraction_cache_misses"] + stats["canonicalization_cache_misses"]
    return rows_out, errors_out, stats


def canonicalize_with_cache(
    *,
    raw_names: list[str],
    extractor_obj: LLMBrandExtractor,
    logs: Path,
    refresh_extraction_cache: bool,
) -> tuple[dict[str, str], dict[str, Any] | None, dict[str, Any]]:
    stats = empty_cache_stats()
    cache = JsonlCache(logs / "canonicalization_cache.jsonl")
    key = canonicalization_cache_key(
        sorted_raw_names_hash=raw_names_hash(raw_names),
        canonicalizer_model=extractor_obj.model,
    )
    if not refresh_extraction_cache:
        entry = cache.get(key)
        if entry is not None and is_valid_canonical_map(entry.get("canonical_map"), raw_names):
            stats["canonicalization_cache_hits"] = 1
            stats["cache_load_error_count"] += cache.load_error_count
            return {str(k): str(v) for k, v in entry["canonical_map"].items()}, None, stats
        if entry is not None:
            stats["cache_validation_error_count"] += 1
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


def canonicalize_from_cache_only(
    *,
    raw_names: list[str],
    canonicalizer_model: str,
    logs: Path,
    refresh_extraction_cache: bool,
) -> tuple[dict[str, str], dict[str, Any] | None, dict[str, Any]]:
    stats = empty_cache_stats()
    cache = JsonlCache(logs / "canonicalization_cache.jsonl")
    entry = None
    if not refresh_extraction_cache:
        entry = cache.get(
            canonicalization_cache_key(
                sorted_raw_names_hash=raw_names_hash(raw_names),
                canonicalizer_model=canonicalizer_model,
            )
        )
    if entry is not None and is_valid_canonical_map(entry.get("canonical_map"), raw_names):
        stats["canonicalization_cache_hits"] = 1
        stats["cache_load_error_count"] += cache.load_error_count
        return {str(k): str(v) for k, v in entry["canonical_map"].items()}, None, stats
    if entry is not None:
        stats["cache_validation_error_count"] += 1
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
            return None
        raw_name = str(row.get("brand_name_raw") or "")
        if not raw_name:
            return None
        if response_text and not is_traceable_text(response_text, raw_name):
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
    redacted = redacted_error(error, settings)
    if str(redacted.get("type") or "") == "TraceabilityQuarantine" and isinstance(redacted.get("quarantined_rows"), list):
        rows = []
        query_id = str(record.get("query_id")) if record else redacted.get("query_id")
        repeat_index = int(record.get("repeat_index") or 1) if record else redacted.get("repeat_index") or 1
        input_query = record.get("input_query", "") if record else ""
        for row in redacted["quarantined_rows"]:
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    "type": "TraceabilityQuarantine",
                    "message": redacted.get("message", ""),
                    "query_id": query_id,
                    "repeat_index": repeat_index,
                    "input_query": input_query or row.get("input_query", ""),
                    "brand_name_raw": row.get("brand_name_raw", ""),
                    "evidence": row.get("evidence", ""),
                    "reason": row.get("reason", "untraceable_extraction_item"),
                    "claim_type": row.get("claim_type", ""),
                    "claim_text": row.get("claim_text", ""),
                }
            )
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
    rows = [
        {
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
        }
    ]
    if repeat == 1:
        rows.append(
            {
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
            }
        )
    return rows, None


def redacted_error(error: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    copy = dict(error)
    if copy.get("message"):
        copy["message"] = redact_secret(str(copy["message"]), settings)
    return copy
