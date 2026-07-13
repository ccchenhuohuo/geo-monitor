"""Maintain the optional cross-run aggregate index and trend tables."""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import Any

from ..exporters import read_jsonl
from ..filesystem import ensure_private_directory
from ..jobs.layout import runs_root_for_bundle
from ..jobs.manifest import load_job_manifest, query_set_hash
from ..schemas import utc_now_iso
from .artifacts import file_sha256, write_csv, write_json_atomic, write_jsonl_atomic
from .contracts import EXTRACTION_SCHEMA_VERSION


def update_cross_job_aggregates(bundle_dir: Path, summary: dict[str, Any]) -> dict[str, Path]:
    paths = cross_job_aggregate_paths(bundle_dir)
    runs_root = paths["runs_index"].parent
    ensure_private_directory(runs_root)
    ensure_private_directory(paths["brand_trends"].parent)
    index_path = paths["runs_index"]
    brand_trends_path = paths["brand_trends"]
    target_trends_path = paths["target_brand_trends"]

    with _file_lock(runs_root / ".aggregate.lock"):
        _upsert_jsonl(index_path, _job_index_row(bundle_dir, summary), key="job_id")
        _upsert_csv_rows(
            brand_trends_path,
            [_brand_trend_row(summary, row) for row in summary.get("brand_summary", [])],
            key_fields=["job_id", "brand_name_canonical"],
            replace_fields={"job_id": _job_id(summary, bundle_dir)},
        )
        target_rows = [
            _brand_trend_row(summary, row, diagnosis=summary.get("target_diagnosis") or {})
            for row in summary.get("brand_summary", [])
            if int(row.get("is_target_brand") or 0)
        ]
        if not target_rows:
            target_rows = [_empty_target_trend_row(summary)]
        _upsert_csv_rows(
            target_trends_path, target_rows, key_fields=["job_id", "brand_name_canonical"], replace_fields={"job_id": _job_id(summary, bundle_dir)}
        )
        aggregate_manifest = {
            "schema_version": "geo-aggregate-v2",
            "updated_at": utc_now_iso(),
            "last_job_id": _job_id(summary, bundle_dir),
            "files": {
                key: {"path": path.name, "sha256": file_sha256(path), "size_bytes": path.stat().st_size}
                for key, path in paths.items()
                if key != "aggregate_manifest"
            },
        }
        write_json_atomic(paths["aggregate_manifest"], aggregate_manifest)

    return paths


def cross_job_aggregate_paths(bundle_dir: Path) -> dict[str, Path]:
    runs_root = runs_root_for_bundle(bundle_dir)
    aggregate_dir = runs_root / "aggregate"
    return {
        "runs_index": runs_root / "index.jsonl",
        "brand_trends": aggregate_dir / "brand_trends.csv",
        "target_brand_trends": aggregate_dir / "target_brand_trends.csv",
        "aggregate_manifest": aggregate_dir / "aggregate_manifest.json",
    }


def _job_index_row(bundle_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    diagnosis = summary.get("target_diagnosis") or {}
    top_brands = " | ".join(row["brand_name_canonical"] for row in summary.get("brand_summary", [])[:5])
    manifest = load_job_manifest(bundle_dir)
    return {
        "job_id": _job_id(summary, bundle_dir),
        "bundle_dir": str(bundle_dir),
        "target_brand": summary.get("target_brand", ""),
        "industry": summary.get("industry", ""),
        "market": summary.get("market", ""),
        "model": manifest.get("model", ""),
        "query_set_hash": query_set_hash(manifest),
        "web_search_limit": manifest.get("web_search_limit", ""),
        "sample_mode": summary.get("sample_mode", "live"),
        "expected_queries": summary.get("expected_queries", 0),
        "expected_repeats": summary.get("expected_repeats", 0),
        "conclusion_strength": (summary.get("data_quality") or {}).get("conclusion_strength", ""),
        "extraction_error_rate": summary.get("extraction_error_rate", "0.0%"),
        "comparability_key": _comparability_key(summary),
        "success_record_count": summary.get("success_record_count", 0),
        "partial_sample": summary.get("partial_sample", False),
        "target_detected": summary.get("target_detected", False),
        "target_sov_response_share": diagnosis.get("target_sov_response_share", "0.0%"),
        "target_rank_by_sov": diagnosis.get("target_rank_by_sov", ""),
        "target_sov_gap_to_leader": diagnosis.get("target_sov_gap_to_leader", ""),
        "top_brands": top_brands,
    }


def _brand_trend_row(summary: dict[str, Any], row: dict[str, Any], *, diagnosis: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = {"queries": [{"query_id": qid, "query": ""} for qid in summary.get("query_ids", [])]}
    diagnosis = diagnosis or {}
    return {
        "job_id": _job_id(summary),
        "target_brand": summary.get("target_brand", ""),
        "industry": summary.get("industry", ""),
        "market": summary.get("market", ""),
        "model": summary.get("model", ""),
        "web_search_limit": summary.get("web_search_limit", ""),
        "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
        "expected_queries": summary.get("expected_queries", 0),
        "expected_repeats": summary.get("expected_repeats", 0),
        "sample_mode": summary.get("sample_mode", "live"),
        "query_set_hash": summary.get("query_set_hash", query_set_hash(manifest)),
        "conclusion_strength": (summary.get("data_quality") or {}).get("conclusion_strength", ""),
        "extraction_error_rate": summary.get("extraction_error_rate", "0.0%"),
        "comparability_key": _comparability_key(summary),
        "partial_sample": summary.get("partial_sample", False),
        "brand_name_canonical": row.get("brand_name_canonical", ""),
        "is_target_brand": row.get("is_target_brand", 0),
        "sov_rank": row.get("sov_rank", ""),
        "sov_response_share": row.get("sov_response_share", "0.0%"),
        "response_mention_rate": row.get("response_mention_rate", "0.0%"),
        "query_coverage_rate": row.get("query_coverage_rate", "0.0%"),
        "recommended_rate_when_mentioned": row.get("recommended_rate_when_mentioned", "0.0%"),
        "recommended_rate_over_success": row.get("recommended_rate_over_success", "0.0%"),
        "rank_observed_rate": row.get("rank_observed_rate", "0.0%"),
        "sentiment_unknown_rate": row.get("sentiment_unknown_rate", "0.0%"),
        "avg_rank_position": row.get("avg_rank_position", ""),
        "positive_rate": row.get("positive_rate", "0.0%"),
        "neutral_rate": row.get("neutral_rate", "0.0%"),
        "negative_rate": row.get("negative_rate", "0.0%"),
        "target_sov_gap_to_leader": diagnosis.get("target_sov_gap_to_leader", "") if int(row.get("is_target_brand") or 0) else "",
        "target_sov_gap_to_top3_avg": diagnosis.get("target_sov_gap_to_top3_avg", "") if int(row.get("is_target_brand") or 0) else "",
        "success_record_count": summary.get("success_record_count", 0),
    }


def _empty_target_trend_row(summary: dict[str, Any]) -> dict[str, Any]:
    manifest = {"queries": [{"query_id": qid, "query": ""} for qid in summary.get("query_ids", [])]}
    return {
        "job_id": _job_id(summary),
        "target_brand": summary.get("target_brand", ""),
        "industry": summary.get("industry", ""),
        "market": summary.get("market", ""),
        "model": summary.get("model", ""),
        "web_search_limit": summary.get("web_search_limit", ""),
        "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
        "expected_queries": summary.get("expected_queries", 0),
        "expected_repeats": summary.get("expected_repeats", 0),
        "sample_mode": summary.get("sample_mode", "live"),
        "query_set_hash": summary.get("query_set_hash", query_set_hash(manifest)),
        "conclusion_strength": (summary.get("data_quality") or {}).get("conclusion_strength", ""),
        "extraction_error_rate": summary.get("extraction_error_rate", "0.0%"),
        "comparability_key": _comparability_key(summary),
        "partial_sample": summary.get("partial_sample", False),
        "brand_name_canonical": summary.get("target_brand", ""),
        "is_target_brand": 1,
        "sov_rank": "",
        "sov_response_share": "0.0%",
        "response_mention_rate": "0.0%",
        "query_coverage_rate": "0.0%",
        "recommended_rate_when_mentioned": "0.0%",
        "recommended_rate_over_success": "0.0%",
        "rank_observed_rate": "0.0%",
        "sentiment_unknown_rate": "0.0%",
        "avg_rank_position": "",
        "positive_rate": "0.0%",
        "neutral_rate": "0.0%",
        "negative_rate": "0.0%",
        "target_sov_gap_to_leader": "",
        "target_sov_gap_to_top3_avg": "",
        "success_record_count": summary.get("success_record_count", 0),
    }


def _job_id(summary: dict[str, Any], bundle_dir: Path | None = None) -> str:
    if summary.get("job_id"):
        return str(summary["job_id"])
    if bundle_dir is not None:
        try:
            return str(load_job_manifest(bundle_dir).get("job_id") or bundle_dir.name)
        except Exception:
            return bundle_dir.name
    return ""


def _comparability_key(summary: dict[str, Any]) -> str:
    parts = [
        str(summary.get("model", "")),
        str(summary.get("target_brand", "")),
        ",".join(sorted(str(alias) for alias in (summary.get("target_aliases") or []))),
        str(summary.get("query_set_hash", "")),
        str(summary.get("web_search_limit", "")),
        str(summary.get("expected_queries", "")),
        str(summary.get("expected_repeats", "")),
        str(summary.get("sample_mode", "")),
        EXTRACTION_SCHEMA_VERSION,
    ]
    return "|".join(parts)


def _upsert_jsonl(path: Path, row: dict[str, Any], *, key: str) -> None:
    rows = read_jsonl(path) if path.exists() else []
    keyed = {str(item.get(key)): item for item in rows if item.get(key) is not None}
    keyed[str(row[key])] = row
    write_jsonl_atomic(path, sorted(keyed.values(), key=lambda item: str(item.get(key))))


def _upsert_csv_rows(path: Path, rows: list[dict[str, Any]], *, key_fields: list[str], replace_fields: dict[str, Any] | None = None) -> None:
    existing = _read_csv_rows(path) if path.exists() else []
    if replace_fields:
        existing = [row for row in existing if not all(str(row.get(field, "")) == str(value) for field, value in replace_fields.items())]
    merged = {tuple(str(row.get(field, "")) for field in key_fields): row for row in existing}
    for row in rows:
        merged[tuple(str(row.get(field, "")) for field in key_fields)] = row
    output_rows = sorted(merged.values(), key=lambda row: tuple(str(row.get(field, "")) for field in key_fields))
    schema = "brand_trends" if path.stem in {"brand_trends", "target_brand_trends"} else None
    write_csv(path, output_rows, schema=schema)


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class _file_lock:
    def __init__(self, path: Path, *, timeout_seconds: float = 10.0, stale_seconds: float = 12 * 60 * 60):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self.fd: int | None = None

    def __enter__(self) -> "_file_lock":
        ensure_private_directory(self.path.parent)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                os.write(self.fd, str(os.getpid()).encode("utf-8"))
                return self
            except FileExistsError:
                if self._is_stale():
                    try:
                        self.path.unlink()
                        continue
                    except FileNotFoundError:
                        continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待聚合文件锁超时：{self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _is_stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > self.stale_seconds
        except FileNotFoundError:
            return False
