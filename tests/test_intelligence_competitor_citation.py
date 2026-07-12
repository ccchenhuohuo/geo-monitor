import pytest

from geo_monitor.analysis.intelligence import (
    aggregate_citations,
    canonicalize_url,
    compute_competitor_intelligence,
    compute_source_gaps,
    summarize_citations,
)


def test_competitor_win_loss_tie_replacement_and_rank_gap_are_auditable():
    rows = [
        _brand("a1", "q1", "Target", "recommended", 2),
        _brand("a1", "q1", "Peer", "recommended", 1),
        _brand("a2", "q2", "Target", "top_pick", 1),
        _brand("a2", "q2", "Peer", "recommended", 2),
        _brand("a3", "q3", "Target", "mentioned_only", ""),
        _brand("a3", "q3", "Peer", "mentioned_only", ""),
        _brand("a4", "q4", "Peer", "recommended", 1),
    ]
    attempts = [{"job_id": "j1", "query_id": f"q{i}", "repeat_index": 1, "stats_included": 1} for i in range(1, 6)]

    result = compute_competitor_intelligence(rows, "Target", attempts)

    assert len(result) == 1
    edge = result[0]
    assert edge["co_occurrence_count"] == 3
    assert edge["jaccard_similarity"] == 0.75
    assert (edge["target_wins"], edge["competitor_wins"], edge["ties"]) == (1, 1, 1)
    assert edge["win_loss_denominator"] == 2
    assert edge["target_win_rate"] == 0.5
    assert edge["replacement_count"] == 1
    assert edge["replacement_denominator"] == 5
    assert edge["replacement_risk"] == 0.2
    assert edge["target_top_pick_share"] == 1.0
    assert edge["competitor_top_pick_share"] == 0.0
    assert edge["avg_rank_gap"] == 0.0
    assert edge["rank_gap_distribution"] == {"target_ahead": 1, "same_rank": 0, "competitor_ahead": 1}
    assert edge["trace_replacement_keys"] == [["j1", "q4", 1]]


def test_citations_canonicalize_attribute_summarize_and_find_gaps():
    assert canonicalize_url("http://WWW.Example.com/a/?utm_source=x#frag") == "http://example.com/a"
    assert canonicalize_url("http://[::1]/a") == "http://[::1]/a"
    assert canonicalize_url("https://[2001:db8::1]:8443/a") == "https://[2001:db8::1]:8443/a"
    assert canonicalize_url("https://example.com:bad/a") is None
    assert canonicalize_url("ftp://example.com/a") is None
    sources = [
        _source("a1", "q1", "http://WWW.Example.com/a/?utm_source=x#frag", anchor="Target"),
        _source("a2", "q2", "https://example.com/a", anchor="Peer"),
        _source("a3", "q3", "https://reddit.com/r/test?gclid=x"),
        _source("a4", "q4", "https://news.example/review", anchor="Peer"),
    ]
    brands = [
        _brand("a1", "q1", "Target", "mentioned_only", ""),
        _brand("a2", "q2", "Peer", "mentioned_only", ""),
        _brand("a3", "q3", "Target", "mentioned_only", ""),
        _brand("a3", "q3", "Peer", "mentioned_only", ""),
        _brand("a4", "q4", "Peer", "mentioned_only", ""),
    ]

    citations = aggregate_citations(sources, brands, owned_domains={"example.com"})

    assert len(citations) == 5
    target_owned = next(row for row in citations if row["brand_name_canonical"] == "Target" and row["domain"] == "example.com")
    assert target_owned["source_type"] == "official_website"
    assert target_owned["source_attribution_method"] == "anchor"
    target_reddit = next(row for row in citations if row["brand_name_canonical"] == "Target" and row["domain"] == "reddit.com")
    assert target_reddit["source_type"] == "forum_qa"
    assert target_reddit["source_attribution_method"] == "answer_cooccurrence"

    attempts = [{"job_id": "j1", "query_id": f"q{i}", "repeat_index": 1, "stats_included": 1} for i in range(1, 6)]
    target_summary = next(row for row in summarize_citations(citations, attempts) if row["brand_name_canonical"] == "Target")
    assert target_summary["citation_count"] == 2
    assert target_summary["source_coverage_rate"] == 0.4
    assert target_summary["source_diversity_score"] == 1.0
    assert target_summary["owned_source_rate"] == 0.5
    assert target_summary["source_score"] == pytest.approx(63.33, abs=0.01)
    no_source = summarize_citations([], attempts, [{"brand_name_canonical": "NoSource"}])[0]
    assert no_source["citation_count"] == 0
    assert no_source["source_score"] is None

    gaps = compute_source_gaps(citations, "Target")
    assert [(row["competitor_brand"], row["domain"]) for row in gaps] == [
        ("Peer", "example.com"),
        ("Peer", "news.example"),
    ]
    assert gaps[0]["competitor_citation_denominator"] == 3


def test_source_coverage_deduplicates_multiple_urls_from_one_response():
    sources = [
        _source("a1", "q1", "https://one.example/a", anchor="Target"),
        _source("a1", "q1", "https://two.example/b", anchor="Target"),
    ]
    attempts = [{"job_id": "j1", "query_id": "q1", "repeat_index": 1, "stats_included": 1}]

    citations = aggregate_citations(sources)
    summary = summarize_citations(citations, attempts, owned_domains_configured=False)[0]

    assert summary["citation_count"] == 2
    assert summary["source_coverage_rate"] == 1.0
    assert summary["owned_source_rate"] is None


def _brand(attempt_id, query_id, brand, recommendation_type, rank):
    return {
        "job_id": "j1",
        "attempt_id": attempt_id,
        "query_id": query_id,
        "repeat_index": 1,
        "brand_name_canonical": brand,
        "recommendation_type": recommendation_type,
        "rank_position": rank,
        "confidence": 0.9,
        "evidence": brand,
        "stats_included": 1,
    }


def _source(attempt_id, query_id, url, *, anchor=""):
    return {
        "job_id": "j1",
        "attempt_id": attempt_id,
        "query_id": query_id,
        "repeat_index": 1,
        "url": url,
        "anchor_brand": anchor,
    }
