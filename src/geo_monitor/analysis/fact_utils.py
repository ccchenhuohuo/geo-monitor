"""Shared normalization and arithmetic helpers for deterministic fact builders."""

from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import Any

from ..brand_extraction import RECOMMENDATION_STRENGTH


def is_brand_sov_candidate(row: dict[str, Any]) -> bool:
    eligible = coerce_optional_bool(row.get("sov_eligible"))
    if eligible is False:
        return False
    brand_type = str(row.get("brand_type") or "").strip().lower()
    context = str(row.get("mention_context") or "").strip().lower()
    role = str(row.get("role") or "").strip().lower()
    if context == "source" or role == "source":
        return False
    if not brand_type:
        return False
    excluded_types = {
        "媒体",
        "来源",
        "协会",
        "政府",
        "平台",
        "榜单",
        "奖项",
        "其他",
        "source",
        "media",
        "publisher",
        "association",
        "government",
        "platform",
        "ranking",
        "award",
        "other",
    }
    allowed_types = {
        "品牌",
        "公司",
        "企业",
        "厂商",
        "商家",
        "机构",
        "设计机构",
        "装修公司",
        "工作室",
        "brand",
        "company",
        "business",
        "vendor",
        "institution",
        "agency",
        "studio",
    }
    if brand_type in excluded_types:
        return False
    return brand_type in allowed_types


def coerce_optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "是", "推荐"}:
            return True
        if text in {"false", "0", "no", "n", "否", "未推荐"}:
            return False
    return None


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def pct_points(value: float) -> str:
    return f"{max(0.0, value):.1f}pp"


def pct_to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().rstrip("%")
    try:
        return float(text)
    except Exception:
        return 0.0


def rank_sort_value(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 999999.0


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "是", "推荐"}
    return False


def normalized_recommendation_type(row: dict[str, Any]) -> str:
    explicit = str(row.get("recommendation_type") or "").strip().lower()
    if explicit in RECOMMENDATION_STRENGTH:
        return "mentioned_only" if explicit == "not_mentioned" else explicit
    role = str(row.get("role") or "").strip().lower()
    role_mapping = {
        "discouraged": "discouraged",
        "avoid": "discouraged",
        "warning": "warning",
        "conditional": "conditional",
        "strong_alternative": "strong_alternative",
        "alternative": "strong_alternative",
        "budget_pick": "budget_pick",
        "premium_pick": "premium_pick",
        "best_for_use_case": "best_for_use_case",
        "top_pick": "top_pick",
        "recommended": "recommended",
    }
    if role in role_mapping:
        return role_mapping[role]
    if row.get("is_recommended") not in (None, ""):
        return "recommended" if as_bool(row.get("is_recommended")) else "mentioned_only"
    if as_positive_int(row.get("rank_position")) is not None:
        return "recommended"
    return "mentioned_only"


def add_nonempty(target: set[str], value: Any) -> None:
    text = str(value or "").strip()
    if text:
        target.add(text)


def as_positive_int(value: Any) -> int | None:
    if value in {None, ""} or isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        parsed = int(value)
    elif isinstance(value, str) and value.strip().lstrip("+").isdigit():
        parsed = int(value.strip())
    else:
        return None
    return parsed if parsed > 0 else None


def dominant_sentiment(counter: Counter) -> str:
    if not counter:
        return "unknown"
    # Ties must not silently turn mixed evidence into a positive signal.
    priority = {"negative": 0, "neutral": 1, "positive": 2, "unknown": 3}
    return sorted(counter.items(), key=lambda item: (-item[1], priority.get(item[0], 9)))[0][0]


def fmt_float(value: float | None) -> object:
    return round(value, 3) if value is not None else ""


def avg_pairwise_jaccard(sets: list[set], *, treat_empty_as_missing: bool = False) -> float | None:
    if len(sets) < 2:
        return None
    values = []
    for a, b in combinations(sets, 2):
        if treat_empty_as_missing and (not a or not b):
            continue
        values.append(1.0 if not a and not b else len(a & b) / len(a | b))
    return sum(values) / len(values) if values else None


def top_items(items: list[str], limit: int = 8) -> str:
    return " | ".join(f"{item}:{count}" for item, count in Counter(items).most_common(limit))
