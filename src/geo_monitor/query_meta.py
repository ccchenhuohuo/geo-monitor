from __future__ import annotations

import json
from typing import Any


QUERY_META_SCHEMA_VERSION = "query-meta-v1"

QUERY_META_FIRST_CLASS_FIELDS = {
    "schema_version",
    "variant_id",
    "seed_id",
    "seed_query",
    "category",
    "intent",
    "persona",
    "template_id",
    "locale",
    "market",
    "tags",
    "language",
    "generation_method",
    "fanout_version",
    "manifest_version",
    "locked_at",
}

QUERY_META_NON_CUSTOM_FIELDS = QUERY_META_FIRST_CLASS_FIELDS | {"query_id", "query", "query_metadata_json"}

QUERY_META_DEFAULTS = {
    "schema_version": QUERY_META_SCHEMA_VERSION,
    "variant_id": "",
    "seed_id": "",
    "seed_query": "",
    "category": "",
    "intent": "",
    "persona": "",
    "template_id": "",
    "locale": "",
    "market": "",
    "tags": "",
    "language": "",
    "generation_method": "config",
    "fanout_version": "",
    "manifest_version": "",
    "locked_at": "",
    "query_metadata_json": "{}",
}


def compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def tags_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def query_metadata_json(meta: dict[str, Any], extra_metadata: dict[str, Any] | None = None) -> str:
    existing = meta.get("query_metadata_json")
    if isinstance(existing, str) and existing.strip() and existing.strip() != "{}":
        return existing
    source = dict(extra_metadata or {})
    source.update({key: value for key, value in meta.items() if value not in (None, "")})
    custom = {key: value for key, value in source.items() if key not in QUERY_META_NON_CUSTOM_FIELDS and value not in (None, "")}
    return compact_json(custom)


def ensure_query_meta_defaults(value: dict[str, Any] | None) -> dict[str, str]:
    meta = {key: "" if item is None else str(item) for key, item in dict(value or {}).items()}
    for key, default in QUERY_META_DEFAULTS.items():
        meta.setdefault(key, default)
    if not meta.get("schema_version"):
        meta["schema_version"] = QUERY_META_SCHEMA_VERSION
    if not meta.get("generation_method"):
        meta["generation_method"] = "config"
    if not meta.get("query_metadata_json"):
        meta["query_metadata_json"] = "{}"
    return meta


def query_record_meta(query: Any) -> dict[str, str]:
    metadata = query.metadata_with_tags()

    def text(key: str, default: str = "") -> str:
        item = metadata.get(key, default)
        if item in (None, ""):
            return default
        return str(item)

    custom_metadata = {
        key: value
        for key, value in metadata.items()
        if key not in QUERY_META_FIRST_CLASS_FIELDS and value not in (None, "")
    }
    return {
        "schema_version": QUERY_META_SCHEMA_VERSION,
        "variant_id": text("variant_id"),
        "seed_id": text("seed_id"),
        "seed_query": text("seed_query"),
        "category": str(query.category or metadata.get("category") or ""),
        "intent": text("intent"),
        "persona": text("persona"),
        "template_id": text("template_id"),
        "locale": text("locale", str(query.locale or "")),
        "market": text("market", str(query.market or "")),
        "tags": tags_text(metadata.get("tags", query.tags)),
        "language": text("language", str(query.locale or "")),
        "generation_method": text("generation_method", "config"),
        "fanout_version": text("fanout_version"),
        "manifest_version": text("manifest_version"),
        "locked_at": text("locked_at"),
        "query_metadata_json": compact_json(custom_metadata),
    }
