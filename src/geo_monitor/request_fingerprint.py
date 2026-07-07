from __future__ import annotations

import hashlib
import json
from typing import Any


REQUEST_FINGERPRINT_VERSION = "request-fingerprint-v1"


def stable_hash(value: Any, *, length: int = 16) -> str:
    stable = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:length]


def request_fingerprint(basis: dict[str, Any]) -> str:
    return stable_hash({"version": REQUEST_FINGERPRINT_VERSION, "basis": basis})


def legacy_payload_hash(payload: dict[str, Any]) -> str:
    return stable_hash(payload)


def base_url_fingerprint(base_url: str | None) -> str:
    text = str(base_url or "").strip().rstrip("/")
    if not text:
        return ""
    return stable_hash(text, length=12)


def analysis_fingerprint(profile: dict[str, Any]) -> str:
    basis = {key: value for key, value in profile.items() if key != "analysis_fingerprint"}
    return stable_hash({"analysis_profile": basis})

