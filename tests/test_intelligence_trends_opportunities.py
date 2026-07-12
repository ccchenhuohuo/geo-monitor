import pytest

from geo_monitor.analysis.intelligence import (
    build_opportunity_tables,
    build_query_opportunities,
    build_source_opportunities,
    compute_presence_volatility,
    compute_run_deltas,
    compute_topk_drift,
    compute_volatility,
)


def test_trends_return_na_for_zero_baseline_and_explicit_topk_drift():
    scores = [
        {"job_id": "r1", "completed_at": "2026-01-01", "brand_name_canonical": "A", "visibility_score": 50, "recommendation_score": 0},
        {"job_id": "r2", "completed_at": "2026-01-02", "brand_name_canonical": "A", "visibility_score": 60, "recommendation_score": 10},
    ]

    deltas = compute_run_deltas(scores)

    visibility = next(row for row in deltas if row["metric"] == "visibility_score")
    recommendation = next(row for row in deltas if row["metric"] == "recommendation_score")
    assert visibility["absolute_delta"] == 10
    assert visibility["relative_delta"] == 0.2
    assert recommendation["absolute_delta"] == 10
    assert recommendation["relative_delta"] is None
    assert recommendation["delta_denominator"] == 0

    ranking = [
        {"job_id": "r1", "completed_at": "2026-01-01", "brand_name_canonical": "A", "sov_response_share": 60},
        {"job_id": "r1", "completed_at": "2026-01-01", "brand_name_canonical": "B", "sov_response_share": 40},
        {"job_id": "r2", "completed_at": "2026-01-02", "brand_name_canonical": "B", "sov_response_share": 70},
        {"job_id": "r2", "completed_at": "2026-01-02", "brand_name_canonical": "C", "sov_response_share": 30},
    ]
    drift = compute_topk_drift(ranking, k=2)[0]
    assert drift["top_k_baseline"] == ["A", "B"]
    assert drift["top_k_current"] == ["B", "C"]
    assert drift["jaccard_distance"] == pytest.approx(2 / 3)


def test_volatility_separates_within_and_between_run_denominators():
    rows = [
        {"job_id": "r1", "brand_name_canonical": "A", "value": 1},
        {"job_id": "r1", "brand_name_canonical": "A", "value": 3},
        {"job_id": "r2", "brand_name_canonical": "A", "value": 5},
        {"job_id": "r2", "brand_name_canonical": "A", "value": 7},
    ]

    result = compute_volatility(rows, metric_field="value")[0]

    assert result["within_run_volatility"] == 1.0
    assert result["within_run_denominator"] == 2
    assert result["between_run_volatility"] == 2.0
    assert result["between_run_denominator"] == 2


def test_presence_volatility_uses_eligible_attempts_and_explicit_zeroes():
    attempts = [
        {"job_id": "r1", "query_id": "q1", "repeat_index": 1, "attempt_id": "a1", "stats_included": True},
        {"job_id": "r1", "query_id": "q1", "repeat_index": 2, "attempt_id": "a2", "stats_included": True},
        {"job_id": "r1", "query_id": "q1", "repeat_index": 3, "attempt_id": "a3", "stats_included": False},
    ]
    brands = [
        {
            "job_id": "r1",
            "query_id": "q1",
            "repeat_index": 1,
            "attempt_id": "a1",
            "brand_name_canonical": "A",
            "sov_eligible": True,
            "confidence": 0.8,
        }
    ]

    rows = compute_presence_volatility(attempts, brands, brands={"A", "NeverSeen"})
    by_brand = {row["brand_name_canonical"]: row for row in rows}

    assert by_brand["A"]["observation_count"] == 2
    assert by_brand["A"]["presence_count"] == 1
    assert by_brand["A"]["within_run_volatility"] == 0.5
    assert by_brand["A"]["within_run_observation_count"] == 2
    assert by_brand["A"]["trace_attempt_ids"] == ["a1", "a2"]
    assert by_brand["A"]["confidence"] == 0.8
    assert by_brand["NeverSeen"]["presence_count"] == 0
    assert by_brand["NeverSeen"]["within_run_volatility"] == 0.0
    assert by_brand["NeverSeen"]["confidence"] is None


def test_presence_volatility_is_na_when_only_one_eligible_attempt():
    attempts = [
        {
            "job_id": "r1",
            "query_id": "q1",
            "repeat_index": 1,
            "attempt_id": "a1",
            "stats_included": True,
        }
    ]

    row = compute_presence_volatility(attempts, [], brands={"Target"})[0]

    assert row["within_run_volatility"] is None
    assert row["within_run_denominator"] == 0
    assert row["within_run_observation_count"] == 0


def test_opportunities_use_only_rule_factors_and_keep_trace_keys():
    query = build_query_opportunities(
        [
            {
                "job_id": "j1",
                "query_id": "q1",
                "query": "best option",
                "competitor_brand": "Peer",
                "competitor_visibility": "80%",
                "competitor_recommendation_strength": 50,
                "query_quality_score": 90,
                "attempt_id": "a1",
            }
        ]
    )
    source = build_source_opportunities(
        [
            {
                "job_id": "j1",
                "competitor_brand": "Peer",
                "domain": "news.example",
                "canonical_url": "https://news.example/review",
                "source_gap_rate": "50%",
                "source_quality_score": 80,
                "trace_attempt_ids": ["a2"],
            }
        ]
    )

    assert query[0]["opportunity_score"] == 36.0
    assert query[0]["trace_query_ids"] == ["q1"]
    assert query[0]["trace_attempt_ids"] == ["a1"]
    assert source[0]["opportunity_score"] == 40.0
    assert source[0]["factor_breakdown"]["target_source_gap"] == 1.0
    combined = build_opportunity_tables(
        query_rows=[
            {
                "job_id": "j1",
                "query_id": "q1",
                "competitor_brand": "Peer",
                "competitor_visibility": "80%",
                "competitor_recommendation_strength": 50,
                "query_quality_score": 90,
            }
        ],
        source_rows=[
            {
                "job_id": "j1",
                "competitor_brand": "Peer",
                "domain": "news.example",
                "canonical_url": "https://news.example/review",
                "source_gap_rate": "50%",
                "source_quality_score": 80,
            }
        ],
    )
    assert [row["opportunity_type"] for row in combined] == ["source_gap", "query_gap"]
