# Metrics Reference

This document defines the current CSV/report metric contract for GEO Brand
Monitor. Raw attempts remain the source of truth; CSVs, DuckDB, reports, and
dashboards are rebuildable analysis layers.

## Shared Terms

- Attempt grain: the latest terminal attempt for one `query_id` and
  `repeat_index`. A later `error` supersedes an older success and is excluded
  from stats with a data-quality flag.
- Response grain: a latest terminal attempt whose status is eligible for the
  current sample mode (`success` for live, `mock` only when explicitly included).
- Brand response hit: one SOV-eligible canonical brand appearing in one response.
  Multiple raw mentions of the same canonical brand in the same response are
  deduped.
- Mention event grain: currently the same as brand response hit for exported
  SOV fields. `sov_event_share` and `sov_response_share` intentionally overlap
  until a future event-level metric migration is designed.
- Query grain: planned query IDs from the job manifest or the preserved raw
  `query_meta` fallback.
- Mock samples: excluded unless `analyze-job --include-mock` or the Python API
  sets `include_mock=True` or `mock=True`.
- Partial samples: data quality sets `conclusion_strength=observational` when
  samples are incomplete, duplicated, malformed, contract-mismatched, or
  extraction has errors/quarantined rows.
- Provider/search/source evidence: missing web-search evidence, unverifiable
  required search, legacy-inferred profiles, or non-comparable URL source
  parsing also downgrade conclusions to observational.
- Traceability quarantine: non-traceable extracted rows are excluded from brand
  metrics and logged in `logs/extraction_errors.jsonl`; traceable rows from the
  same response are retained.

## Facts Layer

The analysis step also writes denominator facts. These files are the stable
foundation for later intelligence scoring and DuckDB/dashboard views:

| File | Grain | Purpose |
|---|---|---|
| `quality_summary.csv` | Job | Sample mode, conclusion strength, partial flags, record counts, and evidence-quality counts |
| `attempt_facts.csv` | Latest terminal query/repeat | Planned vs completed attempts, valid/stats-included status, request hash, web/source evidence |
| `query_facts.csv` | Planned query | Planned attempts, terminal/completed/stats-included counts, usable sample rate, frozen query metadata |
| `brand_attempt_facts.csv` | Brand/query/repeat | SOV-eligible brand facts traceable back to a stats-included attempt |

Downstream recommendation, competitor, citation, or overview scores should be
derived from these facts or DuckDB views, not directly from raw summary CSVs.

## Brand Summary

Exported in `brand_summary.csv`, `sov_summary.csv`, and
`discovered_brands.csv`.

| Metric | Numerator | Denominator / Grain |
|---|---:|---|
| `responses_mentioned` | Responses where the canonical brand appears | Response grain |
| `response_mention_count` | Same as `responses_mentioned` | Response grain |
| `mention_rate` | `responses_mentioned` | Successful analysis responses |
| `response_mention_rate` | `responses_mentioned` | Successful analysis responses |
| `query_coverage` | Planned queries where brand appears at least once | Query grain |
| `query_coverage_count` | Same as `query_coverage` | Query grain |
| `query_coverage_rate` | `query_coverage` | Planned query count |
| `query_macro_mention_rate` | Mean per-query mention rate | For each planned query: mentioned repeats / expected repeats |
| `sov_response_share` | Brand response hits for this brand | All brand response hits across brands |
| `sov_event_share` | Currently compatible with `sov_response_share` | All brand response hits; reserved for a future true event-grain migration |
| `recommended_count` | Responses where brand is mentioned and recommended | Brand response hits |
| `recommended_rate` | `recommended_count` | Brand response hits for this brand |
| `recommended_rate_when_mentioned` | Same as `recommended_rate` | Brand response hits for this brand |
| `recommended_rate_over_success` | `recommended_count` | Successful analysis responses |
| `rank_observed_count` | Brand response hits with a positive rank | Brand response hits |
| `rank_observed_rate` | `rank_observed_count` | Brand response hits |
| `avg_rank_position` | Mean observed rank | Ranked brand response hits only |
| `top3_rate` | Brand response hits with rank <= 3 | Brand response hits |
| `positive_rate` | Dominant positive sentiment events | Brand response hits with sentiment bucket |
| `neutral_rate` | Dominant neutral sentiment events | Brand response hits with sentiment bucket |
| `negative_rate` | Dominant negative sentiment events | Brand response hits with sentiment bucket |
| `sentiment_unknown_rate` | Unknown sentiment events | Brand response hits with sentiment bucket |
| `sentiment_observed_rate` | Non-unknown sentiment events | Brand response hits with sentiment bucket |
| `avg_confidence` | Mean retained extraction confidence | Rows with numeric confidence for this brand |
| `is_target_brand` | 1 when canonical/raw names match target or aliases | Target alias normalization |
| `target_brand_detected` | Same target flag, repeated for convenience | Target alias normalization |
| `target_rank_by_sov` | Target brand rank by SOV | Only set on target rows |
| `target_sov_gap_to_leader` | Leader SOV minus target SOV | Percentage points |
| `target_sov_gap_to_top3_avg` | Top-3 average SOV minus target SOV | Percentage points |

## Brand By Query

Exported in `brand_by_query.csv`.

| Metric | Numerator | Denominator / Grain |
|---|---:|---|
| `responses_mentioned` | Repeats for this query where brand appears | Query/repeat grain |
| `mention_rate_within_query` | `responses_mentioned` | Expected repeats for the query |
| `recommended_responses` | Repeats where brand is recommended | Query/repeat grain |
| `recommended_rate_within_query` | `recommended_responses` | Mentioned repeats for this query/brand |
| `recommended_rate_when_mentioned_within_query` | Same as `recommended_rate_within_query` | Mentioned repeats for this query/brand |
| `recommended_rate_over_success_within_query` | `recommended_responses` | Expected repeats for the query |

## Query Stability

Exported in `query_stability.csv`.

| Metric | Numerator | Denominator / Grain |
|---|---:|---|
| `successful_repeats` | Latest successful/mock repeats included in analysis | Query/repeat grain |
| `expected_repeats` | Configured repeats | Query grain |
| `sample_sufficient` | 1 when enough repeats exist for a basic stability read | `successful_repeats >= min(2, expected_repeats)` |
| `brand_set_jaccard_avg` | Average pairwise Jaccard similarity | Non-empty brand sets across successful repeats |
| `unique_brand_sets` | Distinct canonical brand sets observed | Successful repeats |
| `top_brands` | Most common canonical brands in repeat brand sets | Successful repeats |

## Source Metrics

Exported in `source_domains.csv`, `source_urls.csv`, and
`source_by_query.csv`.

| Metric | Numerator | Denominator / Grain |
|---|---:|---|
| `parsed_source_occurrences` | Parsed citation/source records after per-response dedupe | Source record grain |
| `distinct_source_url_count` | Distinct URLs for a domain or query/domain | URL grain |
| `response_coverage` | Responses where a domain appears | Successful analysis responses |
| `response_coverage_rate` | `response_coverage` | Successful analysis responses |
| `query_coverage` | Queries where a domain appears | Planned query count |
| `query_coverage_rate` | `query_coverage` | Planned query count |
| `avg_source_order` | Mean parsed source rank/order | Source records with order |
| `best_source_order` | Minimum parsed source rank/order | Source records with order |
| `repeat_coverage` | Repeats for a query where domain appears | Successful repeats for that query |
| `repeat_coverage_rate` | `repeat_coverage` | Successful repeats for that query |

`citation_occurrences`, `avg_rank`, and `best_rank` may appear internally as
backward-compatible aliases, but the exported contract uses
`parsed_source_occurrences`, `avg_source_order`, and `best_source_order`.

## Data Quality

Exported in `logs/data_quality.json` and summarized in
`logs/analysis_summary.json`.

| Field | Meaning |
|---|---|
| `planned_units` | Planned query/repeat units, including unknown missing units when raw-only rebuild cannot hydrate the full manifest |
| `analysis_record_count` | Records eligible for analysis before stat exclusions |
| `stats_record_count` | Records retained for metrics after contract exclusions |
| `partial_sample` | True when completeness, traceability, or contract issues exist |
| `conclusion_strength` | `strong` or `observational` |
| `missing_units` | Planned query/repeat units not found in analyzable records |
| `extra_units` | Records not present in the planned manifest universe |
| `duplicate_units` | Multiple records for the same query/repeat in analysis statuses |
| `contract_mismatches` | Stored raw request/attempt fields that differ from manifest expectations |
| `raw_read_errors` | Malformed JSONL rows skipped during raw read |
| `invalid_records` | Records lacking valid query/repeat identity |
| `extraction_error_count` | Backward-compatible alias for response-level extraction error count |
| `extraction_error_record_count` | Responses with at least one extraction error or quarantine row; used as the numerator for `extraction_error_rate` |
| `extraction_error_row_count` | Extraction error detail rows logged for this analysis |
| `extraction_error_rate` | `extraction_error_record_count` / `analysis_record_count`, capped naturally by response grain |
| `traceability_quarantine_count` | Non-traceable extraction detail rows excluded from metrics |

## Cache Metrics

`logs/analysis_summary.json.cache` reports extraction and canonicalization cache
hits, misses, and writes. Cache keys include response text hash, extraction
schema version, model, and prompt version for extraction; canonicalization keys
include sorted raw-name hash, model, and prompt version.
