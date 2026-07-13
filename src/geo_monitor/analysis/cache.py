from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..filesystem import open_private_text
from ..schemas import utc_now_iso

EXTRACTION_PROMPT_VERSION = "brand-extraction-prompt-v2"
CANONICALIZATION_PROMPT_VERSION = "brand-canonicalization-prompt-v1"


class JsonlCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._entries: dict[str, dict[str, Any]] | None = None
        self.load_error_count = 0

    def get(self, key: str) -> dict[str, Any] | None:
        return self._load().get(key)

    def put(self, entry: dict[str, Any]) -> None:
        cache_key = str(entry.get("cache_key") or "")
        if not cache_key:
            return
        record = {"created_at": utc_now_iso(), **entry}
        with open_private_text(self.path, append=True) as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
        self._load()[cache_key] = record

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise ValueError(f"cache 必须是普通非 symlink 文件：{self.path}")
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        self.load_error_count += 1
                        continue
                    if isinstance(row, dict) and row.get("cache_key"):
                        entries[str(row["cache_key"])] = row
                    else:
                        self.load_error_count += 1
        self._entries = entries
        return entries


def extraction_cache_entry(
    *,
    record: dict[str, Any],
    schema_version: str,
    extractor_model: str,
    analysis_fingerprint: str = "",
    rows: list[dict[str, Any]],
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    response_hash = response_text_hash(record)
    return {
        "cache_key": extraction_cache_key(
            response_text_hash_value=response_hash,
            schema_version=schema_version,
            extractor_model=extractor_model,
            analysis_fingerprint=analysis_fingerprint,
        ),
        "cache_type": "brand_extraction",
        "response_text_hash": response_hash,
        "extraction_schema_version": schema_version,
        "extractor_model": extractor_model,
        "analysis_fingerprint": analysis_fingerprint,
        "prompt_version": EXTRACTION_PROMPT_VERSION,
        "query_id": record.get("query_id"),
        "repeat_index": record.get("repeat_index") or 1,
        "rows": rows,
        "error": error,
    }


def extraction_cache_key(
    *,
    response_text_hash_value: str,
    schema_version: str,
    extractor_model: str,
    analysis_fingerprint: str = "",
) -> str:
    return _hash_parts(
        "brand_extraction",
        response_text_hash_value,
        schema_version,
        extractor_model,
        analysis_fingerprint,
        EXTRACTION_PROMPT_VERSION,
    )


def response_text_hash(record: dict[str, Any]) -> str:
    return _sha256(str(record.get("response_text") or ""))


def canonicalization_cache_entry(
    *,
    raw_names: list[str],
    canonicalizer_model: str,
    analysis_fingerprint: str = "",
    canonical_map: dict[str, str],
) -> dict[str, Any]:
    names_hash = raw_names_hash(raw_names)
    return {
        "cache_key": canonicalization_cache_key(
            sorted_raw_names_hash=names_hash,
            canonicalizer_model=canonicalizer_model,
            analysis_fingerprint=analysis_fingerprint,
        ),
        "cache_type": "brand_canonicalization",
        "sorted_raw_names_hash": names_hash,
        "canonicalizer_model": canonicalizer_model,
        "analysis_fingerprint": analysis_fingerprint,
        "prompt_version": CANONICALIZATION_PROMPT_VERSION,
        "canonical_map": canonical_map,
    }


def canonicalization_cache_key(*, sorted_raw_names_hash: str, canonicalizer_model: str, analysis_fingerprint: str = "") -> str:
    return _hash_parts(
        "brand_canonicalization",
        sorted_raw_names_hash,
        canonicalizer_model,
        analysis_fingerprint,
        CANONICALIZATION_PROMPT_VERSION,
    )


def raw_names_hash(raw_names: list[str]) -> str:
    unique_names = sorted({name for name in raw_names if name})
    stable = json.dumps(unique_names, ensure_ascii=False, separators=(",", ":"))
    return _sha256(stable)


def _hash_parts(*parts: str) -> str:
    return _sha256("\n".join(parts))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
