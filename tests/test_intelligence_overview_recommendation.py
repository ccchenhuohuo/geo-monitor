from copy import deepcopy

from geo_monitor.analysis.intelligence import aggregate_recommendations, compute_overview_scores


def test_overview_scores_are_explainable_and_source_zero_is_na():
    rows = [
        {
            "job_id": "j1",
            "brand_name_canonical": "Target",
            "response_mention_rate": "60%",
            "query_coverage_rate": "50%",
            "top3_rate": "50%",
            "avg_rank_position": 2,
            "sov_response_share": "40%",
            "recommendation_conversion": "50%",
            "weighted_recommendation_score": 75,
            "target_win_rate": "75%",
            "replacement_risk": "25%",
            "citation_count": 0,
            "source_coverage_rate": "90%",
            "usable_sample_rate": "80%",
            "sample_completeness": "90%",
            "confidence_health": "80%",
            "extraction_error_rate": "10%",
            "planned_attempts": 10,
            "eligible_attempts": 8,
        }
    ]
    original = deepcopy(rows)

    result = compute_overview_scores(rows)

    assert rows == original
    assert result == [
        {
            "job_id": "j1",
            "brand_name_canonical": "Target",
            "visibility_score": 52.0,
            "recommendation_score": 62.5,
            "competitor_score": 75.0,
            "source_score": None,
            "quality_score": 84.0,
            "visibility_breakdown": {
                "mention_rate": {"value": 0.6, "weight": 0.3, "weighted_value": 0.18},
                "query_coverage": {"value": 0.5, "weight": 0.25, "weighted_value": 0.125},
                "prominence_score": {"value": 0.5, "weight": 0.2, "weighted_value": 0.1},
                "rank_score": {"value": 0.5, "weight": 0.15, "weighted_value": 0.075},
                "sov_score": {"value": 0.4, "weight": 0.1, "weighted_value": 0.04},
            },
            "recommendation_breakdown": {"recommendation_conversion": 0.5, "weighted_recommendation_score": 0.75},
            "competitor_breakdown": {"target_win_rate": 0.75, "inverse_replacement_risk": 0.75},
            "source_breakdown": {"source_coverage": 0.9, "source_diversity": None, "owned_source_rate": None},
            "quality_breakdown": {
                "usable_sample_rate": 0.8,
                "sample_completeness": 0.9,
                "confidence_health": 0.8,
                "extraction_success_rate": 0.9,
            },
            "planned_attempts": 10,
            "eligible_attempts": 8,
            "sample_completeness": "90%",
            "usable_sample_rate": "80%",
        }
    ]


def test_recommendations_keep_strongest_attempt_brand_and_reject_untrusted_rows():
    rows = [
        _brand("a1", "q1", "Target", "top_pick", rank=1, confidence=0.9),
        _brand("a1", "q1", "Target", "recommended", rank=2, confidence=0.8),
        _brand("a2", "q2", "Target", "warning", confidence=0.7),
        _brand("a3", "q3", "Target", "recommended", confidence=0.2),
        {**_brand("a4", "q4", "Target", "recommended", confidence=0.9), "evidence": ""},
    ]
    attempts = [{"job_id": "j1", "query_id": f"q{i}", "repeat_index": 1, "stats_included": 1} for i in range(1, 5)]

    result = aggregate_recommendations(rows, attempts)

    assert len(result) == 1
    target = result[0]
    assert target["eligible_attempts"] == 4
    assert target["input_record_count"] == 5
    assert target["core_record_count"] == 2
    assert target["rejected_record_count"] == 3
    assert target["recommendation_type_distribution"]["top_pick"] == 1
    assert target["recommendation_type_distribution"]["warning"] == 1
    assert target["recommendation_conversion"] == 0.5
    assert target["weighted_recommendation_strength"] == 25.0
    assert target["weighted_recommendation_score"] == 62.5
    assert target["trace_attempt_ids"] == ["a1", "a2"]


def _brand(attempt_id, query_id, brand, recommendation_type, *, rank="", confidence=0.9):
    return {
        "job_id": "j1",
        "attempt_id": attempt_id,
        "query_id": query_id,
        "repeat_index": 1,
        "brand_name_canonical": brand,
        "recommendation_type": recommendation_type,
        "rank_position": rank,
        "confidence": confidence,
        "evidence": f"evidence for {brand}",
        "stats_included": 1,
    }
