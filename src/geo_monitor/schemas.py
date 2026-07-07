from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .query_meta import ensure_query_meta_defaults


Status = Literal["success", "error", "dry_run", "mock"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class QueryRecord(BaseModel):
    query_id: str
    query: str
    locale: str | None = None
    market: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("query_id", "query")
    @classmethod
    def not_empty(cls, value: str) -> str:  # noqa: N805
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value

    def metadata_with_tags(self) -> dict[str, Any]:
        metadata = dict(self.metadata)
        if self.locale:
            metadata["locale"] = self.locale
        if self.market:
            metadata["market"] = self.market
        if self.category:
            metadata["category"] = self.category
        if self.tags:
            metadata["tags"] = self.tags
        return metadata


class SourceRecord(BaseModel):
    title: str | None = None
    url: str | None = None
    domain: str | None = None
    snippet: str | None = None
    source_type: str = "unknown"
    rank: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ErrorRecord(BaseModel):
    type: str
    message: str
    raw: dict[str, Any] | None = None


class MonitorResult(BaseModel):
    schema_version: str = "attempts-v2"
    job_id: str | None = None
    attempt_id: str | None = None
    run_id: str
    query_id: str
    repeat_index: int = 1
    repeat_total: int = 1
    request_hash: str | None = None
    request_fingerprint_version: str | None = None
    request_fingerprint_basis: dict[str, Any] = Field(default_factory=dict)
    model: str
    query: str | None = None
    input_query: str
    status: Status
    response_text: str | None = None
    sources: list[SourceRecord] = Field(default_factory=list)
    usage: dict[str, Any] | None = None
    latency_ms: int | None = None
    error: ErrorRecord | None = None
    raw_request: dict[str, Any] = Field(default_factory=dict)
    raw_response: dict[str, Any] | None = None
    sampling_profile: dict[str, Any] = Field(default_factory=dict)
    provider_meta: dict[str, Any] = Field(default_factory=dict)
    web_search_performed: bool | None = None
    web_search_evidence: str | None = None
    web_search_requirement_status: str | None = None
    source_parse_status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    query_meta: dict[str, Any] = Field(default_factory=dict)
    started_at: str
    completed_at: str

    @model_validator(mode="after")
    def ensure_attempts_v2_contract(self) -> "MonitorResult":
        if self.schema_version != "attempts-v2":
            return self
        if not (self.query and self.query.strip()):
            self.query = self.input_query.strip()
        if not self.query:
            raise ValueError("attempts-v2 requires top-level query")
        self.query_meta = ensure_query_meta_defaults(self.query_meta)
        return self

    @property
    def source_count(self) -> int:
        return len(self.sources)
