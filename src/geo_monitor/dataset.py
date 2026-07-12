from __future__ import annotations

import csv
import json
import random
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from .schemas import QueryRecord


class DatasetError(ValueError):
    pass


MAX_DATASET_BYTES = 50 * 1024 * 1024


def _is_formula_like_csv_text(value: str) -> bool:
    if value.startswith(("\t", "\r", "\n")):
        return True
    return value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@"))


def encode_manifest_csv_cell(value: str) -> str:
    """Encode a manifest cell for spreadsheet-safe, reversible CSV storage.

    A leading apostrophe is doubled so decoding can distinguish literal text
    from the apostrophe added in front of a formula-like value.
    """

    text = str(value)
    if text.startswith("'") or _is_formula_like_csv_text(text):
        return "'" + text
    return text


def decode_manifest_csv_cell(value: object) -> object:
    """Reverse :func:`encode_manifest_csv_cell` for values read from CSV."""

    if not isinstance(value, str) or not value.startswith("'"):
        return value
    if value.startswith("''"):
        return value[1:]
    decoded = value[1:]
    return decoded if _is_formula_like_csv_text(decoded) else value


def _parse_tags(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        if any(not isinstance(item, str) for item in value):
            raise DatasetError("tags 必须是字符串或字符串数组")
        return [item.strip() for item in value if item.strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    raise DatasetError("tags 必须是字符串或字符串数组")


def _record_from_mapping(row: dict) -> QueryRecord:
    known = {"query_id", "query", "locale", "market", "category", "tags"}
    metadata = {k: v for k, v in row.items() if k not in known and v not in (None, "")}
    try:
        return QueryRecord(
            query_id=str(row.get("query_id", "")).strip(),
            query=str(row.get("query", "")).strip(),
            locale=(str(row["locale"]).strip() if row.get("locale") else None),
            market=(str(row["market"]).strip() if row.get("market") else None),
            category=(str(row["category"]).strip() if row.get("category") else None),
            tags=_parse_tags(row.get("tags")),
            metadata=metadata,
        )
    except ValidationError as exc:
        raise DatasetError(str(exc)) from exc


def load_queries(path: str | Path) -> list[QueryRecord]:
    file_path = Path(path)
    if not file_path.exists():
        raise DatasetError(f"输入文件不存在：{file_path}")
    if file_path.is_symlink() or not file_path.is_file():
        raise DatasetError(f"输入数据集必须是普通非 symlink 文件：{file_path}")
    if file_path.stat().st_size > MAX_DATASET_BYTES:
        raise DatasetError(f"输入数据集超过 {MAX_DATASET_BYTES} bytes 上限：{file_path}")

    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        records = _load_csv(file_path)
    elif suffix in {".jsonl", ".ndjson"}:
        records = _load_jsonl(file_path)
    else:
        raise DatasetError("仅支持 .csv、.jsonl、.ndjson 输入文件")

    validate_queries(records)
    return records


def _load_csv(path: Path) -> list[QueryRecord]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise DatasetError("CSV 缺少表头")
        required = {"query_id", "query"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise DatasetError(f"CSV 缺少必填字段：{', '.join(sorted(missing))}")
        return [_record_from_mapping({key: decode_manifest_csv_cell(value) for key, value in row.items()}) for row in reader]


def _load_jsonl(path: Path) -> list[QueryRecord]:
    records: list[QueryRecord] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"JSONL 第 {line_no} 行不是合法 JSON：{exc}") from exc
            if not isinstance(row, dict):
                raise DatasetError(f"JSONL 第 {line_no} 行必须是对象")
            try:
                records.append(_record_from_mapping(row))
            except DatasetError as exc:
                raise DatasetError(f"JSONL 第 {line_no} 行字段错误：{exc}") from exc
    return records


def validate_queries(records: Iterable[QueryRecord]) -> None:
    seen: set[str] = set()
    count = 0
    for record in records:
        count += 1
        if record.query_id in seen:
            raise DatasetError(f"query_id 重复：{record.query_id}")
        seen.add(record.query_id)
    if count == 0:
        raise DatasetError("输入数据集没有有效 query")


def select_queries(
    records: list[QueryRecord],
    *,
    limit: int | None = None,
    sample: int | None = None,
    only_query_ids: list[str] | None = None,
) -> list[QueryRecord]:
    selected = list(records)
    if only_query_ids is not None:
        wanted = set(only_query_ids)
        if not wanted:
            raise DatasetError("only_query_ids 已提供但为空；拒绝回退为全量执行")
        selected = [record for record in selected if record.query_id in wanted]
        missing = wanted - {record.query_id for record in selected}
        if missing:
            raise DatasetError(f"指定 query_id 不存在：{', '.join(sorted(missing))}")
    if sample is not None:
        if sample < 1:
            raise DatasetError("sample 必须大于 0")
        selected = random.sample(selected, min(sample, len(selected)))
    if limit is not None:
        if limit < 1:
            raise DatasetError("limit 必须大于 0")
        selected = selected[:limit]
    validate_queries(selected)
    return selected
