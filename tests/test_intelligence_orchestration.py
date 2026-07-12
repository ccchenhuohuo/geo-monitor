from geo_monitor.analysis.intelligence import INTELLIGENCE_TABLE_NAMES, build_intelligence_outputs


def test_orchestration_builds_complete_traceable_intelligence_contract():
    job_id = "job-1"
    attempt_facts = [
        {"job_id": job_id, "query_id": "q1", "repeat_index": 1, "attempt_id": "a1", "stats_included": 1},
        {"job_id": job_id, "query_id": "q2", "repeat_index": 1, "attempt_id": "a2", "stats_included": 1},
    ]
    query_facts = [
        {
            "job_id": job_id,
            "query_id": "q1",
            "query": "best for beginners",
            "planned_attempts": 1,
            "completed_attempts": 1,
            "stats_included_attempts": 1,
            "query_metadata_json": '{"seed_id":"s1","persona":"beginner","intent":"recommend","template_id":"intro"}',
        },
        {
            "job_id": job_id,
            "query_id": "q2",
            "query": "best premium choice",
            "planned_attempts": 1,
            "completed_attempts": 1,
            "stats_included_attempts": 1,
            "query_metadata_json": '{"seed_id":"s2","persona":"premium","intent":"compare","template_id":"premium"}',
        },
    ]
    brand_facts = [
        {
            "job_id": job_id,
            "query_id": "q1",
            "repeat_index": 1,
            "attempt_id": "a1",
            "brand_name_canonical": "Target",
            "is_target_brand": 1,
            "recommendation_type": "recommended",
            "is_recommended": 1,
            "rank_position": 2,
            "confidence": 0.9,
            "evidence": "Target is recommended",
            "traceability_status": "valid",
            "stats_included": 1,
        },
        {
            "job_id": job_id,
            "query_id": "q1",
            "repeat_index": 1,
            "attempt_id": "a1",
            "brand_name_canonical": "Competitor",
            "recommendation_type": "top_pick",
            "is_recommended": 1,
            "rank_position": 1,
            "confidence": 0.95,
            "evidence": "Competitor is the top pick",
            "traceability_status": "valid",
            "stats_included": 1,
        },
        {
            "job_id": job_id,
            "query_id": "q2",
            "repeat_index": 1,
            "attempt_id": "a2",
            "brand_name_canonical": "Competitor",
            "recommendation_type": "premium_pick",
            "is_recommended": 1,
            "rank_position": 1,
            "confidence": 0.9,
            "evidence": "Competitor is the premium pick",
            "traceability_status": "valid",
            "stats_included": 1,
        },
    ]
    mentions = [
        {
            "query_id": "q2",
            "repeat_index": 1,
            "brand_name_canonical": "Competitor",
            "brand_name_raw": "Competitor",
            "perception": [
                {
                    "claim_type": "strength",
                    "claim_text": "premium finish",
                    "evidence": "premium finish",
                    "confidence": 0.9,
                }
            ],
        }
    ]
    success_records = [
        {
            "query_id": "q2",
            "repeat_index": 1,
            "attempt_id": "a2",
            "sources": [
                {
                    "url": "https://reviews.example/item?utm_source=test",
                    "title": "review",
                    "raw": {"anchor_brand": "Competitor"},
                }
            ],
        }
    ]
    brand_summary = [
        {
            "brand_name_canonical": "Target",
            "response_mention_rate": "50.0%",
            "query_coverage_rate": "50.0%",
            "top3_rate": "100.0%",
            "avg_rank_position": 2,
            "sov_event_share": "33.3%",
            "sentiment_observed_rate": "100.0%",
        },
        {
            "brand_name_canonical": "Competitor",
            "response_mention_rate": "100.0%",
            "query_coverage_rate": "100.0%",
            "top3_rate": "100.0%",
            "avg_rank_position": 1,
            "sov_event_share": "66.7%",
            "sentiment_observed_rate": "100.0%",
        },
    ]

    outputs = build_intelligence_outputs(
        manifest={
            "job_id": job_id,
            "target_brand": "Target",
            "last_run_completed_at": "2026-01-01T00:00:00+00:00",
        },
        mentions=mentions,
        success_records=success_records,
        facts={
            "attempt_facts": attempt_facts,
            "query_facts": query_facts,
            "brand_attempt_facts": brand_facts,
            "quality_summary": [{"extraction_error_rate": "0.0%"}],
        },
        brand_summary=brand_summary,
    )

    assert set(outputs) == set(INTELLIGENCE_TABLE_NAMES)
    target_score = next(row for row in outputs["geo_overview_scores"] if row["brand_name_canonical"] == "Target")
    assert target_score["visibility_score"] is not None
    assert target_score["recommendation_score"] is not None
    assert target_score["competitor_score"] == 25.0
    assert target_score["source_score"] is None
    assert target_score["quality_score"] == 100.0
    assert outputs["recommendation_by_persona"]
    assert outputs["competitor_edges"][0]["competitor_wins"] == 1
    assert outputs["source_gaps"][0]["source_attribution_method"] == "anchor"
    assert outputs["visibility_by_persona"][0]["visibility_rate_macro_by_query"] is not None
    assert outputs["perception_strengths"][0]["trace_attempt_ids"] == ["a2"]
    assert outputs["opportunity_query_gaps"]
    assert outputs["opportunity_source_gaps"]
    assert outputs["opportunity_messaging_gaps"]


def test_orchestration_always_emits_zero_visibility_target_row():
    outputs = build_intelligence_outputs(
        manifest={"job_id": "j", "target_brand": "MissingTarget"},
        mentions=[],
        success_records=[],
        facts={
            "attempt_facts": [{"job_id": "j", "query_id": "q1", "repeat_index": 1, "stats_included": 1}],
            "query_facts": [
                {
                    "job_id": "j",
                    "query_id": "q1",
                    "planned_attempts": 1,
                    "latest_terminal_attempts": 1,
                    "completed_attempts": 1,
                    "valid_attempts": 1,
                    "stats_included_attempts": 1,
                }
            ],
            "brand_attempt_facts": [],
            "quality_summary": [{"extraction_error_rate": "0.0%"}],
        },
        brand_summary=[],
    )

    assert len(outputs["geo_overview_scores"]) == 1
    row = outputs["geo_overview_scores"][0]
    assert row["brand_name_canonical"] == "MissingTarget"
    assert row["visibility_score"] == 0.0
    assert row["recommendation_score"] is None
    assert row["competitor_score"] is None
    assert row["source_score"] is None
    assert row["quality_score"] == 100.0
