"""DuckDB schema and derived-view construction."""

from __future__ import annotations

import json
from typing import Any

from .contracts import DUCKDB_SCHEMA_VERSION, INTELLIGENCE_CSV_STEMS
from .query import _quote_identifier


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
            job_id varchar, brand_name_canonical varchar, sov_response_share double,
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


def _merge_intelligence_types(current: str | None, incoming: str) -> str:
    if current is None or current == incoming:
        return incoming
    if {current, incoming} <= {"BIGINT", "DOUBLE"}:
        return "DOUBLE"
    return "VARCHAR"


def _json_path(column: str) -> str:
    return "$." + json.dumps(column, ensure_ascii=False)


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
