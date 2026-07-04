from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from .schemas import SourceRecord


URL_KEYS = {"url", "uri", "link"}
TITLE_KEYS = {"title", "site_name", "name"}
SNIPPET_KEYS = {"snippet", "summary", "text", "content"}
SOURCE_HINT_KEYS = {"citation", "annotations", "source", "sources", "web_search", "url_citation"}


def response_to_dict(response: Any) -> dict[str, Any]:
    if response is None:
        return {}
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    if hasattr(response, "dict"):
        return response.dict()
    try:
        return json.loads(response.model_dump_json())
    except Exception:
        return {"repr": repr(response)}


def extract_output_text(raw: dict[str, Any]) -> str | None:
    output_text = raw.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    texts: list[str] = []
    for item in _walk(raw):
        if not isinstance(item, Mapping):
            continue
        if item.get("type") in {"output_text", "text"} and isinstance(item.get("text"), str):
            texts.append(item["text"])
        elif isinstance(item.get("content"), str):
            texts.append(item["content"])
    if texts:
        return "\n".join(t.strip() for t in texts if t.strip())
    return None


def extract_usage(raw: dict[str, Any]) -> dict[str, Any] | None:
    usage = raw.get("usage")
    if isinstance(usage, Mapping):
        return dict(usage)
    return None


def extract_sources(raw: dict[str, Any]) -> list[SourceRecord]:
    candidates: list[dict[str, Any]] = []
    for item in _walk(raw):
        if not isinstance(item, Mapping):
            continue
        item_dict = dict(item)
        if _looks_like_source(item_dict):
            candidates.append(item_dict)

    seen: set[tuple[str | None, str | None]] = set()
    sources: list[SourceRecord] = []
    for idx, candidate in enumerate(candidates, start=1):
        source = _candidate_to_source(candidate, idx)
        key = (source.url, source.title)
        if key in seen:
            continue
        seen.add(key)
        sources.append(source)
    return sources


def parse_response(response: Any) -> tuple[str | None, list[SourceRecord], dict[str, Any] | None, dict[str, Any]]:
    raw = response_to_dict(response)
    return extract_output_text(raw), extract_sources(raw), extract_usage(raw), raw


def _walk(value: Any) -> list[Any]:
    out: list[Any] = []

    def visit(node: Any) -> None:
        out.append(node)
        if isinstance(node, Mapping):
            for child in node.values():
                visit(child)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            for child in node:
                visit(child)

    visit(value)
    return out


def _looks_like_source(item: dict[str, Any]) -> bool:
    keys = set(item.keys())
    has_url = any(k in keys and isinstance(item.get(k), str) and str(item.get(k)).startswith(("http://", "https://")) for k in URL_KEYS)
    source_type = str(item.get("type", "")).lower()
    has_source_hint = bool(keys & SOURCE_HINT_KEYS) or "citation" in source_type or "search" in source_type
    return has_url and (has_source_hint or bool(keys & TITLE_KEYS))


def _first_str(item: Mapping[str, Any], keys: set[str]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _candidate_to_source(candidate: dict[str, Any], rank: int) -> SourceRecord:
    url = _first_str(candidate, URL_KEYS)
    title = _first_str(candidate, TITLE_KEYS)
    snippet = _first_str(candidate, SNIPPET_KEYS)
    source_type = str(candidate.get("type") or candidate.get("source_type") or "unknown")
    domain = _normalize_domain(urlparse(url).netloc) if url else None
    return SourceRecord(
        title=title,
        url=url,
        domain=domain,
        snippet=snippet,
        source_type=source_type,
        rank=rank,
        raw=candidate,
    )


def _normalize_domain(domain: str) -> str | None:
    text = (domain or "").strip().lower()
    if not text:
        return None
    if ":" in text:
        host, _, port = text.partition(":")
        if port in {"80", "443"}:
            text = host
    if text.startswith("www."):
        text = text[4:]
    return text
