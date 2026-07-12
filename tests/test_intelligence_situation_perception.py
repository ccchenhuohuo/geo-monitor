import pytest

from geo_monitor.analysis.intelligence import aggregate_perception, aggregate_situations, perception_quality_flags


def test_situation_uses_macro_by_query_and_preserves_denominator_queries():
    queries = [
        _query("q1", 2, 2, "p1", "s1", "buy", "t1"),
        _query("q2", 2, 1, "p1", "s1", "buy", "t2"),
        _query("q3", 2, 2, "p2", "s2", "learn", "t3"),
    ]
    brands = [
        _brand("a1", "q1", 1, "top_pick", 1),
        _brand("a2", "q1", 2, "mentioned_only", 2),
        _brand("a3", "q3", 1, "mentioned_only", 3),
    ]

    rows = aggregate_situations(brands, queries, "Target")

    p1 = next(row for row in rows if row["segment_dimension"] == "persona" and row["segment_value"] == "p1")
    assert p1["visibility_rate_macro_by_query"] == 0.5
    assert p1["visibility_rate_micro"] == pytest.approx(2 / 3)
    assert p1["planned_attempts"] == 4
    assert p1["eligible_attempts"] == 3
    assert p1["trace_query_ids"] == ["q1", "q2"]
    assert p1["quality_score"] == 75.0
    scenario = next(row for row in rows if row["segment_dimension"] == "scenario" and row["segment_value"] == "t2")
    assert scenario["visibility_rate_macro_by_query"] == 0.0
    assert scenario["trace_query_ids"] == ["q2"]


def test_perception_requires_evidence_confidence_and_traceability():
    records = [
        _claim("a1", "q1", "strength", "fast", "Fast delivery", 0.9),
        _claim("a2", "q2", "strength", "fast", "Delivery is fast", 0.7),
        _claim("a3", "q3", "strength", "fast", "Maybe fast", 0.2),
        {**_claim("a4", "q4", "pricing", "premium", "Premium price", 0.8), "evidence": ""},
    ]
    attempts = [{"job_id": "j1", "query_id": f"q{i}", "repeat_index": 1, "stats_included": 1} for i in range(1, 4)]

    result = aggregate_perception(records, attempts)
    flags = perception_quality_flags(records)

    assert len(result) == 1
    row = result[0]
    assert row["claim_type"] == "strength"
    assert row["record_count"] == 2
    assert row["response_count"] == 2
    assert row["eligible_attempts"] == 3
    assert row["response_rate"] == pytest.approx(2 / 3)
    assert row["avg_confidence"] == 0.8
    assert row["rejected_record_count"] == 1
    assert row["trace_attempt_ids"] == ["a1", "a2"]
    assert {reason for flag in flags for reason in flag["reasons"]} >= {"low_confidence", "missing_evidence"}


def _query(query_id, planned, eligible, persona, seed, intent, template):
    return {
        "job_id": "j1",
        "query_id": query_id,
        "query": query_id,
        "planned_attempts": planned,
        "stats_included_attempts": eligible,
        "query_metadata_json": f'{{"persona":"{persona}","seed_id":"{seed}","intent":"{intent}","template_id":"{template}"}}',
    }


def _brand(attempt_id, query_id, repeat, recommendation_type, rank):
    return {
        "job_id": "j1",
        "attempt_id": attempt_id,
        "query_id": query_id,
        "repeat_index": repeat,
        "brand_name_canonical": "Target",
        "recommendation_type": recommendation_type,
        "rank_position": rank,
        "confidence": 0.9,
        "evidence": "Target",
        "stats_included": 1,
    }


def _claim(attempt_id, query_id, claim_type, canonical, text, confidence):
    return {
        "job_id": "j1",
        "attempt_id": attempt_id,
        "query_id": query_id,
        "repeat_index": 1,
        "brand_name_canonical": "Target",
        "claim_type": claim_type,
        "claim_canonical": canonical,
        "claim_text": text,
        "evidence": text,
        "confidence": confidence,
        "is_traceable": True,
    }
