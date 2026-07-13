"""Coordinate one analysis run without owning extraction, scoring, or persistence logic."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..brand_extraction import (
    BrandCanonicalizer,
    BrandMentionExtractor,
    LLMBrandExtractor,
    fallback_canonicalize,
)
from ..config import LiveSettingsError, Settings, get_settings, redact_secret, validate_live_settings
from ..exporters import (
    latest_live_terminal_records,
    latest_records,
    read_jsonl,
    read_jsonl_with_errors,
)
from ..filesystem import ensure_private_directory
from ..jobs.cleanup import cleanup_job_work_dir_unlocked
from ..jobs.contracts import QueryManifestIntegrityError, QueryManifestSourceError
from ..jobs.layout import logs_dir, raw_attempts_path, result_dir, work_dir
from ..jobs.locking import job_bundle_lock
from ..jobs.manifest import load_job_manifest, query_set_hash, update_job_manifest
from ..jobs.query_manifest import load_job_queries
from ..query_meta import query_metadata_json, tags_text
from ..reporting import render_report_bundle
from ..request_fingerprint import base_url_fingerprint
from ..schemas import utc_now_iso
from .aggregates import cross_job_aggregate_paths, update_cross_job_aggregates
from .artifacts import (
    build_analysis_artifact_manifest,
    display_path,
    relative_path,
    write_job_analysis_files,
    write_json_atomic,
    write_jsonl_atomic,
)
from .brand_metrics import compute_open_brand_stats
from .denominator_facts import build_fact_rows
from .extraction import (
    apply_target_alias_canonicalization,
    canonicalize_from_cache_only,
    canonicalize_with_cache,
    demo_extract_record,
    empty_cache_stats,
    estimate_live_cache_requests,
    extract_mentions,
    merge_cache_stats,
    redacted_error,
)
from .history import load_intelligence_history
from .intelligence import INTELLIGENCE_SCHEMA_VERSION, build_intelligence_outputs
from .quality import apply_extraction_quality, evaluate_data_quality, records_for_stats
from .source_metrics import compute_source_stats


def _analysis_terminal_records(raw_records: list[dict[str, Any]], *, include_mock: bool) -> list[dict[str, Any]]:
    live_terminal = latest_live_terminal_records(raw_records)
    if live_terminal:
        return live_terminal
    if include_mock:
        mock_records = [record for record in raw_records if str(record.get("execution_mode") or "") == "mock" or record.get("status") == "mock"]
        return latest_records(mock_records, statuses={"mock", "error"})
    return live_terminal


def estimate_job_analysis(bundle_dir: str | Path, *, include_mock: bool = False, refresh_extraction_cache: bool = False) -> dict[str, Any]:
    root = Path(bundle_dir)
    manifest = load_job_manifest(root)
    raw_path = raw_attempts_path(root)
    if not raw_path.exists():
        raise ValueError(f"缺少 raw attempts：{raw_path}")
    if raw_path.is_symlink() or not raw_path.is_file():
        raise ValueError(f"raw attempts 必须是普通非 symlink 文件：{raw_path}")
    raw_records, raw_read_errors = read_jsonl_with_errors(raw_path)
    manifest = _manifest_with_queries_for_analysis(root, manifest, raw_records)
    analysis_profile = _analysis_profile(manifest)
    terminal_records = _analysis_terminal_records(raw_records, include_mock=include_mock)
    live_records = [record for record in terminal_records if record.get("status") == "success"]
    mock_records = [record for record in terminal_records if record.get("status") == "mock"] if include_mock else []
    if live_records:
        analysis_records = live_records
        sample_mode = "live"
        cache_estimate = estimate_live_cache_requests(
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
        cache_estimate = empty_cache_stats()
    else:
        analysis_records = []
        sample_mode = "live"
        extraction_requests = 0
        canonicalization_requests = 0
        cache_estimate = empty_cache_stats()
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
    write_aggregates: bool = False,
    report_formats: tuple[str, ...] = ("markdown", "pdf"),
) -> dict[str, Any]:
    root = Path(bundle_dir)
    with job_bundle_lock(root):
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
                report_formats=report_formats,
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
    write_aggregates: bool = False,
    report_formats: tuple[str, ...] = ("markdown", "pdf"),
) -> dict[str, Any]:
    settings = settings or get_settings()
    root = Path(bundle_dir)
    manifest = load_job_manifest(root)
    raw_path = raw_attempts_path(root)
    if not raw_path.exists():
        raise ValueError(f"缺少 raw attempts：{raw_path}")
    if raw_path.is_symlink() or not raw_path.is_file():
        raise ValueError(f"raw attempts 必须是普通非 symlink 文件：{raw_path}")

    work = work_dir(root)
    logs = logs_dir(root)
    result = result_dir(root)
    for path in [work, logs, result]:
        ensure_private_directory(path)

    raw_records, raw_read_errors = read_jsonl_with_errors(raw_path)
    manifest = _manifest_with_queries_for_analysis(root, manifest, raw_records)
    analysis_profile = _analysis_profile(manifest)
    analysis_model = str(analysis_profile.get("model") or manifest["model"])
    terminal_records = _analysis_terminal_records(raw_records, include_mock=include_mock)
    live_records = [record for record in terminal_records if record.get("status") == "success"]
    mock_records = [record for record in terminal_records if record.get("status") == "mock"] if include_mock else []
    if live_records:
        analysis_statuses = {"success"}
        success_records = live_records
        sample_mode = "live"
        ignored_mock_record_count = len(latest_records(raw_records, statuses={"mock"})) if include_mock else 0
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
    success_records_for_stats = records_for_stats(success_records, data_quality, manifest)
    preflight_cache_stats = empty_cache_stats()
    cache_stats = empty_cache_stats()
    extractor_obj = None
    if sample_mode == "live" and success_records_for_stats and extractor is None:
        preflight_cache_stats = estimate_live_cache_requests(
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
            _validate_analysis_runtime_profile(analysis_profile, settings)
    if extractor is None and success_records_for_stats and sample_mode == "live" and preflight_cache_stats["analysis_llm_requests_remaining"] > 0:
        extractor_obj = LLMBrandExtractor(settings, model=analysis_model)
        extractor_obj.analysis_run_id = f"{manifest.get('job_id', 'job')}_analysis_g{manifest.get('run_generation', 0)}_{time.time_ns()}"
    active_extractor = extractor or (extractor_obj.extract_record if extractor_obj else None)
    if active_extractor is None and sample_mode == "mock":

        def mock_extractor(record: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
            return demo_extract_record(record, manifest)

        active_extractor = mock_extractor

    mention_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    if active_extractor or (sample_mode == "live" and extractor is None):
        mention_rows, error_rows, extraction_cache_stats = extract_mentions(
            records=success_records_for_stats,
            active_extractor=active_extractor,
            settings=settings,
            logs=logs,
            cache_enabled=sample_mode == "live" and extractor is None,
            extractor_model=analysis_model,
            refresh_extraction_cache=refresh_extraction_cache,
        )
        merge_cache_stats(cache_stats, extraction_cache_stats)

    raw_names = [row["brand_name_raw"] for row in mention_rows]
    if canonicalizer:
        canonical_map, canonical_error = canonicalizer(raw_names)
    elif cache_stats.get("analysis_circuit_breaker"):
        canonical_map, canonical_error = fallback_canonicalize(raw_names)
    elif extractor_obj and raw_names:
        canonical_map, canonical_error, canonical_cache_stats = canonicalize_with_cache(
            raw_names=raw_names,
            extractor_obj=extractor_obj,
            logs=logs,
            refresh_extraction_cache=refresh_extraction_cache,
        )
        merge_cache_stats(cache_stats, canonical_cache_stats)
    elif sample_mode == "live" and extractor is None and raw_names:
        canonical_map, canonical_error, canonical_cache_stats = canonicalize_from_cache_only(
            raw_names=raw_names,
            canonicalizer_model=analysis_model,
            logs=logs,
            refresh_extraction_cache=refresh_extraction_cache,
        )
        merge_cache_stats(cache_stats, canonical_cache_stats)
    else:
        canonical_map, canonical_error = fallback_canonicalize(raw_names)
    if canonical_error:
        canonical_error_row = redacted_error(canonical_error, settings)
        canonical_error_row.setdefault("scope", "global")
        canonical_error_row.setdefault("stage", "canonicalization")
        error_rows.append(canonical_error_row)
    analysis_audit_path = logs / "analysis_attempts.jsonl"
    analysis_audit_events = extractor_obj.drain_audit_events() if extractor_obj and hasattr(extractor_obj, "drain_audit_events") else []
    if analysis_audit_events:
        if analysis_audit_path.is_symlink():
            raise ValueError(f"analysis audit log 不能是 symlink：{analysis_audit_path}")
        existing_audit = read_jsonl(analysis_audit_path) if analysis_audit_path.exists() else []
        write_jsonl_atomic(analysis_audit_path, [*existing_audit, *analysis_audit_events])
    canonical_map = apply_target_alias_canonicalization(canonical_map, raw_names, manifest)
    apply_extraction_quality(data_quality, error_rows, len(success_records_for_stats))

    enriched_mentions = []
    for row in mention_rows:
        copy = dict(row)
        copy["brand_name_canonical"] = canonical_map.get(row["brand_name_raw"], row["brand_name_raw"])
        enriched_mentions.append(copy)

    stats = compute_open_brand_stats(enriched_mentions, success_records_for_stats, manifest)
    source_stats = compute_source_stats(success_records_for_stats, manifest)
    facts = build_fact_rows(
        manifest=manifest,
        terminal_records=terminal_records,
        stats_records=success_records_for_stats,
        mentions=enriched_mentions,
        data_quality=data_quality,
        sample_mode=sample_mode,
    )
    history_overview, history_visibility = load_intelligence_history(root, manifest, sample_mode)
    intelligence = build_intelligence_outputs(
        manifest=manifest,
        mentions=enriched_mentions,
        success_records=success_records_for_stats,
        facts=facts,
        brand_summary=stats["brand_summary"],
        history_overview=history_overview,
        history_visibility=history_visibility,
    )
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
        facts=facts,
        intelligence=intelligence,
        data_quality=data_quality,
    )
    if analysis_audit_path.exists():
        files["analysis_attempts_jsonl"] = analysis_audit_path

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
        "run_generation": manifest.get("run_generation", 0),
        "job_conclusion_strength": data_quality.get("conclusion_strength", "observational"),
        "query_set_hash": query_set_hash(manifest),
        "raw_record_count": len(raw_records),
        "success_record_count": len(success_records_for_stats),
        "analysis_record_count": len(success_records),
        "stats_record_count": len(success_records_for_stats),
        "sample_mode": sample_mode,
        "query_ids": [q["query_id"] for q in manifest["queries"]],
        "partial_sample": bool(data_quality["partial_sample"]),
        "data_quality": data_quality,
        "cache": cache_stats,
        "analysis_run_id": extractor_obj.analysis_run_id if extractor_obj else "",
        "analysis_llm_attempt_count": len(analysis_audit_events),
        "extracted_mention_count": len(enriched_mentions),
        "extraction_error_record_count": data_quality.get("extraction_error_record_count", 0),
        "extraction_error_row_count": data_quality.get("extraction_error_row_count", len(error_rows)),
        "extraction_error_rate": data_quality.get("extraction_error_rate", "0.0%"),
        "traceability_quarantine_count": data_quality.get("traceability_quarantine_count", 0),
        "target_detected": stats["target_detected"],
        "brand_summary": stats["brand_summary"],
        "brand_by_query": stats["brand_by_query"],
        "query_stability": stats["query_stability"],
        "source_domains": source_stats["source_domains"],
        "source_urls": source_stats["source_urls"],
        "source_by_query": source_stats["source_by_query"],
        "quality_summary": facts["quality_summary"],
        "attempt_facts": facts["attempt_facts"],
        "query_facts": facts["query_facts"],
        "brand_attempt_facts": facts["brand_attempt_facts"],
        "intelligence_schema_version": INTELLIGENCE_SCHEMA_VERSION,
        "intelligence": intelligence,
        "target_diagnosis": stats["target_diagnosis"],
        "analysis_files": {key: relative_path(root, value) for key, value in files.items()},
        "generated_at": utc_now_iso(),
        "method_note": "本报告基于 query 文本采样后的 LLM 开放式品牌抽取；SOV 主口径为品牌回答命中份额，不等同于市场份额。",
    }
    summary_path = logs / "analysis_summary.json"
    aggregate_files = cross_job_aggregate_paths(root) if write_aggregates else {}
    summary["aggregate_targets"] = {key: display_path(root, value) for key, value in aggregate_files.items()}
    summary["aggregate_files"] = {}
    report_model, report_files = render_report_bundle(summary, result, formats=report_formats)
    summary["report_model_schema_version"] = report_model.schema_version
    summary["report_files"] = {key: relative_path(root, value) for key, value in report_files.items()}
    artifact_manifest_path = logs / "analysis_artifacts.json"
    summary["analysis_artifact_manifest"] = relative_path(root, artifact_manifest_path)
    write_json_atomic(summary_path, summary)
    artifact_manifest = build_analysis_artifact_manifest(
        root=root,
        manifest=manifest,
        analysis_summary=summary_path,
        analysis_files=files,
        report_files=report_files,
    )
    write_json_atomic(artifact_manifest_path, artifact_manifest)
    for legacy_name in ("discovered_brands.csv", "sov_summary.csv"):
        legacy_path = result / legacy_name
        if legacy_path.is_file() or legacy_path.is_symlink():
            legacy_path.unlink()
    analysis_status = "analyzed_partial" if data_quality.get("conclusion_strength") == "observational" or error_rows else "analyzed"
    update_job_manifest(root, status=analysis_status)
    if write_aggregates:
        try:
            committed_aggregates = update_cross_job_aggregates(root, summary)
            summary["aggregate_files"] = {key: display_path(root, value) for key, value in committed_aggregates.items()}
        except Exception as exc:  # local committed analysis remains valid
            aggregate_error = {
                "type": exc.__class__.__name__,
                "message": redact_secret(str(exc), settings) or "",
                "completed_at": utc_now_iso(),
            }
            write_json_atomic(logs / "aggregate_error.json", aggregate_error)
            summary["aggregate_error"] = aggregate_error
    if not keep_work:
        try:
            cleanup_job_work_dir_unlocked(root)
        except Exception as exc:  # committed analysis must remain usable
            cleanup_error = {
                "type": exc.__class__.__name__,
                "message": redact_secret(str(exc), settings) or "",
                "completed_at": utc_now_iso(),
            }
            write_json_atomic(logs / "cleanup_error.json", cleanup_error)
            summary["cleanup_error"] = cleanup_error
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


def _validate_analysis_runtime_profile(profile: dict[str, Any], settings: Settings) -> None:
    expected_endpoint = str(profile.get("base_url_fingerprint") or "")
    actual_endpoint = base_url_fingerprint(settings.llm_base_url)
    if expected_endpoint and expected_endpoint != actual_endpoint:
        raise ValueError("分析运行时 LLM_BASE_URL 与 frozen analysis_profile 不一致；请使用构建 job 时的 endpoint")
    expected_tokens = profile.get("max_output_tokens")
    if expected_tokens not in (None, "") and int(expected_tokens) != settings.analysis_max_output_tokens:
        raise ValueError("分析运行时 ANALYSIS_MAX_OUTPUT_TOKENS 与 frozen analysis_profile 不一致；请使用构建 job 时的配置")


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
