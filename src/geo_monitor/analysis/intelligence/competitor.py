"""Deterministic target-versus-competitor intelligence."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .common import as_bool, mean, positive_int, response_key, safe_div, trace_fields
from .recommendation import RECOMMENDATION_WEIGHTS, strongest_attempt_brand_rows


def compute_competitor_intelligence(
    brand_attempt_facts: list[dict[str, Any]],
    target_brand: str,
    attempt_facts: list[dict[str, Any]] | None = None,
    *,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    """Build one auditable edge per target/competitor pair.

    Recommendation weight decides first, rank decides only when weights tie, and
    missing/equal rank remains a tie.  Ties are deliberately excluded from the
    win/loss denominator.
    """

    core = strongest_attempt_brand_rows(brand_attempt_facts, min_confidence=min_confidence)
    by_brand: dict[str, dict[tuple[str, str, int | str], dict[str, Any]]] = defaultdict(dict)
    canonical_names: dict[str, str] = {}
    for row in core:
        display = str(row.get("brand_name_canonical") or row.get("brand_name_raw") or "").strip()
        key = _brand_key(display)
        if not key:
            continue
        canonical_names.setdefault(key, display)
        by_brand[key][response_key(row)] = row

    target_key = _brand_key(target_brand)
    canonical_names.setdefault(target_key, target_brand)
    target_rows = by_brand.get(target_key, {})
    if attempt_facts is not None:
        eligible_keys = {response_key(row) for row in attempt_facts if as_bool(row.get("stats_included"), default=True)}
    else:
        eligible_keys = {key for rows in by_brand.values() for key in rows}

    output: list[dict[str, Any]] = []
    for competitor_key in sorted(key for key in by_brand if key != target_key):
        competitor_rows = by_brand[competitor_key]
        target_keys = set(target_rows)
        competitor_keys = set(competitor_rows)
        co_keys = target_keys & competitor_keys
        union_keys = target_keys | competitor_keys
        wins = losses = ties = 0
        rank_gaps: list[float] = []
        win_trace: list[dict[str, Any]] = []
        loss_trace: list[dict[str, Any]] = []
        tie_trace: list[dict[str, Any]] = []
        for key in sorted(co_keys):
            target_row = target_rows[key]
            competitor_row = competitor_rows[key]
            outcome, gap = _pair_outcome(target_row, competitor_row)
            if gap is not None:
                rank_gaps.append(gap)
            if outcome == "target_win":
                wins += 1
                win_trace.extend([target_row, competitor_row])
            elif outcome == "competitor_win":
                losses += 1
                loss_trace.extend([target_row, competitor_row])
            else:
                ties += 1
                tie_trace.extend([target_row, competitor_row])

        replacement_keys = {
            key for key, row in competitor_rows.items() if key not in target_keys and RECOMMENDATION_WEIGHTS[str(row["recommendation_type"])] > 0
        }
        decisive = wins + losses
        rank_distribution = {
            "target_ahead": sum(1 for value in rank_gaps if value > 0),
            "same_rank": sum(1 for value in rank_gaps if value == 0),
            "competitor_ahead": sum(1 for value in rank_gaps if value < 0),
        }
        target_top_picks = sum(1 for row in target_rows.values() if row["recommendation_type"] == "top_pick")
        competitor_top_picks = sum(1 for row in competitor_rows.values() if row["recommendation_type"] == "top_pick")
        pair_top_picks = target_top_picks + competitor_top_picks
        pair_rows = [row for key in co_keys for row in (target_rows[key], competitor_rows[key])]
        output.append(
            {
                "target_brand": canonical_names[target_key],
                "competitor_brand": canonical_names[competitor_key],
                "eligible_attempts": len(eligible_keys),
                "target_presence_attempts": len(target_keys),
                "competitor_presence_attempts": len(competitor_keys),
                "pair_union_attempts": len(union_keys),
                "co_occurrence_count": len(co_keys),
                "co_occurrence_rate_target": safe_div(len(co_keys), len(target_keys)),
                "jaccard_similarity": safe_div(len(co_keys), len(union_keys)),
                "target_wins": wins,
                "competitor_wins": losses,
                "ties": ties,
                "win_loss_denominator": decisive,
                "target_win_rate": safe_div(wins, decisive),
                "competitor_win_rate": safe_div(losses, decisive),
                "replacement_count": len(replacement_keys),
                "replacement_denominator": len(eligible_keys),
                "replacement_risk": safe_div(len(replacement_keys), len(eligible_keys)),
                "target_top_pick_count": target_top_picks,
                "competitor_top_pick_count": competitor_top_picks,
                "top_pick_denominator": pair_top_picks,
                "target_top_pick_share": safe_div(target_top_picks, pair_top_picks),
                "competitor_top_pick_share": safe_div(competitor_top_picks, pair_top_picks),
                "avg_rank_gap": None if not rank_gaps else round(float(mean(rank_gaps)), 3),
                "rank_gap_observed_count": len(rank_gaps),
                "rank_gap_distribution": rank_distribution,
                "rank_advantage_score": safe_div(rank_distribution["target_ahead"] - rank_distribution["competitor_ahead"], len(rank_gaps)),
                "trace_co_occurrence_keys": [list(key) for key in sorted(co_keys)],
                "trace_replacement_keys": [list(key) for key in sorted(replacement_keys)],
                "trace_target_win_attempt_ids": trace_fields(win_trace)["trace_attempt_ids"],
                "trace_competitor_win_attempt_ids": trace_fields(loss_trace)["trace_attempt_ids"],
                "trace_tie_attempt_ids": trace_fields(tie_trace)["trace_attempt_ids"],
                **trace_fields(pair_rows),
            }
        )
    return output


def build_competitor_edges(
    brand_attempt_facts: list[dict[str, Any]],
    target_brand: str,
    attempt_facts: list[dict[str, Any]] | None = None,
    *,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    return compute_competitor_intelligence(
        brand_attempt_facts,
        target_brand,
        attempt_facts,
        min_confidence=min_confidence,
    )


def _pair_outcome(target: dict[str, Any], competitor: dict[str, Any]) -> tuple[str, float | None]:
    target_weight = RECOMMENDATION_WEIGHTS[str(target["recommendation_type"])]
    competitor_weight = RECOMMENDATION_WEIGHTS[str(competitor["recommendation_type"])]
    target_rank = positive_int(target.get("rank_position"))
    competitor_rank = positive_int(competitor.get("rank_position"))
    gap = float(competitor_rank - target_rank) if target_rank is not None and competitor_rank is not None else None
    if target_weight > competitor_weight:
        return "target_win", gap
    if competitor_weight > target_weight:
        return "competitor_win", gap
    if target_rank is not None and competitor_rank is not None:
        if target_rank < competitor_rank:
            return "target_win", gap
        if competitor_rank < target_rank:
            return "competitor_win", gap
    return "tie", gap


def _brand_key(value: str) -> str:
    return "".join(value.casefold().split())
