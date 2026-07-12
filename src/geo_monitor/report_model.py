"""Versioned, renderer-neutral report document model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

REPORT_MODEL_SCHEMA_VERSION = "geo-report-v1"


@dataclass(frozen=True)
class ReportTable:
    key: str
    headers: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    note: str = ""


@dataclass(frozen=True)
class ReportBlock:
    kind: Literal["paragraph", "bullets", "table"]
    text: str = ""
    items: tuple[str, ...] = ()
    table: ReportTable | None = None


@dataclass(frozen=True)
class ReportSection:
    key: str
    title: str
    blocks: tuple[ReportBlock, ...] = ()


@dataclass(frozen=True)
class ReportModel:
    title: str
    job_id: str
    generated_at: str
    sample_mode: str
    conclusion_strength: str
    sections: tuple[ReportSection, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = REPORT_MODEL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def paragraph(text: object) -> ReportBlock:
    return ReportBlock(kind="paragraph", text=str(text or ""))


def bullets(*items: object) -> ReportBlock:
    return ReportBlock(kind="bullets", items=tuple(str(item) for item in items if str(item).strip()))


def table(
    key: str,
    headers: list[str] | tuple[str, ...],
    rows: list[list[Any] | tuple[Any, ...]],
    *,
    note: str = "",
) -> ReportBlock:
    width = len(headers)
    normalized_rows = tuple(tuple(row[:width]) + ("",) * max(0, width - len(row)) for row in rows)
    return ReportBlock(
        kind="table",
        table=ReportTable(key=key, headers=tuple(headers), rows=normalized_rows, note=note),
    )
