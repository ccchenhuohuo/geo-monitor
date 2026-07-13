from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .schemas import SourceRecord

URL_KEYS = ("url", "uri", "link")
TITLE_KEYS = ("title", "site_name", "name")
SNIPPET_KEYS = ("snippet", "summary", "text", "content")
SOURCE_HINT_KEYS = {"citation", "annotations", "source", "sources", "search_info", "search_results", "web_search", "url_citation"}
TRACKING_QUERY_KEYS = {
    "_ga",
    "_gl",
    "dclid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "yclid",
}


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
    choices = raw.get("choices")
    output_mapping = raw.get("output") if isinstance(raw.get("output"), Mapping) else {}
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
        choices = output_mapping.get("choices")
    if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes, bytearray)):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message")
            if isinstance(message, Mapping):
                text = _message_content_text(message.get("content"))
                if text:
                    texts.append(text)
        if texts:
            return "\n".join(t.strip() for t in texts if t.strip())

    output = raw.get("output")
    if isinstance(output, Sequence) and not isinstance(output, (str, bytes, bytearray)):
        for item in output:
            if not isinstance(item, Mapping):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "output_text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
            if item_type != "message":
                continue
            content = item.get("content")
            if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
                continue
            for part in content:
                if isinstance(part, Mapping) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    if texts:
        return "\n".join(t.strip() for t in texts if t.strip())
    return None


def _message_content_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts = []
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") in {"text", "output_text"} and isinstance(part.get("text"), str):
                parts.append(part["text"])
        if parts:
            return "\n".join(text.strip() for text in parts if text.strip()) or None
    return None


def extract_usage(raw: dict[str, Any]) -> dict[str, Any] | None:
    usage = raw.get("usage")
    if isinstance(usage, Mapping):
        return dict(usage)
    return None


def extract_sources(raw: dict[str, Any]) -> list[SourceRecord]:
    candidates: list[dict[str, Any]] = []

    def visit(node: Any, *, hinted: bool = False) -> None:
        if isinstance(node, Mapping):
            item_dict = dict(node)
            source_type = str(item_dict.get("type", "")).lower()
            current_hinted = hinted or "citation" in source_type or "search" in source_type
            is_source = _looks_like_source(item_dict, hinted=current_hinted)
            if is_source:
                candidates.append(item_dict)
            for key, child in node.items():
                # Nested media or logo URLs describe an accepted citation; they
                # are not additional source records.
                visit(child, hinted=(current_hinted and not is_source) or key in SOURCE_HINT_KEYS)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            for child in node:
                visit(child, hinted=hinted)

    visit(raw)

    seen: set[tuple[str, str]] = set()
    sources: list[SourceRecord] = []
    for idx, candidate in enumerate(candidates, start=1):
        source = _candidate_to_source(candidate, idx)
        key = ("url", source.url) if source.url else ("title", source.title or "")
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


def _looks_like_source(item: dict[str, Any], *, hinted: bool = False) -> bool:
    keys = set(item.keys())
    has_url = any(key in keys and isinstance(item.get(key), str) and canonicalize_source_url(str(item.get(key))) is not None for key in URL_KEYS)
    source_type = str(item.get("type", "")).lower()
    has_source_hint = bool(keys & SOURCE_HINT_KEYS) or "citation" in source_type or "search" in source_type
    return has_url and (hinted or has_source_hint)


def _first_str(item: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _candidate_to_source(candidate: dict[str, Any], rank: int) -> SourceRecord:
    url = next(
        (canonical for key in URL_KEYS if isinstance(candidate.get(key), str) and (canonical := canonicalize_source_url(str(candidate.get(key)))) is not None),
        None,
    )
    title = _first_str(candidate, TITLE_KEYS)
    snippet = _first_str(candidate, SNIPPET_KEYS)
    source_type = str(candidate.get("type") or candidate.get("source_type") or "unknown")
    domain = _normalize_domain(urlsplit(url).hostname or "") if url else None
    return SourceRecord(
        title=title,
        url=url,
        domain=domain,
        snippet=snippet,
        source_type=source_type,
        rank=rank,
        raw=candidate,
    )


def canonicalize_source_url(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not hostname:
        return None
    try:
        host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return None
    if not host:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    query_pairs = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if not _is_tracking_query_key(key)]
    query_pairs.sort()
    return urlunsplit((scheme, netloc, parsed.path or "/", urlencode(query_pairs, doseq=True), ""))


def _is_tracking_query_key(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    return normalized.startswith("utm_") or normalized in TRACKING_QUERY_KEYS


def _normalize_domain(domain: str) -> str | None:
    text = (domain or "").strip().lower().rstrip(".")
    if not text:
        return None
    if text.startswith("www."):
        text = text[4:]
    return text
