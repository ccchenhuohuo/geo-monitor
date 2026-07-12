"""Evidence- and confidence-gated perception record aggregation."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from .common import as_number, core_record_eligible, response_key, safe_div, trace_fields

PERCEPTION_TYPES = {"claim", "strength", "weakness", "pricing", "audience_fit", "persona_alignment"}


def perception_quality_flags(records: list[dict[str, Any]], *, min_confidence: float = 0.5) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for index, row in enumerate(records):
        reasons: list[str] = []
        if not str(row.get("evidence") or "").strip():
            reasons.append("missing_evidence")
        confidence = as_number(row.get("confidence"))
        if confidence is None or not 0.0 <= confidence <= 1.0:
            reasons.append("invalid_confidence")
        elif confidence < min_confidence:
            reasons.append("low_confidence")
        if row.get("is_traceable") is False or str(row.get("traceability_status") or "").lower() in {"invalid", "untraceable", "quarantined"}:
            reasons.append("untraceable")
        claim_type = _claim_type(row)
        if claim_type not in PERCEPTION_TYPES:
            reasons.append("invalid_claim_type")
        if not _claim_text(row):
            reasons.append("missing_claim")
        if reasons:
            flags.append(
                {
                    "record_index": index,
                    "brand_name_canonical": row.get("brand_name_canonical") or row.get("brand_name_raw") or "",
                    "query_id": row.get("query_id") or "",
                    "attempt_id": row.get("attempt_id") or "",
                    "reasons": reasons,
                }
            )
    return flags


def aggregate_perception(
    records: list[dict[str, Any]],
    attempt_facts: list[dict[str, Any]] | None = None,
    *,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    rejected_by_key: Counter[tuple[str, str, str]] = Counter()
    for source in records:
        row = dict(source)
        brand = str(row.get("brand_name_canonical") or row.get("brand_name_raw") or "").strip()
        claim_type = _claim_type(row)
        claim_text = _claim_text(row)
        canonical = str(row.get("claim_canonical") or _canonical_claim(claim_text)).strip()
        key = (brand, claim_type, canonical)
        if not brand or claim_type not in PERCEPTION_TYPES or not claim_text or not core_record_eligible(row, min_confidence=min_confidence):
            rejected_by_key[key] += 1
            continue
        row["claim_type"] = claim_type
        row["claim_text"] = claim_text
        row["claim_canonical"] = canonical
        valid.append(row)

    eligible_attempts = (
        len({response_key(row) for row in attempt_facts if str(row.get("stats_included", "1")).lower() not in {"0", "false", "no"}})
        if attempt_facts is not None
        else None
    )
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        grouped[
            (row["brand_name_canonical"] if row.get("brand_name_canonical") else str(row.get("brand_name_raw")), row["claim_type"], row["claim_canonical"])
        ].append(row)

    output: list[dict[str, Any]] = []
    for key, rows in sorted(grouped.items()):
        response_count = len({response_key(row) for row in rows})
        query_count = len({str(row.get("query_id") or "") for row in rows if row.get("query_id")})
        evidence = sorted({str(row.get("evidence") or "").strip() for row in rows if row.get("evidence")})
        claim_texts = Counter(str(row.get("claim_text") or "") for row in rows)
        output.append(
            {
                "brand_name_canonical": key[0],
                "claim_type": key[1],
                "claim_canonical": key[2],
                "representative_claim_text": claim_texts.most_common(1)[0][0],
                "record_count": len(rows),
                "response_count": response_count,
                "query_count": query_count,
                "eligible_attempts": eligible_attempts,
                "response_rate": safe_div(response_count, eligible_attempts) if eligible_attempts is not None else None,
                "avg_confidence": round(sum(float(row["confidence"]) for row in rows) / len(rows), 3),
                "evidence_samples": evidence[:5],
                "rejected_record_count": rejected_by_key[key],
                **trace_fields(rows),
            }
        )
    return output


def build_perception_intelligence(
    records: list[dict[str, Any]],
    attempt_facts: list[dict[str, Any]] | None = None,
    *,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    return aggregate_perception(records, attempt_facts, min_confidence=min_confidence)


def _claim_type(row: dict[str, Any]) -> str:
    value = str(row.get("claim_type") or row.get("perception_type") or row.get("type") or "claim").strip().lower()
    aliases = {"strengths": "strength", "weaknesses": "weakness", "pricing_signal": "pricing", "audience": "audience_fit"}
    return aliases.get(value, value)


def _claim_text(row: dict[str, Any]) -> str:
    return str(row.get("claim_text") or row.get("claim") or row.get("value") or "").strip()


def _canonical_claim(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()
