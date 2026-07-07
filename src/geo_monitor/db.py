from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any

from .job import JOB_MANIFEST, RAW_ATTEMPTS, load_job_manifest, load_job_queries
from .query_meta import query_metadata_json, tags_text


DUCKDB_SCHEMA_VERSION = "duckdb-schema-v5"
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
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
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
        os.replace(tmp, output)
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
            response_preview varchar,
            response_length integer,
            raw_path varchar,
            raw_line_number integer
        )
        """
    )
    con.execute("create table brand_summary(job_id varchar, brand_name_canonical varchar, sov_event_share double, response_mention_rate double, query_coverage_rate double, is_target_brand integer)")
    con.execute("create table brand_by_query(job_id varchar, query_id varchar, brand_name_canonical varchar, responses_mentioned integer, mention_rate_within_query double)")
    con.execute("create table query_stability(job_id varchar, query_id varchar, successful_repeats integer, expected_repeats integer, brand_set_jaccard_avg double)")
    con.execute("create table source_domains(job_id varchar, domain varchar, response_coverage_rate double, query_coverage_rate double)")
    con.execute("create table source_urls(job_id varchar, url varchar, domain varchar, title varchar, parsed_source_occurrences integer)")
    con.execute("create table quality_summary(job_id varchar, sample_mode varchar, conclusion_strength varchar, partial_sample boolean, planned_units integer, analysis_record_count integer, stats_record_count integer, missing_unit_count integer, latest_failed_unit_count integer, web_search_quality_flag_count integer, source_quality_flag_count integer, extraction_error_record_count integer, extraction_error_rate double)")
    con.execute("create table attempt_facts(job_id varchar, query_id varchar, repeat_index integer, latest_status varchar, completed_at varchar, valid_attempt integer, stats_included integer, web_search_requirement_status varchar, web_search_evidence varchar, source_parse_status varchar, request_hash varchar, attempt_id varchar)")
    con.execute("create table query_facts(job_id varchar, query_id varchar, query varchar, planned_attempts integer, latest_terminal_attempts integer, completed_attempts integer, valid_attempts integer, stats_included_attempts integer, latest_failed_attempts integer, sample_completeness double, usable_sample_rate double, query_metadata_json varchar)")
    con.execute("create table brand_attempt_facts(job_id varchar, query_id varchar, repeat_index integer, brand_name_canonical varchar, brand_name_raw varchar, is_target_brand integer, sov_eligible boolean, is_recommended integer, rank_position integer, sentiment varchar, confidence double, evidence varchar, stats_included integer)")
    con.execute("create table quality_flags(job_id varchar, type varchar, message varchar, path varchar, raw_line_number integer, query_id varchar)")


def _create_views(con: Any) -> None:
    con.execute(
        """
        create view metrics_by_seed as
        select q.seed_id, count(distinct q.query_id) as query_count, count(a.attempt_id) as attempt_count
        from queries q left join attempts a on q.job_id = a.job_id and q.query_id = a.query_id
        group by q.seed_id
        """
    )
    con.execute(
        """
        create view metrics_by_persona as
        select q.persona, count(distinct q.query_id) as query_count, count(a.attempt_id) as attempt_count
        from queries q left join attempts a on q.job_id = a.job_id and q.query_id = a.query_id
        group by q.persona
        """
    )
    con.execute("create view metrics_by_run as select job_id, count(*) as attempt_count from attempts group by job_id")
    con.execute(
        """
        create view comparison_cohorts as
        select
            coalesce(nullif(query_manifest_sha256, ''), 'unknown') as query_manifest_sha256,
            repeats,
            substr(coalesce(nullif(completed_at, ''), created_at), 1, 10) as execution_window_bucket,
            count(*) as job_count,
            count(distinct provider || ':' || adapter || ':' || api_family) as comparison_group_count,
            count(distinct analysis_fingerprint) as analysis_fingerprint_count,
            count(distinct study_fingerprint) as study_fingerprint_count,
            count(distinct sampling_fingerprint) as sampling_fingerprint_count,
            case
                when count(*) > 1
                 and count(distinct provider || ':' || adapter || ':' || api_family) > 1
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
            from attempts
            where coalesce(source_parse_status, '') not in ('parsed', 'provider_returned_empty')
            group by job_id
        ) source_status_bad using (job_id)
        left join (
            select job_id, count(*) as bad_count
            from attempts
            where coalesce(source_parse_status, '') != 'parsed'
            group by job_id
        ) source_status_not_parsed using (job_id)
        left join (
            select job_id, count(*) as parsed_count
            from attempts
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
            from attempts
            where coalesce(web_search_requirement_status, '') not in ('satisfied', 'not_applicable')
               or (web_search_requirement_status = 'satisfied' and coalesce(web_search_evidence, '') = '')
            group by job_id
        ) web_status_bad using (job_id)
        group by query_manifest_sha256, repeats, execution_window_bucket
        """
    )


def _iter_run_dirs(runs_root: Path) -> list[Path]:
    if not runs_root.exists():
        return []
    candidates: list[Path] = []
    for path in sorted(item for item in runs_root.iterdir() if item.is_dir()):
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
    analysis_summary = _read_analysis_summary(run_dir)
    analysis_current = _analysis_summary_is_current(manifest, analysis_summary)
    if not analysis_current and str(manifest.get("status") or "").startswith("analyzed"):
        _quality(con, job_id, "stale_or_missing_analysis_summary", "analysis_summary.json does not match latest run_generation", str(run_dir / "logs" / "analysis_summary.json"))
        counts["quality_flags"] += 1
    report_path = run_dir / "result" / "report.html"
    completed_at = _completed_at(run_dir, attempts)
    inferred_from_legacy = bool(
        sampling_profile.get("inferred_from_legacy")
        or analysis_profile.get("inferred_from_legacy")
        or comparability_profile.get("inferred_from_legacy")
    )
    data_quality = analysis_summary.get("data_quality") if analysis_current and isinstance(analysis_summary.get("data_quality"), dict) else {}
    con.execute(
        "insert into runs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            job_id,
            manifest.get("status"),
            manifest.get("created_at"),
            completed_at,
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
            analysis_summary.get("sample_mode", "") if analysis_current else "",
            bool(analysis_summary.get("partial_sample") or data_quality.get("partial_sample")) if analysis_current else None,
            _to_int(analysis_summary.get("success_record_count")) if analysis_current else None,
            _to_int(analysis_summary.get("stats_record_count")) if analysis_current else None,
            _to_int(manifest.get("run_generation")),
            inferred_from_legacy,
            (analysis_summary.get("job_conclusion_strength") or data_quality.get("conclusion_strength", "")) if analysis_current else "",
            len(attempts),
            comparability_profile.get("query_manifest_sha256") or info.get("sha256", ""),
            info.get("source_type", ""),
            info.get("source_uri", ""),
            str(report_path) if analysis_current and report_path.exists() else "",
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
            "insert into attempts values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                attempt["attempt_id"],
                attempt["query_id"],
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
                attempt["response_preview"],
                attempt["response_length"],
                attempt["raw_path"],
                attempt["raw_line_number"],
            ],
        )
        counts["attempts"] += 1
    planned_query_rows = _planned_query_rows(run_dir, manifest)
    for qid, row in query_rows.items():
        if qid in planned_query_rows:
            planned_query_rows[qid].update({key: value for key, value in row.items() if value not in (None, "")})
        else:
            planned_query_rows[qid] = row
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
    if analysis_current:
        counts["quality_flags"] += _ingest_csv_outputs(con, run_dir, job_id)
    elif (run_dir / "result").exists() and any((run_dir / "result").iterdir()):
        _quality(con, job_id, "stale_result_csv_ignored", "result CSV ignored because analysis_summary is stale or missing", str(run_dir / "result"))
        counts["quality_flags"] += 1
    return counts


def _read_attempts(raw_path: Path, job_id: str, fallback_rows: dict[str, dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    queries: dict[str, dict[str, str]] = {}
    query_field_priority: dict[str, dict[str, int]] = {}
    flags: list[dict[str, Any]] = []
    if not raw_path.exists():
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
                flags.append({"type": "query_meta_fallback_used", "message": "query_meta filled from fallback manifest", "path": str(raw_path), "raw_line_number": line_no, "query_id": qid})
            elif not meta:
                flags.append({"type": "query_meta_missing", "message": "query_meta missing", "path": str(raw_path), "raw_line_number": line_no, "query_id": qid})
            elif fallback:
                conflicts = [key for key, value in fallback.items() if key in meta and str(meta.get(key) or "") and str(value or "") and str(meta.get(key)) != str(value)]
                if conflicts:
                    flags.append({"type": "query_meta_conflict", "message": ",".join(conflicts), "path": str(raw_path), "raw_line_number": line_no, "query_id": qid})
            error = record.get("error") if isinstance(record.get("error"), dict) else {}
            sampling_profile = record.get("sampling_profile") if isinstance(record.get("sampling_profile"), dict) else {}
            response_text = str(record.get("response_text") or "")
            attempt_id = str(record.get("attempt_id") or f"{job_id}__{qid}__r{record.get('repeat_index', 1)}__{record.get('request_hash', '')}")
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "query_id": qid,
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
                    "created_at": str(record.get("started_at") or ""),
                    "completed_at": str(record.get("completed_at") or ""),
                    "response_preview": response_text[:500],
                    "response_length": len(response_text),
                    "raw_path": str(raw_path),
                    "raw_line_number": line_no,
                }
            )
            if qid:
                priority = 3 if record.get("status") in {"success", "mock"} and record.get("query_meta") else 2 if record.get("query_meta") else 1 if fallback else 0
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


def _ingest_csv_outputs(con: Any, run_dir: Path, job_id: str) -> int:
    result = run_dir / "result"
    quality_count = 0
    expected_files = [
        "brand_summary.csv",
        "brand_by_query.csv",
        "query_stability.csv",
        "source_domains.csv",
        "source_urls.csv",
        "quality_summary.csv",
        "attempt_facts.csv",
        "query_facts.csv",
        "brand_attempt_facts.csv",
    ]
    for name in expected_files:
        path = result / name
        if not path.exists():
            _quality(con, job_id, "missing_result_csv", f"result CSV missing: {name}", str(path))
            quality_count += 1
    for row in _read_csv(result / "brand_summary.csv"):
        con.execute("insert into brand_summary values (?, ?, ?, ?, ?, ?)", [job_id, row.get("brand_name_canonical", ""), _pct(row.get("sov_event_share")), _pct(row.get("response_mention_rate")), _pct(row.get("query_coverage_rate")), _to_int(row.get("is_target_brand"))])
    for row in _read_csv(result / "brand_by_query.csv"):
        con.execute("insert into brand_by_query values (?, ?, ?, ?, ?)", [job_id, row.get("query_id", ""), row.get("brand_name_canonical", ""), _to_int(row.get("responses_mentioned")), _pct(row.get("mention_rate_within_query"))])
    for row in _read_csv(result / "query_stability.csv"):
        con.execute("insert into query_stability values (?, ?, ?, ?, ?)", [job_id, row.get("query_id", ""), _to_int(row.get("successful_repeats")), _to_int(row.get("expected_repeats")), _to_float(row.get("brand_set_jaccard_avg"))])
    for row in _read_csv(result / "source_domains.csv"):
        con.execute("insert into source_domains values (?, ?, ?, ?)", [job_id, row.get("domain", ""), _pct(row.get("response_coverage_rate")), _pct(row.get("query_coverage_rate"))])
    for row in _read_csv(result / "source_urls.csv"):
        con.execute("insert into source_urls values (?, ?, ?, ?, ?)", [job_id, row.get("url", ""), row.get("domain", ""), row.get("title", ""), _to_int(row.get("parsed_source_occurrences"))])
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
            "insert into attempt_facts values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                row.get("query_id", ""),
                _to_int(row.get("repeat_index")),
                row.get("latest_status", ""),
                row.get("completed_at", ""),
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
    return quality_count


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
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _quality(con: Any, job_id: str, type: str, message: str, path: str, raw_line_number: int | None = None, query_id: str = "") -> None:
    con.execute("insert into quality_flags values (?, ?, ?, ?, ?, ?)", [job_id, type, message, path, raw_line_number, query_id])


def _completed_at(run_dir: Path, attempts: list[dict[str, Any]]) -> str:
    summary = run_dir / "logs" / "run_summary.json"
    if summary.exists():
        try:
            return str(json.loads(summary.read_text(encoding="utf-8")).get("completed_at") or "")
        except Exception:
            pass
    values = [str(row.get("completed_at") or "") for row in attempts if row.get("completed_at")]
    return max(values) if values else ""


def _read_analysis_summary(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "logs" / "analysis_summary.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _analysis_summary_is_current(manifest: dict[str, Any], summary: dict[str, Any]) -> bool:
    if not str(manifest.get("status") or "").startswith("analyzed"):
        return False
    if not summary:
        return False
    manifest_generation = _to_int(manifest.get("run_generation")) or 0
    summary_generation = _to_int(summary.get("run_generation")) or 0
    return manifest_generation == summary_generation


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
        "runs": {"job_id", "status", "sample_mode", "partial_sample", "study_fingerprint", "sampling_fingerprint", "job_conclusion_strength"},
        "queries": {"job_id", "query_id", "query", "query_metadata_json"},
        "attempts": {"job_id", "attempt_id", "query_id", "web_search_requirement_status", "source_parse_status"},
        "quality_summary": {"job_id", "sample_mode", "stats_record_count"},
        "attempt_facts": {"job_id", "query_id", "repeat_index", "latest_status", "stats_included"},
        "query_facts": {"job_id", "query_id", "planned_attempts", "usable_sample_rate"},
        "brand_attempt_facts": {"job_id", "query_id", "brand_name_canonical", "stats_included"},
        "comparison_cohorts": {"query_manifest_sha256", "comparison_conclusion_strength", "source_metrics_comparable"},
    }
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
