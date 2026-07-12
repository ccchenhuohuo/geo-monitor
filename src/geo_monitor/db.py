from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .filesystem import UnsafeOutputPathError, ensure_private_directory, prepare_private_output, secure_private_file
from .job import JOB_MANIFEST, RAW_ATTEMPTS, load_job_manifest, load_job_queries
from .query_meta import query_metadata_json, tags_text

DUCKDB_SCHEMA_VERSION = "duckdb-schema-v6"
TERMINAL_ATTEMPT_STATUSES = {"success", "mock", "error", "dry_run", "interrupted"}
CORE_RESULT_CSV_STEMS = (
    "brand_summary",
    "brand_by_query",
    "query_stability",
    "source_domains",
    "source_urls",
    "quality_summary",
    "attempt_facts",
    "query_facts",
    "brand_attempt_facts",
)
INTELLIGENCE_CSV_STEMS = (
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
SAFE_QUERY_START_RE = re.compile(r"^\s*(select|with|show|describe|explain)\b", re.IGNORECASE | re.DOTALL)
FORBIDDEN_SQL_RE = re.compile(
    r"\b(attach|copy|create|delete|detach|drop|export|import|insert|install|load|merge|pragma|set|update|alter|call)\b"
    r"|\b(read_csv|read_csv_auto|read_json|read_ndjson|read_parquet|read_text|read_blob|parquet_scan|glob)\s*\(",
    re.IGNORECASE,
)


class DuckDBError(ValueError):
    pass


def build_duckdb(runs_dir: str | Path, output_path: str | Path, *, query_manifest: str | Path | None = None) -> dict[str, Any]:
    duckdb = _duckdb()
    runs_root = Path(runs_dir)
    if runs_root.is_symlink():
        raise DuckDBError(f"runs_dir 不能是 symlink：{runs_root}")
    output = Path(output_path)
    try:
        ensure_private_directory(output.parent)
        prepare_private_output(output)
    except UnsafeOutputPathError as exc:
        raise DuckDBError(str(exc)) from exc
    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    if tmp.is_symlink():
        raise DuckDBError(f"临时 DuckDB 路径不能是 symlink：{tmp}")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(str(tmp))
    try:
        _create_schema(con)
        fallback_rows = _load_fallback_manifest(query_manifest)
        counts = {"runs": 0, "queries": 0, "attempts": 0, "quality_flags": 0}
        for run_dir in _iter_run_dirs(runs_root):
            run_counts = _ingest_run(con, run_dir, fallback_rows)
            for key, value in run_counts.items():
                counts[key] = counts.get(key, 0) + value
        _create_views(con)
        con.close()
        secure_private_file(tmp)
        os.replace(tmp, output)
        secure_private_file(output)
    except Exception:
        con.close()
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return {"db_path": str(output), **counts, "schema_version": DUCKDB_SCHEMA_VERSION}


def inspect_duckdb(db_path: str | Path) -> dict[str, Any]:
    con = _connect_readonly(db_path)
    try:
        rows = con.execute("select table_name from information_schema.tables where table_schema='main' order by table_name").fetchall()
        tables = []
        for (name,) in rows:
            try:
                count = con.execute(f"select count(*) from {_quote_identifier(str(name))}").fetchone()[0]
            except Exception as exc:
                raise DuckDBError(f"无法 inspect DuckDB 表 {name!r}: {exc}") from exc
            tables.append({"table": name, "row_count": count})
        return {"db_path": str(db_path), "tables": tables}
    finally:
        con.close()


def query_duckdb(db_path: str | Path, sql: str, *, max_rows: int = 10_000) -> tuple[list[str], list[tuple[Any, ...]]]:
    _validate_safe_query_sql(sql)
    con = _connect_readonly(db_path)
    try:
        result = con.execute(sql)
        columns = [item[0] for item in result.description or []]
        rows = result.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            raise DuckDBError(f"查询结果超过 max_rows={max_rows}，请添加过滤或 LIMIT")
        return columns, rows
    except Exception as exc:
        raise DuckDBError(str(exc)) from exc
    finally:
        con.close()


def _create_schema(con: Any) -> None:
    con.execute("create table schema_info(schema_version varchar)")
    con.execute("insert into schema_info values (?)", [DUCKDB_SCHEMA_VERSION])
    con.execute(
        """
        create table runs(
            job_id varchar primary key,
            status varchar,
            created_at varchar,
            completed_at varchar,
            created_at_ts timestamptz,
            completed_at_ts timestamptz,
            target_brand varchar,
            model varchar,
            provider varchar,
            adapter varchar,
            adapter_version varchar,
            api_family varchar,
            repeats integer,
            source_grain varchar,
            analysis_model varchar,
            analysis_adapter varchar,
            analysis_fingerprint varchar,
            study_fingerprint varchar,
            sampling_fingerprint varchar,
            sample_mode varchar,
            partial_sample boolean,
            success_record_count integer,
            stats_record_count integer,
            run_generation integer,
            inferred_from_legacy boolean,
            job_conclusion_strength varchar,
            sample_count integer,
            query_manifest_sha256 varchar,
            query_manifest_source_type varchar,
            query_manifest_source_uri varchar,
            report_path varchar
        )
        """
    )
    con.execute(
        """
        create table queries(
            job_id varchar,
            query_id varchar,
            variant_id varchar,
            seed_id varchar,
            seed_query varchar,
            category varchar,
            intent varchar,
            persona varchar,
            template_id varchar,
            query varchar,
            locale varchar,
            market varchar,
            tags varchar,
            language varchar,
            generation_method varchar,
            fanout_version varchar,
            manifest_version varchar,
            locked_at varchar,
            query_metadata_json varchar
        )
        """
    )
    con.execute(
        """
        create table attempts(
            job_id varchar,
            attempt_id varchar,
            query_id varchar,
            repeat_index integer,
            run_generation integer,
            diagnostic_generation integer,
            execution_mode varchar,
            status varchar,
            latency_ms integer,
            error varchar,
            model varchar,
            provider varchar,
            adapter varchar,
            adapter_version varchar,
            api_family varchar,
            request_fingerprint_version varchar,
            web_search_performed boolean,
            web_search_evidence varchar,
            web_search_requirement_status varchar,
            source_parse_status varchar,
            created_at varchar,
            completed_at varchar,
            created_at_ts timestamptz,
            completed_at_ts timestamptz,
            request_hash varchar,
            response_preview varchar,
            response_length integer,
            raw_path varchar,
            raw_line_number integer
        )
        """
    )
    con.execute(
        """
        create table brand_summary(
            job_id varchar, brand_name_canonical varchar, sov_event_share double,
            response_mention_rate double, query_coverage_rate double, is_target_brand integer
        )
        """
    )
    con.execute(
        """
        create table brand_by_query(
            job_id varchar, query_id varchar, brand_name_canonical varchar,
            responses_mentioned integer, mention_rate_within_query double
        )
        """
    )
    con.execute(
        "create table query_stability(job_id varchar, query_id varchar, successful_repeats integer, expected_repeats integer, brand_set_jaccard_avg double)"
    )
    con.execute("create table source_domains(job_id varchar, domain varchar, response_coverage_rate double, query_coverage_rate double)")
    con.execute("create table source_urls(job_id varchar, url varchar, domain varchar, title varchar, parsed_source_occurrences integer)")
    con.execute(
        """
        create table quality_summary(
            job_id varchar, sample_mode varchar, conclusion_strength varchar, partial_sample boolean,
            planned_units integer, analysis_record_count integer, stats_record_count integer,
            missing_unit_count integer, latest_failed_unit_count integer,
            web_search_quality_flag_count integer, source_quality_flag_count integer,
            extraction_error_record_count integer, extraction_error_rate double
        )
        """
    )
    con.execute(
        """
        create table attempt_facts(
            job_id varchar, run_generation integer, query_id varchar, repeat_index integer,
            latest_status varchar, completed_at varchar, completed_at_ts timestamptz,
            valid_attempt integer, stats_included integer, web_search_requirement_status varchar,
            web_search_evidence varchar, source_parse_status varchar, request_hash varchar, attempt_id varchar
        )
        """
    )
    con.execute(
        """
        create table query_facts(
            job_id varchar, query_id varchar, query varchar, planned_attempts integer,
            latest_terminal_attempts integer, completed_attempts integer, valid_attempts integer,
            stats_included_attempts integer, latest_failed_attempts integer,
            sample_completeness double, usable_sample_rate double, query_metadata_json varchar
        )
        """
    )
    con.execute(
        """
        create table brand_attempt_facts(
            job_id varchar, query_id varchar, repeat_index integer,
            brand_name_canonical varchar, brand_name_raw varchar, is_target_brand integer,
            sov_eligible boolean, is_recommended integer, rank_position integer,
            sentiment varchar, confidence double, evidence varchar, stats_included integer
        )
        """
    )
    con.execute("create table quality_flags(job_id varchar, type varchar, message varchar, path varchar, raw_line_number integer, query_id varchar)")
    con.execute(
        """
        create table intelligence_artifacts(
            job_id varchar,
            run_generation integer,
            artifact_stem varchar,
            source_path varchar,
            source_sha256 varchar,
            row_count integer,
            columns_json varchar,
            column_types_json varchar
        )
        """
    )
    con.execute(
        """
        create table intelligence_rows(
            job_id varchar,
            run_generation integer,
            artifact_stem varchar,
            row_number integer,
            row_json varchar
        )
        """
    )


def _create_views(con: Any) -> None:
    con.execute(
        """
        create view latest_attempts as
        select * exclude (_latest_rank)
        from (
            select
                attempts.*,
                row_number() over (
                    partition by job_id, query_id, repeat_index
                    order by
                        case
                            when coalesce(
                                execution_mode,
                                case when status = 'mock' then 'mock' when status = 'dry_run' then 'dry_run' else 'live' end
                            ) = 'live'
                            then 0 else 1
                        end,
                        coalesce(completed_at_ts, created_at_ts) desc nulls last,
                        raw_line_number desc
                ) as _latest_rank
            from attempts
            where status in ('success', 'mock', 'error', 'dry_run', 'interrupted')
              and coalesce(query_id, '') != ''
              and repeat_index is not null
              and repeat_index >= 1
        ) ranked
        where _latest_rank = 1
        """
    )
    con.execute(
        """
        create view current_attempts as
        select
            job_id,
            attempt_id,
            query_id,
            repeat_index,
            run_generation,
            latest_status as status,
            valid_attempt,
            stats_included,
            completed_at,
            completed_at_ts,
            web_search_requirement_status,
            web_search_evidence,
            source_parse_status,
            request_hash,
            'analysis_fact' as fact_source
        from attempt_facts
        union all
        select
            latest.job_id,
            latest.attempt_id,
            latest.query_id,
            latest.repeat_index,
            latest.run_generation,
            latest.status,
            case when latest.status in ('success', 'mock') then 1 else 0 end,
            case when latest.status in ('success', 'mock') then 1 else 0 end,
            latest.completed_at,
            latest.completed_at_ts,
            latest.web_search_requirement_status,
            latest.web_search_evidence,
            latest.source_parse_status,
            latest.request_hash,
            'latest_raw' as fact_source
        from latest_attempts latest
        where not exists (
            select 1 from attempt_facts facts where facts.job_id = latest.job_id
        )
        """
    )
    con.execute(
        """
        create view metrics_by_seed as
        select q.job_id, q.seed_id, count(distinct q.query_id) as query_count, count(a.query_id) as attempt_count
        from queries q left join current_attempts a on q.job_id = a.job_id and q.query_id = a.query_id
        group by q.job_id, q.seed_id
        """
    )
    con.execute(
        """
        create view metrics_by_persona as
        select q.job_id, q.persona, count(distinct q.query_id) as query_count, count(a.query_id) as attempt_count
        from queries q left join current_attempts a on q.job_id = a.job_id and q.query_id = a.query_id
        group by q.job_id, q.persona
        """
    )
    con.execute("create view metrics_by_run as select job_id, count(*) as attempt_count from current_attempts group by job_id")
    con.execute(
        """
        create view attempt_quality_by_run as
        select
            job_id,
            count(*) as latest_terminal_attempt_count,
            sum(coalesce(valid_attempt, 0)) as valid_attempt_count,
            sum(coalesce(stats_included, 0)) as stats_included_attempt_count,
            sum(case when status = 'error' then 1 else 0 end) as latest_failed_attempt_count,
            sum(
                case
                    when coalesce(web_search_requirement_status, '') not in ('satisfied', 'not_applicable')
                      or (web_search_requirement_status = 'satisfied' and coalesce(web_search_evidence, '') = '')
                    then 1 else 0
                end
            ) as web_search_quality_flag_count,
            sum(
                case when coalesce(source_parse_status, '') not in ('parsed', 'provider_returned_empty') then 1 else 0 end
            ) as source_quality_flag_count
        from current_attempts
        group by job_id
        """
    )
    con.execute(
        """
        create view comparison_cohorts as
        select
            coalesce(nullif(query_manifest_sha256, ''), 'unknown') as query_manifest_sha256,
            repeats,
            coalesce(cast(cast(coalesce(completed_at_ts, created_at_ts) as date) as varchar), 'unknown') as execution_window_bucket,
            count(*) as job_count,
            count(distinct provider || ':' || adapter || ':' || api_family) as comparison_group_count,
            count(distinct analysis_fingerprint) as analysis_fingerprint_count,
            count(distinct study_fingerprint) as study_fingerprint_count,
            count(distinct sampling_fingerprint) as sampling_fingerprint_count,
            case
                when count(*) > 1
                 and count(distinct provider || ':' || adapter || ':' || api_family) = 1
                 and count(distinct analysis_fingerprint) = 1
                 and count(distinct study_fingerprint) = 1
                 and count(distinct sampling_fingerprint) = 1
                 and min(case when coalesce(study_fingerprint, '') != '' then 1 else 0 end) = 1
                 and min(case when coalesce(sampling_fingerprint, '') != '' then 1 else 0 end) = 1
                 and min(case when job_conclusion_strength = 'strong' then 1 else 0 end) = 1
                 and min(case when coalesce(partial_sample, false) = false then 1 else 0 end) = 1
                 and min(case when coalesce(inferred_from_legacy, false) = false then 1 else 0 end) = 1
                 and min(case when coalesce(web_status_bad.bad_count, 0) = 0 then 1 else 0 end) = 1
                 and min(case when coalesce(source_status_bad.bad_count, 0) = 0 then 1 else 0 end) = 1
                then 'strong'
                else 'observational'
            end as comparison_conclusion_strength,
            case
                when min(case when source_grain = 'url' then 1 else 0 end) = 1
                 and min(case when coalesce(source_status_not_parsed.bad_count, 0) = 0 then 1 else 0 end) = 1
                 and min(case when coalesce(source_status_parsed.parsed_count, 0) > 0 then 1 else 0 end) = 1
                 and min(case when coalesce(source_url_facts.url_count, 0) > 0 then 1 else 0 end) = 1
                then true
                else false
            end as source_metrics_comparable
        from runs
        left join (
            select job_id, count(*) as bad_count
            from current_attempts
            where coalesce(source_parse_status, '') not in ('parsed', 'provider_returned_empty')
            group by job_id
        ) source_status_bad using (job_id)
        left join (
            select job_id, count(*) as bad_count
            from current_attempts
            where coalesce(source_parse_status, '') != 'parsed'
            group by job_id
        ) source_status_not_parsed using (job_id)
        left join (
            select job_id, count(*) as parsed_count
            from current_attempts
            where source_parse_status = 'parsed'
            group by job_id
        ) source_status_parsed using (job_id)
        left join (
            select job_id, count(*) as url_count
            from source_urls
            where coalesce(url, '') != '' and coalesce(parsed_source_occurrences, 0) > 0
            group by job_id
        ) source_url_facts using (job_id)
        left join (
            select job_id, count(*) as bad_count
            from current_attempts
            where coalesce(web_search_requirement_status, '') not in ('satisfied', 'not_applicable')
               or (web_search_requirement_status = 'satisfied' and coalesce(web_search_evidence, '') = '')
            group by job_id
        ) web_status_bad using (job_id)
        group by query_manifest_sha256, repeats, execution_window_bucket
        """
    )
    _create_intelligence_views(con)


def _iter_run_dirs(runs_root: Path) -> list[Path]:
    if not runs_root.exists():
        return []
    candidates: list[Path] = []
    for path in sorted(item for item in runs_root.iterdir() if item.is_dir() and not item.is_symlink()):
        if path.name.startswith("."):
            continue
        if (path / JOB_MANIFEST).exists() or (path / RAW_ATTEMPTS).exists():
            candidates.append(path)
    return candidates


def _ingest_run(con: Any, run_dir: Path, fallback_rows: dict[str, dict[str, str]]) -> dict[str, int]:
    counts = {"runs": 0, "queries": 0, "attempts": 0, "quality_flags": 0}
    manifest_path = run_dir / JOB_MANIFEST
    if not manifest_path.exists():
        _quality(con, run_dir.name, "missing_job_manifest", "job_manifest.json missing", str(manifest_path))
        counts["quality_flags"] += 1
        return counts
    try:
        manifest = load_job_manifest(run_dir)
    except Exception as exc:
        _quality(con, run_dir.name, "bad_job_manifest", str(exc), str(manifest_path))
        counts["quality_flags"] += 1
        return counts
    job_id = str(manifest.get("job_id") or run_dir.name)
    raw_path = run_dir / RAW_ATTEMPTS
    attempts, query_rows, flags = _read_attempts(raw_path, job_id, fallback_rows)
    for flag in flags:
        _quality(con, job_id, **flag)
    counts["quality_flags"] += len(flags)
    if not raw_path.exists():
        _quality(con, job_id, "missing_raw_attempts", "raw/attempts.jsonl missing", str(raw_path))
        counts["quality_flags"] += 1
    info = manifest.get("query_manifest") if isinstance(manifest.get("query_manifest"), dict) else {}
    sampling_profile = manifest.get("sampling_profile") if isinstance(manifest.get("sampling_profile"), dict) else {}
    analysis_profile = manifest.get("analysis_profile") if isinstance(manifest.get("analysis_profile"), dict) else {}
    comparability_profile = manifest.get("comparability_profile") if isinstance(manifest.get("comparability_profile"), dict) else {}
    planned_query_rows = _planned_query_rows(run_dir, manifest)
    for qid, row in query_rows.items():
        if qid in planned_query_rows:
            planned_query_rows[qid].update({key: value for key, value in row.items() if value not in (None, "")})
        else:
            planned_query_rows[qid] = row
    analysis_summary = _read_analysis_summary(run_dir)
    artifact_manifest_present, artifact_manifest_reasons = _validate_analysis_artifact_manifest(run_dir, manifest, analysis_summary)
    artifact_manifest_required = str(manifest.get("schema_version") or "") == "geo-job-v3" and str(manifest.get("status") or "").startswith("analyzed")
    if artifact_manifest_required and not artifact_manifest_present:
        artifact_manifest_reasons = ["commit marker is required for analyzed geo-job-v3 bundles"]
    if artifact_manifest_reasons:
        summary_current = False
        summary_reasons = [f"analysis_artifacts.json: {reason}" for reason in artifact_manifest_reasons]
    else:
        summary_current, summary_reasons = _analysis_summary_is_current(
            manifest,
            analysis_summary,
            attempts=attempts,
            planned_query_rows=planned_query_rows,
        )
    artifact_reasons: list[str] = []
    if summary_current and _has_result_csvs(run_dir, analysis_summary):
        artifact_reasons = _validate_analysis_artifacts(
            run_dir,
            job_id,
            manifest,
            analysis_summary,
            attempts,
            planned_query_rows,
        )
    analysis_artifacts_current = summary_current and not artifact_reasons
    if not summary_current and str(manifest.get("status") or "").startswith("analyzed"):
        _quality(
            con,
            job_id,
            "invalid_analysis_artifact_manifest" if artifact_manifest_present or artifact_manifest_required else "stale_or_missing_analysis_summary",
            "; ".join(summary_reasons) or "analysis_summary.json is missing or stale",
            str(run_dir / "logs" / "analysis_summary.json"),
        )
        counts["quality_flags"] += 1
        if artifact_manifest_present:
            for stem in CORE_RESULT_CSV_STEMS:
                missing_path = run_dir / "result" / f"{stem}.csv"
                if not missing_path.exists():
                    _quality(con, job_id, "missing_result_csv", f"result CSV missing: {stem}.csv", str(missing_path))
                    counts["quality_flags"] += 1
    elif artifact_reasons:
        _quality(
            con,
            job_id,
            "stale_analysis_artifacts_ignored",
            "; ".join(artifact_reasons),
            str(run_dir / "result"),
        )
        counts["quality_flags"] += 1
    pdf_report = run_dir / "result" / "report.pdf"
    markdown_report = run_dir / "result" / "report.md"
    report_path = pdf_report if pdf_report.exists() else markdown_report
    completed_at = _completed_at(run_dir, attempts)
    inferred_from_legacy = bool(
        sampling_profile.get("inferred_from_legacy") or analysis_profile.get("inferred_from_legacy") or comparability_profile.get("inferred_from_legacy")
    )
    data_quality = analysis_summary.get("data_quality") if summary_current and isinstance(analysis_summary.get("data_quality"), dict) else {}
    con.execute(
        """
        insert into runs(
            job_id, status, created_at, completed_at, created_at_ts, completed_at_ts,
            target_brand, model, provider, adapter, adapter_version, api_family, repeats,
            source_grain, analysis_model, analysis_adapter, analysis_fingerprint,
            study_fingerprint, sampling_fingerprint, sample_mode, partial_sample,
            success_record_count, stats_record_count, run_generation, inferred_from_legacy,
            job_conclusion_strength, sample_count, query_manifest_sha256,
            query_manifest_source_type, query_manifest_source_uri, report_path
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            job_id,
            manifest.get("status"),
            manifest.get("created_at"),
            completed_at,
            _parse_timestamp(manifest.get("created_at")),
            _parse_timestamp(completed_at),
            manifest.get("target_brand"),
            manifest.get("model"),
            sampling_profile.get("provider", ""),
            sampling_profile.get("adapter", manifest.get("adapter", "")),
            sampling_profile.get("adapter_version", ""),
            sampling_profile.get("api_family", ""),
            _to_int(manifest.get("repeats")),
            sampling_profile.get("source_grain", ""),
            analysis_profile.get("model", ""),
            analysis_profile.get("adapter", ""),
            analysis_profile.get("analysis_fingerprint", ""),
            comparability_profile.get("study_fingerprint", ""),
            comparability_profile.get("sampling_fingerprint", ""),
            analysis_summary.get("sample_mode", "") if summary_current else "",
            bool(analysis_summary.get("partial_sample") or data_quality.get("partial_sample")) if summary_current else None,
            _to_int(analysis_summary.get("success_record_count")) if summary_current else None,
            _to_int(analysis_summary.get("stats_record_count")) if summary_current else None,
            _to_int(manifest.get("run_generation")),
            inferred_from_legacy,
            (analysis_summary.get("job_conclusion_strength") or data_quality.get("conclusion_strength", "")) if summary_current else "",
            len(attempts),
            comparability_profile.get("query_manifest_sha256") or info.get("sha256", ""),
            info.get("source_type", ""),
            info.get("source_uri", ""),
            str(report_path) if analysis_artifacts_current and report_path.exists() else "",
        ],
    )
    counts["runs"] += 1
    seen_attempts: set[str] = set()
    for attempt in attempts:
        if attempt["attempt_id"] in seen_attempts:
            _quality(con, job_id, "duplicate_attempt_id", "duplicate attempt_id retained", attempt["raw_path"], attempt["raw_line_number"], attempt["query_id"])
            counts["quality_flags"] += 1
        else:
            seen_attempts.add(attempt["attempt_id"])
        con.execute(
            """
            insert into attempts(
                job_id, attempt_id, query_id, repeat_index, run_generation, diagnostic_generation,
                execution_mode, status, latency_ms,
                error, model, provider, adapter, adapter_version, api_family,
                request_fingerprint_version, web_search_performed, web_search_evidence,
                web_search_requirement_status, source_parse_status, created_at, completed_at,
                created_at_ts, completed_at_ts, request_hash, response_preview, response_length,
                raw_path, raw_line_number
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                job_id,
                attempt["attempt_id"],
                attempt["query_id"],
                attempt["repeat_index"],
                attempt["run_generation"],
                attempt["diagnostic_generation"],
                attempt["execution_mode"],
                attempt["status"],
                attempt["latency_ms"],
                attempt["error"],
                attempt["model"],
                attempt["provider"],
                attempt["adapter"],
                attempt["adapter_version"],
                attempt["api_family"],
                attempt["request_fingerprint_version"],
                attempt["web_search_performed"],
                attempt["web_search_evidence"],
                attempt["web_search_requirement_status"],
                attempt["source_parse_status"],
                attempt["created_at"],
                attempt["completed_at"],
                attempt["created_at_ts"],
                attempt["completed_at_ts"],
                attempt["request_hash"],
                attempt["response_preview"],
                attempt["response_length"],
                attempt["raw_path"],
                attempt["raw_line_number"],
            ],
        )
        counts["attempts"] += 1
    for row in planned_query_rows.values():
        con.execute(
            "insert into queries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row["query_id"],
                row["variant_id"],
                row["seed_id"],
                row["seed_query"],
                row["category"],
                row["intent"],
                row["persona"],
                row["template_id"],
                row["query"],
                row["locale"],
                row["market"],
                row["tags"],
                row["language"],
                row["generation_method"],
                row["fanout_version"],
                row["manifest_version"],
                row["locked_at"],
                row["query_metadata_json"],
            ],
        )
        counts["queries"] += 1
    if analysis_artifacts_current:
        counts["quality_flags"] += _ingest_csv_outputs(
            con,
            run_dir,
            job_id,
            run_generation=_to_int(analysis_summary.get("run_generation")) or 0,
            analysis_summary=analysis_summary,
        )
    elif (run_dir / "result").exists() and any((run_dir / "result").iterdir()):
        _quality(con, job_id, "stale_result_csv_ignored", "result CSV ignored because analysis_summary is stale or missing", str(run_dir / "result"))
        counts["quality_flags"] += 1
    return counts


def _read_attempts(
    raw_path: Path, job_id: str, fallback_rows: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    queries: dict[str, dict[str, str]] = {}
    query_field_priority: dict[str, dict[str, int]] = {}
    flags: list[dict[str, Any]] = []
    if not raw_path.exists():
        return attempts, queries, flags
    if raw_path.is_symlink() or not raw_path.is_file():
        flags.append(
            {
                "type": "unsafe_raw_attempts",
                "message": "raw attempts must be a regular non-symlink file",
                "path": str(raw_path),
                "raw_line_number": 0,
                "query_id": "",
            }
        )
        return attempts, queries, flags
    with raw_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                flags.append({"type": "malformed_jsonl", "message": str(exc), "path": str(raw_path), "raw_line_number": line_no, "query_id": ""})
                continue
            qid = str(record.get("query_id") or "")
            query = str(record.get("query") or record.get("input_query") or "")
            meta = record.get("query_meta") if isinstance(record.get("query_meta"), dict) else {}
            record_metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            if record_metadata:
                meta = _merge_record_metadata(meta, record_metadata)
            fallback = fallback_rows.get(qid)
            if not meta and fallback:
                meta = fallback
                flags.append(
                    {
                        "type": "query_meta_fallback_used",
                        "message": "query_meta filled from fallback manifest",
                        "path": str(raw_path),
                        "raw_line_number": line_no,
                        "query_id": qid,
                    }
                )
            elif not meta:
                flags.append(
                    {"type": "query_meta_missing", "message": "query_meta missing", "path": str(raw_path), "raw_line_number": line_no, "query_id": qid}
                )
            elif fallback:
                conflicts = [
                    key for key, value in fallback.items() if key in meta and str(meta.get(key) or "") and str(value or "") and str(meta.get(key)) != str(value)
                ]
                if conflicts:
                    flags.append(
                        {"type": "query_meta_conflict", "message": ",".join(conflicts), "path": str(raw_path), "raw_line_number": line_no, "query_id": qid}
                    )
            error = record.get("error") if isinstance(record.get("error"), dict) else {}
            sampling_profile = record.get("sampling_profile") if isinstance(record.get("sampling_profile"), dict) else {}
            response_text = str(record.get("response_text") or "")
            attempt_id = str(record.get("attempt_id") or f"{job_id}__{qid}__r{record.get('repeat_index', 1)}__{record.get('request_hash', '')}")
            created_at = str(record.get("started_at") or "")
            completed_at = str(record.get("completed_at") or "")
            repeat_index = _to_positive_int(record.get("repeat_index", 1))
            # Legacy attempts used an execution-scoped run_id that was not the
            # bundle job_id, so only an explicit job_id participates in this check.
            record_job_id = str(record.get("job_id") or "")
            if repeat_index is None:
                flags.append(
                    {
                        "type": "invalid_repeat_index",
                        "message": "repeat_index must be a positive integer",
                        "path": str(raw_path),
                        "raw_line_number": line_no,
                        "query_id": qid,
                    }
                )
            if record_job_id and record_job_id != job_id:
                flags.append(
                    {
                        "type": "attempt_job_id_mismatch",
                        "message": f"attempt job_id={record_job_id!r} differs from manifest job_id",
                        "path": str(raw_path),
                        "raw_line_number": line_no,
                        "query_id": qid,
                    }
                )
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "explicit_attempt_id": bool(record.get("attempt_id")),
                    "query_id": qid,
                    "repeat_index": repeat_index,
                    "run_generation": _to_int(record.get("run_generation")),
                    "diagnostic_generation": _to_int(record.get("diagnostic_generation")),
                    "execution_mode": str(record.get("execution_mode") or "live"),
                    "record_job_id": record_job_id,
                    "status": str(record.get("status") or ""),
                    "latency_ms": _to_int(record.get("latency_ms")),
                    "error": str(error.get("message") or error.get("type") or ""),
                    "model": str(record.get("model") or ""),
                    "provider": str(sampling_profile.get("provider") or ""),
                    "adapter": str(sampling_profile.get("adapter") or ""),
                    "adapter_version": str(sampling_profile.get("adapter_version") or ""),
                    "api_family": str(sampling_profile.get("api_family") or ""),
                    "request_fingerprint_version": str(record.get("request_fingerprint_version") or ""),
                    "web_search_performed": _to_bool(record.get("web_search_performed")),
                    "web_search_evidence": str(record.get("web_search_evidence") or ""),
                    "web_search_requirement_status": str(record.get("web_search_requirement_status") or ""),
                    "source_parse_status": str(record.get("source_parse_status") or ""),
                    "created_at": created_at,
                    "completed_at": completed_at,
                    "created_at_ts": _parse_timestamp(created_at),
                    "completed_at_ts": _parse_timestamp(completed_at),
                    "request_hash": str(record.get("request_hash") or ""),
                    "response_preview": response_text[:500],
                    "response_length": len(response_text),
                    "raw_path": str(raw_path),
                    "raw_line_number": line_no,
                }
            )
            if qid:
                priority = (
                    3 if record.get("status") in {"success", "mock"} and record.get("query_meta") else 2 if record.get("query_meta") else 1 if fallback else 0
                )
                _merge_query_row(queries, query_field_priority, qid, query, meta, priority)
    return attempts, queries, flags


def _merge_query_row(
    queries: dict[str, dict[str, str]],
    priorities: dict[str, dict[str, int]],
    qid: str,
    query: str,
    meta: dict[str, Any],
    priority: int,
) -> None:
    row = queries.setdefault(
        qid,
        {
            "query_id": qid,
            "variant_id": "",
            "seed_id": "",
            "seed_query": "",
            "category": "",
            "intent": "",
            "persona": "",
            "template_id": "",
            "query": "",
            "locale": "",
            "market": "",
            "tags": "",
            "language": "",
            "generation_method": "",
            "fanout_version": "",
            "manifest_version": "",
            "locked_at": "",
            "query_metadata_json": "{}",
        },
    )
    field_priorities = priorities.setdefault(qid, {})
    values = {
        "query": query,
        "variant_id": str(meta.get("variant_id") or ""),
        "seed_id": str(meta.get("seed_id") or ""),
        "seed_query": str(meta.get("seed_query") or ""),
        "category": str(meta.get("category") or ""),
        "intent": str(meta.get("intent") or ""),
        "persona": str(meta.get("persona") or ""),
        "template_id": str(meta.get("template_id") or ""),
        "locale": str(meta.get("locale") or ""),
        "market": str(meta.get("market") or ""),
        "tags": tags_text(meta.get("tags")),
        "language": str(meta.get("language") or ""),
        "generation_method": str(meta.get("generation_method") or ""),
        "fanout_version": str(meta.get("fanout_version") or ""),
        "manifest_version": str(meta.get("manifest_version") or ""),
        "locked_at": str(meta.get("locked_at") or ""),
        "query_metadata_json": query_metadata_json(meta),
    }
    for key, value in values.items():
        if not value:
            continue
        current_priority = field_priorities.get(key, -1)
        if not row.get(key) or priority >= current_priority:
            row[key] = value
            field_priorities[key] = priority


def _merge_record_metadata(meta: dict[str, Any], record_metadata: dict[str, Any]) -> dict[str, Any]:
    merged = dict(record_metadata)
    merged.update({key: value for key, value in meta.items() if value not in (None, "")})
    return merged


def _ingest_csv_outputs(
    con: Any,
    run_dir: Path,
    job_id: str,
    *,
    run_generation: int,
    analysis_summary: dict[str, Any],
) -> int:
    result = run_dir / "result"
    quality_count = 0
    for name in (f"{stem}.csv" for stem in CORE_RESULT_CSV_STEMS):
        path = result / name
        if not path.exists():
            _quality(con, job_id, "missing_result_csv", f"result CSV missing: {name}", str(path))
            quality_count += 1
    for row in _read_csv(result / "brand_summary.csv"):
        con.execute(
            "insert into brand_summary values (?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("brand_name_canonical", ""),
                _pct(row.get("sov_event_share")),
                _pct(row.get("response_mention_rate")),
                _pct(row.get("query_coverage_rate")),
                _to_int(row.get("is_target_brand")),
            ],
        )
    for row in _read_csv(result / "brand_by_query.csv"):
        con.execute(
            "insert into brand_by_query values (?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("query_id", ""),
                row.get("brand_name_canonical", ""),
                _to_int(row.get("responses_mentioned")),
                _pct(row.get("mention_rate_within_query")),
            ],
        )
    for row in _read_csv(result / "query_stability.csv"):
        con.execute(
            "insert into query_stability values (?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("query_id", ""),
                _to_int(row.get("successful_repeats")),
                _to_int(row.get("expected_repeats")),
                _to_float(row.get("brand_set_jaccard_avg")),
            ],
        )
    for row in _read_csv(result / "source_domains.csv"):
        con.execute(
            "insert into source_domains values (?, ?, ?, ?)",
            [job_id, row.get("domain", ""), _pct(row.get("response_coverage_rate")), _pct(row.get("query_coverage_rate"))],
        )
    for row in _read_csv(result / "source_urls.csv"):
        con.execute(
            "insert into source_urls values (?, ?, ?, ?, ?)",
            [job_id, row.get("url", ""), row.get("domain", ""), row.get("title", ""), _to_int(row.get("parsed_source_occurrences"))],
        )
    for row in _read_csv(result / "quality_summary.csv"):
        con.execute(
            "insert into quality_summary values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("sample_mode", ""),
                row.get("conclusion_strength", ""),
                _to_bool(row.get("partial_sample")),
                _to_int(row.get("planned_units")),
                _to_int(row.get("analysis_record_count")),
                _to_int(row.get("stats_record_count")),
                _to_int(row.get("missing_unit_count")),
                _to_int(row.get("latest_failed_unit_count")),
                _to_int(row.get("web_search_quality_flag_count")),
                _to_int(row.get("source_quality_flag_count")),
                _to_int(row.get("extraction_error_record_count")),
                _pct(row.get("extraction_error_rate")),
            ],
        )
    for row in _read_csv(result / "attempt_facts.csv"):
        con.execute(
            "insert into attempt_facts values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                run_generation,
                row.get("query_id", ""),
                _to_int(row.get("repeat_index")),
                row.get("latest_status", ""),
                row.get("completed_at", ""),
                _parse_timestamp(row.get("completed_at")),
                _to_int(row.get("valid_attempt")),
                _to_int(row.get("stats_included")),
                row.get("web_search_requirement_status", ""),
                row.get("web_search_evidence", ""),
                row.get("source_parse_status", ""),
                row.get("request_hash", ""),
                row.get("attempt_id", ""),
            ],
        )
    for row in _read_csv(result / "query_facts.csv"):
        con.execute(
            "insert into query_facts values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("query_id", ""),
                row.get("query", ""),
                _to_int(row.get("planned_attempts")),
                _to_int(row.get("latest_terminal_attempts")),
                _to_int(row.get("completed_attempts")),
                _to_int(row.get("valid_attempts")),
                _to_int(row.get("stats_included_attempts")),
                _to_int(row.get("latest_failed_attempts")),
                _pct(row.get("sample_completeness")),
                _pct(row.get("usable_sample_rate")),
                row.get("query_metadata_json", ""),
            ],
        )
    for row in _read_csv(result / "brand_attempt_facts.csv"):
        con.execute(
            "insert into brand_attempt_facts values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("query_id", ""),
                _to_int(row.get("repeat_index")),
                row.get("brand_name_canonical", ""),
                row.get("brand_name_raw", ""),
                _to_int(row.get("is_target_brand")),
                _to_bool(row.get("sov_eligible")),
                _to_int(row.get("is_recommended")),
                _to_int(row.get("rank_position")),
                row.get("sentiment", ""),
                _to_float(row.get("confidence")),
                row.get("evidence", ""),
                _to_int(row.get("stats_included")),
            ],
        )
    quality_count += _ingest_intelligence_outputs(
        con,
        run_dir,
        job_id,
        run_generation=run_generation,
        analysis_summary=analysis_summary,
    )
    return quality_count


def _has_result_csvs(run_dir: Path, summary: dict[str, Any]) -> bool:
    result = run_dir / "result"
    if result.exists() and any(path.is_file() and path.suffix.lower() == ".csv" for path in result.iterdir()):
        return True
    for field in ("analysis_files", "intelligence_files"):
        mapping = summary.get(field)
        if isinstance(mapping, dict) and any(stem in mapping for stem in INTELLIGENCE_CSV_STEMS):
            return True
    return False


def _intelligence_artifact_paths(run_dir: Path, summary: dict[str, Any]) -> tuple[dict[str, Path], list[str]]:
    result_root = (run_dir / "result").resolve()
    declared: dict[str, Any] = {}
    for field in ("analysis_files", "intelligence_files"):
        mapping = summary.get(field)
        if isinstance(mapping, dict):
            declared.update({str(key): value for key, value in mapping.items() if str(key) in INTELLIGENCE_CSV_STEMS})
    artifacts = summary.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            stem = str(item.get("stem") or item.get("artifact_stem") or "")
            if stem in INTELLIGENCE_CSV_STEMS and item.get("path"):
                declared[stem] = item["path"]

    paths: dict[str, Path] = {}
    reasons: list[str] = []
    resolved_sources: dict[Path, str] = {}
    for stem in INTELLIGENCE_CSV_STEMS:
        declared_value = declared.get(stem)
        fallback = run_dir / "result" / f"{stem}.csv"
        if isinstance(declared_value, dict):
            declared_value = declared_value.get("path") or declared_value.get("csv")
        candidate = Path(str(declared_value)) if declared_value not in (None, "") else fallback
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        candidate_was_symlink = candidate.is_symlink()
        try:
            resolved = candidate.resolve()
            resolved.relative_to(result_root)
        except (OSError, ValueError):
            if declared_value not in (None, "") or candidate.exists() or candidate_was_symlink:
                reasons.append(f"declared intelligence artifact {stem!r} escapes result directory")
            continue
        if candidate_was_symlink:
            reasons.append(f"intelligence artifact {stem!r} must not be a symlink")
            continue
        if resolved.suffix.lower() != ".csv":
            if declared_value not in (None, ""):
                reasons.append(f"declared intelligence artifact {stem!r} is not a CSV")
            continue
        if not resolved.exists():
            if declared_value not in (None, ""):
                reasons.append(f"declared intelligence artifact {stem!r} is missing")
            continue
        previous_stem = resolved_sources.get(resolved)
        if previous_stem is not None:
            reasons.append(f"intelligence artifacts {previous_stem!r} and {stem!r} resolve to the same file")
            continue
        resolved_sources[resolved] = stem
        paths[stem] = resolved
    return paths, reasons


def _ingest_intelligence_outputs(
    con: Any,
    run_dir: Path,
    job_id: str,
    *,
    run_generation: int,
    analysis_summary: dict[str, Any],
) -> int:
    paths, reasons = _intelligence_artifact_paths(run_dir, analysis_summary)
    for reason in reasons:
        _quality(con, job_id, "invalid_intelligence_artifact", reason, str(run_dir / "result"))
    for stem, path in paths.items():
        headers, rows = _read_csv_document(path)
        column_types = {header: _infer_intelligence_column_type([row.get(header, "") for row in rows]) for header in headers}
        con.execute(
            "insert into intelligence_artifacts values (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                run_generation,
                stem,
                str(path),
                _file_sha256(path),
                len(rows),
                json.dumps(headers, ensure_ascii=False, separators=(",", ":")),
                json.dumps(column_types, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ],
        )
        for row_number, row in enumerate(rows, start=1):
            con.execute(
                "insert into intelligence_rows values (?, ?, ?, ?, ?)",
                [
                    job_id,
                    run_generation,
                    stem,
                    row_number,
                    json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ],
            )
    return len(reasons)


def _create_intelligence_views(con: Any) -> None:
    registry_rows = con.execute("select artifact_stem, columns_json, column_types_json from intelligence_artifacts order by job_id, artifact_stem").fetchall()
    registry: dict[str, list[tuple[list[str], dict[str, str]]]] = {}
    for stem, columns_json, column_types_json in registry_rows:
        try:
            columns = json.loads(str(columns_json or "[]"))
            column_types = json.loads(str(column_types_json or "{}"))
        except (TypeError, ValueError):
            continue
        if isinstance(columns, list) and isinstance(column_types, dict):
            registry.setdefault(str(stem), []).append(([str(item) for item in columns], {str(key): str(value) for key, value in column_types.items()}))

    reserved = {"job_id", "run_generation", "artifact_stem", "row_number", "row_json"}
    for stem in INTELLIGENCE_CSV_STEMS:
        ordered_columns: list[str] = []
        merged_types: dict[str, str] = {}
        seen: set[str] = set()
        for columns, column_types in registry.get(stem, []):
            for column in columns:
                normalized = column.casefold()
                if normalized in reserved or normalized in seen or not column:
                    continue
                if column not in ordered_columns:
                    ordered_columns.append(column)
                seen.add(normalized)
                merged_types[column] = _merge_intelligence_types(merged_types.get(column), column_types.get(column, "VARCHAR"))

        projections = ["job_id", "run_generation", "artifact_stem", "row_number"]
        for column in ordered_columns:
            extraction = f"json_extract_string(row_json, {_sql_string_literal(_json_path(column))})"
            column_type = merged_types.get(column, "VARCHAR")
            if column_type == "BOOLEAN":
                expression = f"try_cast({extraction} as boolean)"
            elif column_type == "BIGINT":
                expression = f"try_cast({extraction} as bigint)"
            elif column_type == "DOUBLE":
                expression = f"try_cast({extraction} as double)"
            elif column_type == "PERCENT":
                expression = f"try_cast(replace({extraction}, '%', '') as double) / 100.0"
            elif column_type == "TIMESTAMPTZ":
                expression = f"try_cast({extraction} as timestamptz)"
            else:
                expression = extraction
            projections.append(f"{expression} as {_quote_identifier(column)}")
        projections.append("row_json")
        con.execute(
            f"create view {_quote_identifier(stem)} as select {', '.join(projections)} from intelligence_rows where artifact_stem = {_sql_string_literal(stem)}"
        )


def _infer_intelligence_column_type(values: list[str]) -> str:
    materialized = [str(value).strip() for value in values if str(value).strip()]
    if not materialized:
        return "VARCHAR"
    lowered = {value.lower() for value in materialized}
    if lowered <= {"true", "false", "yes", "no"}:
        return "BOOLEAN"
    if all(re.fullmatch(r"[+-]?\d+", value) for value in materialized):
        return "BIGINT"
    if all(value.endswith("%") and _finite_float(value[:-1]) is not None for value in materialized):
        return "PERCENT"
    if all(_finite_float(value) is not None for value in materialized):
        return "DOUBLE"
    if all(_parse_timestamp(value) is not None for value in materialized):
        return "TIMESTAMPTZ"
    return "VARCHAR"


def _merge_intelligence_types(current: str | None, incoming: str) -> str:
    if current is None or current == incoming:
        return incoming
    if {current, incoming} <= {"BIGINT", "DOUBLE"}:
        return "DOUBLE"
    return "VARCHAR"


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json_path(column: str) -> str:
    return "$." + json.dumps(column, ensure_ascii=False)


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _load_fallback_manifest(path: str | Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    rows: dict[str, dict[str, str]] = {}
    for row in _read_csv(Path(path)):
        qid = str(row.get("query_id") or "")
        if qid:
            rows[qid] = {key: str(value or "") for key, value in row.items()}
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    return _read_csv_document(path)[1]


def _read_csv_document(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [str(item) for item in (reader.fieldnames or []) if item is not None]
        rows = [{str(key): "" if value is None else str(value) for key, value in row.items() if key is not None} for row in reader]
    return headers, rows


def _quality(con: Any, job_id: str, type: str, message: str, path: str, raw_line_number: int | None = None, query_id: str = "") -> None:
    con.execute("insert into quality_flags values (?, ?, ?, ?, ?, ?)", [job_id, type, message, path, raw_line_number, query_id])


def _completed_at(run_dir: Path, attempts: list[dict[str, Any]]) -> str:
    summary = run_dir / "logs" / "run_summary.json"
    candidates: list[str] = []
    if summary.exists():
        try:
            candidates.append(str(json.loads(summary.read_text(encoding="utf-8")).get("completed_at") or ""))
        except Exception:
            pass
    candidates.extend(str(row.get("completed_at") or "") for row in attempts if row.get("completed_at"))
    parsed = [(value, _parse_timestamp(value)) for value in candidates if value]
    valid = [(value, timestamp) for value, timestamp in parsed if timestamp is not None]
    if valid:
        return max(valid, key=lambda item: item[1])[0]
    return candidates[-1] if candidates else ""


def _read_analysis_summary(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "logs" / "analysis_summary.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _analysis_summary_is_current(
    manifest: dict[str, Any],
    summary: dict[str, Any],
    *,
    attempts: list[dict[str, Any]],
    planned_query_rows: dict[str, dict[str, str]],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not str(manifest.get("status") or "").startswith("analyzed"):
        return False, ["job manifest status is not analyzed"]
    if not summary:
        return False, ["analysis_summary.json is missing or unreadable"]
    manifest_generation_raw = manifest.get("run_generation")
    summary_generation_raw = summary.get("run_generation")
    manifest_generation = _to_int(manifest_generation_raw)
    summary_generation = _to_int(summary_generation_raw)
    if manifest_generation_raw not in (None, "") and summary_generation_raw in (None, ""):
        reasons.append("analysis summary has no run_generation")
    elif (manifest_generation or 0) != (summary_generation or 0):
        reasons.append(f"run_generation mismatch: manifest={manifest_generation or 0}, summary={summary_generation or 0}")

    job_id = str(manifest.get("job_id") or "")
    if summary.get("job_id") not in (None, "") and str(summary.get("job_id")) != job_id:
        reasons.append("analysis summary job_id does not match manifest")
    if any(row.get("record_job_id") and row.get("record_job_id") != job_id for row in attempts):
        reasons.append("raw attempts contain a different job_id")
    if manifest_generation is not None and any(row.get("run_generation") is not None and int(row["run_generation"]) > manifest_generation for row in attempts):
        reasons.append("raw attempts contain a generation newer than the manifest")

    expected_queries = _to_int(manifest.get("query_count"))
    expected_repeats = _to_int(manifest.get("repeats"))
    _compare_optional_int(summary, "expected_queries", expected_queries, reasons)
    _compare_optional_int(summary, "expected_repeats", expected_repeats, reasons)
    expected_units = expected_queries * expected_repeats if expected_queries is not None and expected_repeats is not None else None
    _compare_optional_int(summary, "expected_units", expected_units, reasons)

    if summary.get("model") not in (None, "") and str(summary.get("model")) != str(manifest.get("model") or ""):
        reasons.append("analysis summary model does not match manifest")
    expected_query_hash = _manifest_query_hash(manifest)
    if expected_query_hash and summary.get("query_set_hash") not in (None, "") and str(summary.get("query_set_hash")) != expected_query_hash[:16]:
        reasons.append("analysis summary query_set_hash does not match manifest")

    summary_query_ids = summary.get("query_ids")
    planned_ids = set(planned_query_rows)
    if isinstance(summary_query_ids, list):
        normalized_summary_ids = {str(value) for value in summary_query_ids if str(value)}
        if len(normalized_summary_ids) != len(summary_query_ids):
            reasons.append("analysis summary query_ids are empty or duplicated")
        if expected_queries is not None and len(normalized_summary_ids) != expected_queries:
            reasons.append("analysis summary query_ids count does not match manifest query_count")
        if planned_ids and (expected_queries is None or len(planned_ids) == expected_queries) and normalized_summary_ids != planned_ids:
            reasons.append("analysis summary query_ids do not match the planned query universe")

    for profile_name, fingerprint_fields in {
        "analysis_profile": ("analysis_fingerprint",),
        "comparability_profile": ("query_manifest_sha256", "study_fingerprint", "sampling_fingerprint"),
        "sampling_profile": ("provider", "adapter", "adapter_version", "api_family"),
    }.items():
        expected_profile = manifest.get(profile_name)
        actual_profile = summary.get(profile_name)
        if not isinstance(expected_profile, dict) or not isinstance(actual_profile, dict):
            continue
        for field in fingerprint_fields:
            expected = expected_profile.get(field)
            actual = actual_profile.get(field)
            if expected not in (None, "") and actual not in (None, "") and str(expected) != str(actual):
                reasons.append(f"analysis summary {profile_name}.{field} does not match manifest")
    return not reasons, list(dict.fromkeys(reasons))


def _validate_analysis_artifact_manifest(
    run_dir: Path,
    manifest: dict[str, Any],
    summary: dict[str, Any],
) -> tuple[bool, list[str]]:
    marker_path = run_dir / "logs" / "analysis_artifacts.json"
    if not marker_path.exists():
        return False, []
    if marker_path.is_symlink() or not marker_path.is_file():
        return True, ["commit marker must be a regular non-symlink file"]
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return True, [f"commit marker is unreadable: {exc}"]
    if not isinstance(marker, dict):
        return True, ["commit marker must contain a JSON object"]

    reasons: list[str] = []
    if marker.get("schema_version") != "analysis-artifacts-v1":
        reasons.append("unsupported schema_version")
    job_id = str(manifest.get("job_id") or "")
    if str(marker.get("job_id") or "") != job_id:
        reasons.append("job_id does not match job manifest")
    marker_generation = _to_int(marker.get("run_generation"))
    manifest_generation = _to_int(manifest.get("run_generation")) or 0
    if marker_generation != manifest_generation:
        reasons.append("run_generation does not match job manifest")
    summary_intelligence_schema = summary.get("intelligence_schema_version")
    if summary_intelligence_schema not in (None, "") and marker.get("intelligence_schema_version") not in (None, summary_intelligence_schema):
        reasons.append("intelligence_schema_version does not match analysis summary")

    inputs = marker.get("inputs")
    if not isinstance(inputs, dict):
        reasons.append("inputs must be an object")
        inputs = {}
    if "raw_attempts" not in inputs:
        reasons.append("raw_attempts input is not committed")
    expected_input_paths = {
        "raw_attempts": (run_dir / RAW_ATTEMPTS).resolve(),
        "query_manifest": (run_dir / "work" / "query_manifest.csv").resolve(),
    }
    for name, entry in inputs.items():
        if not isinstance(entry, dict):
            reasons.append(f"input {name!r} metadata must be an object")
            continue
        resolved, path_reason = _resolve_committed_path(run_dir, entry.get("path"))
        if path_reason:
            reasons.append(f"input {name!r}: {path_reason}")
            continue
        if name in expected_input_paths and resolved != expected_input_paths[name]:
            reasons.append(f"input {name!r} path is not the expected bundle path")
            continue
        expected_hash = _normalized_sha256(entry.get("sha256"))
        if expected_hash is None:
            reasons.append(f"input {name!r} has an invalid sha256")
            continue
        validation_mode = str(entry.get("validation_mode") or "")
        expected_size = _to_int(entry.get("size_bytes"))
        if validation_mode not in {"exact", "append_only_prefix"}:
            reasons.append(f"input {name!r} has an unsupported validation_mode")
            continue
        if expected_size is None or expected_size < 0:
            reasons.append(f"input {name!r} has an invalid size_bytes")
            continue
        if resolved.exists():
            if resolved.is_symlink() or not resolved.is_file():
                reasons.append(f"input {name!r} must be a regular non-symlink file")
            elif validation_mode == "exact":
                if resolved.stat().st_size != expected_size:
                    reasons.append(f"input {name!r} size_bytes mismatch")
                elif _file_sha256(resolved) != expected_hash:
                    reasons.append(f"input {name!r} sha256 mismatch")
            elif resolved.stat().st_size < expected_size:
                reasons.append(f"append-only input {name!r} is shorter than its committed prefix")
            elif _file_prefix_sha256(resolved, expected_size) != expected_hash:
                reasons.append(f"append-only input {name!r} committed-prefix sha256 mismatch")
            elif resolved.stat().st_size > expected_size:
                suffix_reason = _validate_diagnostic_jsonl_suffix(resolved, expected_size, manifest_generation)
                if suffix_reason:
                    reasons.append(f"append-only input {name!r} suffix is invalid: {suffix_reason}")
        elif bool(entry.get("required_after_cleanup", True)):
            reasons.append(f"required input {name!r} is missing")
        elif name == "query_manifest":
            query_manifest_info = manifest.get("query_manifest")
            declared_manifest_hash = query_manifest_info.get("sha256") if isinstance(query_manifest_info, dict) else None
            manifest_query_hash = _normalized_sha256(declared_manifest_hash)
            if manifest_query_hash is not None and manifest_query_hash != expected_hash:
                reasons.append("cleaned query_manifest hash does not match job manifest")

    artifacts = marker.get("artifacts")
    if not isinstance(artifacts, dict):
        reasons.append("artifacts must be an object")
        artifacts = {}
    if "analysis_summary" not in artifacts:
        reasons.append("analysis_summary artifact is not committed")
    committed_paths: dict[Path, str] = {}
    for name, entry in artifacts.items():
        if not isinstance(entry, dict):
            reasons.append(f"artifact {name!r} metadata must be an object")
            continue
        resolved, path_reason = _resolve_committed_path(run_dir, entry.get("path"))
        if path_reason:
            reasons.append(f"artifact {name!r}: {path_reason}")
            continue
        previous = committed_paths.get(resolved)
        if previous is not None:
            reasons.append(f"artifacts {previous!r} and {name!r} resolve to the same file")
            continue
        committed_paths[resolved] = str(name)
        if resolved.is_symlink() or not resolved.is_file():
            reasons.append(f"artifact {name!r} is missing or is not a regular file")
            continue
        expected_hash = _normalized_sha256(entry.get("sha256"))
        if expected_hash is None:
            reasons.append(f"artifact {name!r} has an invalid sha256")
        elif _file_sha256(resolved) != expected_hash:
            reasons.append(f"artifact {name!r} sha256 mismatch")
        expected_size = _to_int(entry.get("size_bytes"))
        if expected_size is None or expected_size != resolved.stat().st_size:
            reasons.append(f"artifact {name!r} size_bytes mismatch")

    summary_path = (run_dir / "logs" / "analysis_summary.json").resolve()
    if committed_paths.get(summary_path) != "analysis_summary":
        reasons.append("analysis_summary artifact path is incorrect")
    result_dir = run_dir / "result"
    if result_dir.exists():
        for path in result_dir.iterdir():
            if path.is_file() and path.suffix.lower() in {".csv", ".md", ".html", ".pdf"}:
                if path.resolve() not in committed_paths:
                    reasons.append(f"uncommitted analysis artifact exists: result/{path.name}")
    return True, list(dict.fromkeys(reasons))


def validate_analysis_commit(
    run_dir: str | Path,
    manifest: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Validate the v1 analysis commit marker for trusted cross-run consumers."""

    root = Path(run_dir)
    manifest = manifest or load_job_manifest(root)
    if summary is None:
        summary_path = root / "logs" / "analysis_summary.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return False, [f"analysis summary is unreadable: {exc}"]
    present, reasons = _validate_analysis_artifact_manifest(root, manifest, summary)
    if not present:
        return False, ["analysis commit marker is missing"]
    return not reasons, reasons


def _resolve_committed_path(run_dir: Path, value: Any) -> tuple[Path, str]:
    if value in (None, ""):
        return run_dir.resolve(), "path is missing"
    candidate = Path(str(value))
    if not candidate.is_absolute():
        candidate = run_dir / candidate
    if candidate.is_symlink():
        return candidate, "path must not be a symlink"
    try:
        resolved = candidate.resolve()
        resolved.relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return candidate, "path escapes the job bundle"
    return resolved, ""


def _normalized_sha256(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_prefix_sha256(path: Path, size_bytes: int) -> str:
    digest = hashlib.sha256()
    remaining = size_bytes
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest() if remaining == 0 else ""


def _validate_diagnostic_jsonl_suffix(path: Path, offset: int, run_generation: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            suffix = handle.read().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return f"unreadable suffix: {exc}"
    for line_number, line in enumerate(suffix.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            return f"line {line_number} is not JSON: {exc}"
        if not isinstance(record, dict):
            return f"line {line_number} is not an object"
        execution_mode = str(record.get("execution_mode") or "")
        if execution_mode not in {"mock", "dry_run"}:
            return f"line {line_number} is not a diagnostic execution"
        if _to_int(record.get("run_generation")) != run_generation:
            return f"line {line_number} changes run_generation"
        if _to_positive_int(record.get("diagnostic_generation")) is None:
            return f"line {line_number} lacks diagnostic_generation"
        allowed_statuses = {"mock", "error", "interrupted"} if execution_mode == "mock" else {"dry_run", "error", "interrupted"}
        if str(record.get("status") or "") not in allowed_statuses:
            return f"line {line_number} has invalid diagnostic status"
    return ""


def _validate_analysis_artifacts(
    run_dir: Path,
    job_id: str,
    manifest: dict[str, Any],
    summary: dict[str, Any],
    attempts: list[dict[str, Any]],
    planned_query_rows: dict[str, dict[str, str]],
) -> list[str]:
    reasons: list[str] = []
    result = run_dir / "result"
    attempt_path = result / "attempt_facts.csv"
    csv_paths = [result / f"{stem}.csv" for stem in CORE_RESULT_CSV_STEMS]
    intelligence_paths, path_reasons = _intelligence_artifact_paths(run_dir, summary)
    reasons.extend(path_reasons)
    present_paths = [path for path in csv_paths if path.exists()] + list(intelligence_paths.values())
    if present_paths and not attempt_path.exists():
        reasons.append("analysis CSVs exist but attempt_facts.csv is missing; artifacts are not generation-verifiable")
        return reasons

    _, fact_rows = _read_csv_document(attempt_path)
    expected_attempts = _latest_analysis_attempts(attempts, sample_mode=str(summary.get("sample_mode") or ""))
    expected_by_key = {(str(row.get("query_id") or ""), int(row.get("repeat_index") or 1)): row for row in expected_attempts}
    actual_by_key: dict[tuple[str, int], dict[str, str]] = {}
    for row in fact_rows:
        row_job_id = str(row.get("job_id") or "")
        if row_job_id and row_job_id != job_id:
            reasons.append("attempt_facts.csv contains a different job_id")
        repeat_index = _to_positive_int(row.get("repeat_index"))
        query_id = str(row.get("query_id") or "")
        if not query_id or repeat_index is None:
            reasons.append("attempt_facts.csv contains an invalid logical unit key")
            continue
        key = (query_id, repeat_index)
        if key in actual_by_key:
            reasons.append("attempt_facts.csv contains duplicate logical unit keys")
            continue
        actual_by_key[key] = row
        expected = expected_by_key.get(key)
        if expected is None:
            reasons.append(f"attempt_facts.csv contains stale or unknown unit {query_id}#{repeat_index}")
            continue
        if str(row.get("latest_status") or "") != str(expected.get("status") or ""):
            reasons.append(f"attempt_facts.csv status is stale for {query_id}#{repeat_index}")
        expected_attempt_id = str(expected.get("attempt_id") or "") if expected.get("explicit_attempt_id") else ""
        actual_attempt_id = str(row.get("attempt_id") or "")
        if expected_attempt_id and actual_attempt_id != expected_attempt_id:
            reasons.append(f"attempt_facts.csv attempt_id is stale for {query_id}#{repeat_index}")
        actual_completed = _parse_timestamp(row.get("completed_at"))
        expected_completed = expected.get("completed_at_ts")
        if actual_completed is not None and expected_completed is not None and actual_completed != expected_completed:
            reasons.append(f"attempt_facts.csv completed_at is stale for {query_id}#{repeat_index}")

    if set(actual_by_key) != set(expected_by_key):
        reasons.append("attempt_facts.csv logical-unit universe does not match latest terminal raw attempts")
    stats_count = sum(1 for row in fact_rows if (_to_int(row.get("stats_included")) or 0) != 0)
    _compare_optional_int(summary, "stats_record_count", stats_count, reasons)
    analysis_count = sum(1 for row in fact_rows if (_to_int(row.get("valid_attempt")) or 0) != 0)
    _compare_optional_int(summary, "analysis_record_count", analysis_count, reasons)

    _, query_fact_rows = _read_csv_document(result / "query_facts.csv")
    if query_fact_rows:
        query_fact_ids = [str(row.get("query_id") or "") for row in query_fact_rows]
        expected_query_ids = (
            {str(value) for value in summary.get("query_ids", []) if str(value)} if isinstance(summary.get("query_ids"), list) else set(planned_query_rows)
        )
        if len(set(query_fact_ids)) != len(query_fact_ids) or "" in query_fact_ids:
            reasons.append("query_facts.csv contains empty or duplicate query_id values")
        if expected_query_ids and set(query_fact_ids) != expected_query_ids:
            reasons.append("query_facts.csv query universe does not match analysis input")

    _, quality_rows = _read_csv_document(result / "quality_summary.csv")
    expected_units = (_to_int(manifest.get("query_count")) or 0) * (_to_int(manifest.get("repeats")) or 0)
    for row in quality_rows:
        if row.get("job_id") not in (None, "", job_id):
            reasons.append("quality_summary.csv contains a different job_id")
        planned_units = _to_int(row.get("planned_units"))
        if expected_units and planned_units is not None and planned_units != expected_units:
            reasons.append("quality_summary.csv planned_units does not match manifest")

    summary_generation = _to_int(summary.get("run_generation")) or 0
    for stem, path in intelligence_paths.items():
        headers, rows = _read_csv_document(path)
        if len(headers) != len(set(headers)):
            reasons.append(f"{stem}.csv contains duplicate column names")
        for row in rows:
            if row.get("job_id") not in (None, "", job_id):
                reasons.append(f"{stem}.csv contains a different job_id")
                break
            generation_value = row.get("run_generation") or row.get("analysis_generation")
            if generation_value not in (None, "") and _to_int(generation_value) != summary_generation:
                reasons.append(f"{stem}.csv contains a stale run_generation")
                break
    return list(dict.fromkeys(reasons))


def _latest_analysis_attempts(attempts: list[dict[str, Any]], *, sample_mode: str) -> list[dict[str, Any]]:
    if sample_mode == "live":
        statuses = {"success", "error"}
    elif sample_mode == "mock":
        statuses = {"mock", "error"}
    else:
        statuses = TERMINAL_ATTEMPT_STATUSES
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for row in attempts:
        repeat_index = _to_positive_int(row.get("repeat_index"))
        query_id = str(row.get("query_id") or "")
        if not query_id or repeat_index is None or row.get("status") not in statuses:
            continue
        key = (query_id, repeat_index)
        previous = latest.get(key)
        if previous is None or _attempt_order_key(row) >= _attempt_order_key(previous):
            latest[key] = row
    return [latest[key] for key in sorted(latest)]


def _attempt_order_key(row: dict[str, Any]) -> tuple[int, datetime, int]:
    timestamp = row.get("completed_at_ts") or row.get("created_at_ts")
    return (
        int(timestamp is not None),
        timestamp or datetime.min.replace(tzinfo=timezone.utc),
        int(row.get("raw_line_number") or 0),
    )


def _compare_optional_int(source: dict[str, Any], field: str, expected: int | None, reasons: list[str]) -> None:
    if expected is None or source.get(field) in (None, ""):
        return
    actual = _to_int(source.get(field))
    if actual != expected:
        reasons.append(f"analysis summary {field}={actual!r} does not match expected {expected}")


def _manifest_query_hash(manifest: dict[str, Any]) -> str:
    comparability = manifest.get("comparability_profile")
    if isinstance(comparability, dict) and comparability.get("query_manifest_sha256"):
        return str(comparability["query_manifest_sha256"])
    query_manifest = manifest.get("query_manifest")
    if isinstance(query_manifest, dict) and query_manifest.get("sha256"):
        return str(query_manifest["sha256"])
    return ""


def _planned_query_rows(run_dir: Path, manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    try:
        records = load_job_queries(run_dir, manifest, materialize=False)
    except Exception:
        records = []
    for record in records:
        meta = dict(record.metadata)
        if record.locale:
            meta["locale"] = record.locale
        if record.market:
            meta["market"] = record.market
        if record.category:
            meta["category"] = record.category
        if record.tags:
            meta["tags"] = ",".join(record.tags)
        row: dict[str, str] = {
            "query_id": str(record.query_id),
            "variant_id": str(meta.get("variant_id") or ""),
            "seed_id": str(meta.get("seed_id") or ""),
            "seed_query": str(meta.get("seed_query") or ""),
            "category": str(meta.get("category") or ""),
            "intent": str(meta.get("intent") or ""),
            "persona": str(meta.get("persona") or ""),
            "template_id": str(meta.get("template_id") or ""),
            "query": str(record.query),
            "locale": str(meta.get("locale") or ""),
            "market": str(meta.get("market") or ""),
            "tags": tags_text(meta.get("tags")),
            "language": str(meta.get("language") or ""),
            "generation_method": str(meta.get("generation_method") or ""),
            "fanout_version": str(meta.get("fanout_version") or ""),
            "manifest_version": str(meta.get("manifest_version") or ""),
            "locked_at": str(meta.get("locked_at") or ""),
            "query_metadata_json": query_metadata_json(meta),
        }
        rows[str(record.query_id)] = row
    return rows


def _pct(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("%"):
        return _to_float(text[:-1], scale=100.0)
    return _to_float(text)


def _to_float(value: Any, *, scale: float = 1.0) -> float | None:
    try:
        return float(value) / scale
    except Exception:
        return None


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


def _to_positive_int(value: Any) -> int | None:
    parsed = _to_int(value)
    return parsed if parsed is not None and parsed >= 1 else None


def _to_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def connect_readonly(db_path: str | Path) -> Any:
    return _connect_readonly(db_path)


def validate_schema(con: Any) -> None:
    try:
        row = con.execute("select schema_version from schema_info limit 1").fetchone()
    except Exception as exc:
        raise DuckDBError(f"DuckDB schema_info 缺失或不可读：{exc}") from exc
    if not row or str(row[0]) != DUCKDB_SCHEMA_VERSION:
        raise DuckDBError(f"DuckDB schema_version 不支持：{row[0] if row else 'unknown'}；请重建 DuckDB")
    required = {
        "runs": {
            "job_id",
            "status",
            "sample_mode",
            "partial_sample",
            "study_fingerprint",
            "sampling_fingerprint",
            "job_conclusion_strength",
            "created_at_ts",
            "completed_at_ts",
        },
        "queries": {"job_id", "query_id", "query", "query_metadata_json"},
        "attempts": {
            "job_id",
            "attempt_id",
            "query_id",
            "repeat_index",
            "run_generation",
            "diagnostic_generation",
            "execution_mode",
            "completed_at_ts",
            "web_search_requirement_status",
            "source_parse_status",
        },
        "quality_summary": {"job_id", "sample_mode", "stats_record_count"},
        "attempt_facts": {"job_id", "run_generation", "query_id", "repeat_index", "latest_status", "completed_at_ts", "stats_included"},
        "query_facts": {"job_id", "query_id", "planned_attempts", "usable_sample_rate"},
        "brand_attempt_facts": {"job_id", "query_id", "brand_name_canonical", "stats_included"},
        "latest_attempts": {"job_id", "attempt_id", "query_id", "repeat_index", "completed_at_ts"},
        "current_attempts": {"job_id", "attempt_id", "query_id", "repeat_index", "fact_source"},
        "attempt_quality_by_run": {"job_id", "latest_terminal_attempt_count", "stats_included_attempt_count"},
        "intelligence_artifacts": {"job_id", "run_generation", "artifact_stem", "columns_json", "column_types_json"},
        "intelligence_rows": {"job_id", "run_generation", "artifact_stem", "row_number", "row_json"},
        "comparison_cohorts": {"query_manifest_sha256", "comparison_conclusion_strength", "source_metrics_comparable"},
    }
    for stem in INTELLIGENCE_CSV_STEMS:
        required[stem] = {"job_id", "run_generation", "artifact_stem", "row_number", "row_json"}
    for table, columns in required.items():
        try:
            rows = con.execute(
                "select column_name from information_schema.columns where table_schema='main' and table_name=?",
                [table],
            ).fetchall()
        except Exception as exc:
            raise DuckDBError(f"DuckDB schema 校验失败：{table}: {exc}") from exc
        existing = {str(item[0]) for item in rows}
        missing = sorted(columns - existing)
        if missing:
            raise DuckDBError(f"DuckDB schema 缺少 {table} 字段：{', '.join(missing)}；请重建 DuckDB")


def _connect_readonly(db_path: str | Path) -> Any:
    path = Path(db_path)
    if path.is_symlink() or not path.is_file():
        raise DuckDBError(f"DuckDB 必须是普通非 symlink 文件：{path}")
    duckdb = _duckdb()
    try:
        return duckdb.connect(str(db_path), read_only=True, config={"enable_external_access": "false", "memory_limit": "512MB"})
    except TypeError:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            con.execute("set enable_external_access=false")
            con.execute("set memory_limit='512MB'")
        except Exception:
            con.close()
            raise
        return con


def _validate_safe_query_sql(sql: str) -> None:
    text = str(sql or "").strip()
    if not text:
        raise DuckDBError("SQL 查询不能为空")
    without_trailing = text[:-1].strip() if text.endswith(";") else text
    if ";" in without_trailing:
        raise DuckDBError("只允许单条只读 SQL 查询")
    if not SAFE_QUERY_START_RE.search(text):
        raise DuckDBError("只允许 SELECT/WITH/SHOW/DESCRIBE/EXPLAIN 只读查询")
    if FORBIDDEN_SQL_RE.search(text):
        raise DuckDBError("SQL 包含不允许的 DuckDB 语句或外部文件读取函数")


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _duckdb() -> Any:
    try:
        import duckdb
    except ModuleNotFoundError as exc:
        raise DuckDBError("缺少 duckdb 依赖，请安装项目依赖：pip install -e .") from exc
    return duckdb
