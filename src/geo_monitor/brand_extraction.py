from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Callable

from .llm_client import LLMResponsesClient
from .config import Settings
from .response_parser import parse_response


BrandMentionExtractor = Callable[[dict], tuple[list[dict[str, Any]], dict[str, Any] | None]]
BrandCanonicalizer = Callable[[list[str]], tuple[dict[str, str], dict[str, Any] | None]]
ALLOWED_BRAND_TYPES = {
    "品牌",
    "公司",
    "企业",
    "厂商",
    "商家",
    "机构",
    "设计机构",
    "装修公司",
    "工作室",
    "媒体",
    "来源",
    "协会",
    "政府",
    "平台",
    "榜单",
    "奖项",
    "其他",
    "brand",
    "company",
    "business",
    "vendor",
    "institution",
    "agency",
    "studio",
    "media",
    "publisher",
    "association",
    "government",
    "platform",
    "ranking",
    "award",
    "source",
    "other",
}


def normalize_brand_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = text.strip("（）()[]【】")
    return text


class LLMBrandExtractor:
    def __init__(self, settings: Settings, *, model: str | None = None):
        self.settings = settings
        self.model = model or settings.llm_model
        self.client = LLMResponsesClient(settings)

    def extract_record(self, record: dict) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        text = str(record.get("response_text") or "").strip()
        if not text:
            return [], {"type": "EmptyResponseText", "message": "response_text 为空"}
        payload = {
            "model": self.model,
            "input": _brand_extraction_prompt(text),
        }
        try:
            response = self.client.create_response(payload)
            output_text, _, _, raw = parse_response(response)
            data = parse_json_payload(output_text or "")
            mentions, quarantined = normalize_extraction_items_with_quarantine(data.get("brands", []), record)
            if quarantined:
                return mentions, {
                    "type": "TraceabilityQuarantine",
                    "message": f"{len(quarantined)} 个抽取项无法在 response_text 中追溯，已按行隔离",
                    "query_id": record.get("query_id"),
                    "repeat_index": record.get("repeat_index") or 1,
                    "quarantined_rows": quarantined,
                }
            return mentions, None
        except Exception as exc:  # noqa: BLE001
            return [], {
                "type": exc.__class__.__name__,
                "message": str(exc),
                "query_id": record.get("query_id"),
                "repeat_index": record.get("repeat_index") or 1,
            }

    def canonicalize(self, names: list[str]) -> tuple[dict[str, str], dict[str, Any] | None]:
        unique_names = sorted({name for name in names if name})
        fallback = {name: name for name in unique_names}
        if not unique_names:
            return {}, None
        payload = {
            "model": self.model,
            "input": _canonicalization_prompt(unique_names),
        }
        try:
            response = self.client.create_response(payload)
            output_text, _, _, raw = parse_response(response)
            data = parse_json_payload(output_text or "")
            return parse_canonical_map(data, unique_names), None
        except Exception as exc:  # noqa: BLE001
            return fallback, {"type": exc.__class__.__name__, "message": str(exc), "stage": "canonicalization"}


def parse_json_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise
        data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("LLM 输出必须是 JSON 对象")
    return data


def normalize_extraction_items(items: Any, record: dict) -> list[dict[str, Any]]:
    rows, _ = normalize_extraction_items_with_quarantine(items, record)
    return rows


def normalize_extraction_items_with_quarantine(items: Any, record: dict) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(items, list):
        raise ValueError("brands 必须是数组")
    rows: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    seen: set[str] = set()
    response_text = str(record.get("response_text") or "")
    query_id = str(record.get("query_id"))
    repeat_index = int(record.get("repeat_index") or 1)
    input_query = record.get("input_query", "")
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("brand_name_raw") or item.get("name") or "").strip()
        if not raw_name:
            continue
        evidence = str(item.get("evidence") or "")[:500]
        if response_text and not _traceable(response_text, raw_name):
            quarantined.append(
                {
                    "query_id": query_id,
                    "repeat_index": repeat_index,
                    "input_query": input_query,
                    "brand_name_raw": raw_name,
                    "evidence": evidence,
                    "reason": "untraceable_extraction_item",
                }
            )
            continue
        dedupe_key = normalize_brand_name(raw_name)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        brand_type = _brand_type_value(item.get("brand_type") or item.get("type"))
        confidence = item.get("confidence", "")
        try:
            confidence = float(confidence)
        except Exception:
            confidence = ""
        role = str(item.get("role") or "").strip().lower()
        rows.append({
            "query_id": query_id,
            "repeat_index": repeat_index,
            "input_query": input_query,
            "brand_name_raw": raw_name,
            "brand_type": brand_type,
            "evidence": evidence,
            "role": role,
            "confidence": confidence,
            "is_recommended": _bool_value(item.get("is_recommended"), default=role == "recommended"),
            "rank_position": _rank_value(item.get("rank_position") or item.get("rank")),
            "sentiment": _sentiment_value(item.get("sentiment")),
            "mention_context": _mention_context_value(item.get("mention_context") or item.get("context")),
            "sov_eligible": _bool_value(item.get("sov_eligible"), default=False) if brand_type else False,
            "canonical_hint": str(item.get("canonical_hint") or item.get("canonical_name") or "")[:200],
        })
    return rows, quarantined


def parse_canonical_map(data: dict[str, Any], raw_names: list[str]) -> dict[str, str]:
    mapping = {name: name for name in raw_names}
    groups = data.get("canonical_brands") or data.get("brands") or []
    if not isinstance(groups, list):
        return mapping
    raw_name_set = set(raw_names)
    for group in groups:
        if not isinstance(group, dict):
            continue
        canonical = str(group.get("canonical_name") or group.get("name") or "").strip()
        names = group.get("raw_names") or group.get("aliases") or []
        if not canonical or not isinstance(names, list):
            continue
        for name in names:
            raw = str(name).strip()
            if raw in raw_name_set:
                mapping[raw] = canonical
    return mapping


def fallback_canonicalize(names: list[str]) -> tuple[dict[str, str], dict[str, Any] | None]:
    groups: dict[str, str] = {}
    for name in sorted({name for name in names if name}):
        key = normalize_brand_name(name)
        groups.setdefault(key, name)
    return {name: groups[normalize_brand_name(name)] for name in names if name}, None


def _brand_extraction_prompt(response_text: str) -> str:
    return (
        "你是一个严格的信息抽取器。请只从下面这段回答文本中抽取明确出现的品牌、公司或机构名称。"
        "不要补充文本中没有出现的品牌。同一回答内同一品牌只保留一条。只输出 JSON，不要输出解释。\n\n"
        "字段要求：is_recommended 表示是否被明确推荐或列为候选；rank_position 只在原文明确编号、排名或推荐顺序时填写整数，"
        "普通并列举例或不分先后的列表不要填写；sentiment 只能是 positive、neutral、negative、unknown；"
        "mention_context 只能是 answer、source、comparison、other；sov_eligible 表示该实体是否应进入品牌 SOV，"
        "媒体、协会、政府、榜单、奖项、信息来源、平台类泛主体应为 false。"
        "所有判断都必须能从 evidence 中追溯，无法判断时使用 false、null 或 unknown。\n\n"
        "输出格式：\n"
        "{\"brands\":[{\"brand_name_raw\":\"原文品牌名\",\"brand_type\":\"品牌/公司/机构/媒体/其他\","
        "\"evidence\":\"原文证据片段\",\"role\":\"recommended/mentioned/source/other\",\"confidence\":0.0,"
        "\"is_recommended\":false,\"rank_position\":null,\"sentiment\":\"unknown\","
        "\"mention_context\":\"answer\",\"sov_eligible\":true,\"canonical_hint\":\"统一名建议\"}]}\n\n"
        f"回答文本：\n{response_text}"
    )


def _canonicalization_prompt(names: list[str]) -> str:
    return (
        "你是品牌名称归一化助手。请把同一个品牌、公司或机构的不同写法合并，"
        "不要把不同主体错误合并。只输出 JSON，不要解释。\n\n"
        "输出格式：\n"
        "{\"canonical_brands\":[{\"canonical_name\":\"统一名称\",\"raw_names\":[\"原始名称1\"]}]}\n\n"
        "待归一化名称：\n"
        + json.dumps(names, ensure_ascii=False)
    )


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "是", "推荐"}:
            return True
        if text in {"false", "0", "no", "n", "否", "未推荐"}:
            return False
    return default


def _rank_value(value: Any) -> int | str:
    if value in {None, ""}:
        return ""
    try:
        rank = int(value)
    except Exception:
        return ""
    return rank if rank > 0 else ""


def _brand_type_value(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.lower() in ALLOWED_BRAND_TYPES or text in ALLOWED_BRAND_TYPES else ""


def _sentiment_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"positive", "neutral", "negative", "unknown"} else "unknown"


def _mention_context_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"answer", "source", "comparison", "other"} else "answer"


def _traceable(response_text: str, value: str) -> bool:
    if not value:
        return False
    if value in response_text:
        return True
    return normalize_brand_name(value) in normalize_brand_name(response_text)
