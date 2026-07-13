"""Write one run's analysis artifacts and immutable commit metadata."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from ..exporters import sanitize_csv_row, write_jsonl
from ..filesystem import ensure_private_directory, open_private_text, secure_private_file
from ..jobs.layout import raw_attempts_path
from ..schemas import utc_now_iso
from .contracts import CSV_FIELD_SCHEMAS, EXTRACTION_SCHEMA_VERSION
from .intelligence import INTELLIGENCE_SCHEMA_VERSION, INTELLIGENCE_TABLE_NAMES


def write_job_analysis_files(
    *,
    root: Path,
    work: Path,
    logs: Path,
    result: Path,
    mentions: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    canonical_map: dict[str, str],
    stats: dict[str, Any],
    source_stats: dict[str, Any],
    facts: dict[str, list[dict[str, Any]]],
    intelligence: dict[str, list[dict[str, Any]]],
    data_quality: dict[str, Any],
) -> dict[str, Path]:
    ensure_private_directory(work)
    ensure_private_directory(logs)
    ensure_private_directory(result)
    write_jsonl(work / "brand_mentions_raw.jsonl", mentions)
    write_text_atomic(work / "brand_canonical_map_work.json", json.dumps(canonical_map, ensure_ascii=False, indent=2))
    files = {
        "extraction_errors_jsonl": logs / "extraction_errors.jsonl",
        "raw_read_errors_jsonl": logs / "raw_read_errors.jsonl",
        "data_quality": logs / "data_quality.json",
        "brand_mentions_extracted": result / "brand_mentions_extracted.csv",
        "brand_canonical_map": result / "brand_canonical_map.csv",
        "brand_summary": result / "brand_summary.csv",
        "brand_by_query": result / "brand_by_query.csv",
        "query_stability": result / "query_stability.csv",
        "source_entity_mentions": result / "source_entity_mentions.csv",
        "source_domains": result / "source_domains.csv",
        "source_urls": result / "source_urls.csv",
        "source_by_query": result / "source_by_query.csv",
        "quality_summary": result / "quality_summary.csv",
        "attempt_facts": result / "attempt_facts.csv",
        "query_facts": result / "query_facts.csv",
        "brand_attempt_facts": result / "brand_attempt_facts.csv",
    }
    files.update({name: result / f"{name}.csv" for name in INTELLIGENCE_TABLE_NAMES})
    write_jsonl(files["extraction_errors_jsonl"], errors)
    write_jsonl(files["raw_read_errors_jsonl"], data_quality.get("raw_read_errors", []))
    write_text_atomic(files["data_quality"], json.dumps(data_quality, ensure_ascii=False, indent=2))
    write_csv(files["brand_mentions_extracted"], mentions, schema="brand_mentions_extracted")
    write_csv(
        files["brand_canonical_map"],
        [{"brand_name_raw": raw, "brand_name_canonical": canonical} for raw, canonical in sorted(canonical_map.items())],
        schema="brand_canonical_map",
    )
    write_csv(files["brand_summary"], stats["brand_summary"], schema="brand_summary")
    write_csv(files["brand_by_query"], stats["brand_by_query"], schema="brand_by_query")
    write_csv(files["query_stability"], stats["query_stability"], schema="query_stability")
    write_csv(files["source_entity_mentions"], stats["source_entity_mentions"], schema="source_entity_mentions")
    write_csv(files["source_domains"], source_stats["source_domains"], schema="source_domains")
    write_csv(files["source_urls"], source_stats["source_urls"], schema="source_urls")
    write_csv(files["source_by_query"], source_stats["source_by_query"], schema="source_by_query")
    write_csv(files["quality_summary"], facts["quality_summary"], schema="quality_summary")
    write_csv(files["attempt_facts"], facts["attempt_facts"], schema="attempt_facts")
    write_csv(files["query_facts"], facts["query_facts"], schema="query_facts")
    write_csv(files["brand_attempt_facts"], facts["brand_attempt_facts"], schema="brand_attempt_facts")
    for name in INTELLIGENCE_TABLE_NAMES:
        write_csv(files[name], intelligence.get(name, []), schema=name)
    return files


def write_csv(path: Path, rows: list[dict[str, Any]], *, schema: str | None = None) -> None:
    ensure_private_directory(path.parent)
    base_fields = CSV_FIELD_SCHEMAS.get(schema or "", [])
    row_fields = {key for row in rows for key in row.keys()}
    fieldnames = [*base_fields, *sorted(row_fields - set(base_fields))]
    if not fieldnames:
        fieldnames = ["empty"]
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open_private_text(tmp_path, encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(sanitize_csv_row(row))
        os.replace(tmp_path, path)
        secure_private_file(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_private_directory(path.parent)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        write_jsonl(tmp_path, rows)
        os.replace(tmp_path, path)
        secure_private_file(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2))


def write_text_atomic(path: Path, value: str) -> None:
    ensure_private_directory(path.parent)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open_private_text(tmp_path) as handle:
            handle.write(value)
        os.replace(tmp_path, path)
        secure_private_file(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def build_analysis_artifact_manifest(
    *,
    root: Path,
    manifest: dict[str, Any],
    analysis_summary: Path,
    analysis_files: dict[str, Path],
    report_files: dict[str, Path],
) -> dict[str, Any]:
    artifact_paths = {**analysis_files, **{f"report_{key}": value for key, value in report_files.items()}}
    artifact_paths["analysis_summary"] = analysis_summary
    inputs = {
        "raw_attempts": raw_attempts_path(root),
        "query_manifest": root / "work" / "query_manifest.csv",
    }
    return {
        "schema_version": "analysis-artifacts-v1",
        "job_id": manifest.get("job_id"),
        "run_generation": int(manifest.get("run_generation") or 0),
        "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
        "intelligence_schema_version": INTELLIGENCE_SCHEMA_VERSION,
        "created_at": utc_now_iso(),
        "inputs": {
            key: _artifact_input_record(root, path, append_only=(key == "raw_attempts"), required=(key != "query_manifest"))
            for key, path in inputs.items()
            if path.is_file() and not path.is_symlink()
        },
        "artifacts": {
            key: {"path": relative_path(root, path), "sha256": file_sha256(path), "size_bytes": path.stat().st_size}
            for key, path in sorted(artifact_paths.items())
            if path.is_file() and not path.is_symlink()
        },
    }


def _artifact_input_record(root: Path, path: Path, *, append_only: bool, required: bool) -> dict[str, Any]:
    record = {
        "path": relative_path(root, path),
        "sha256": file_sha256(path),
        "size_bytes": path.stat().st_size,
        "required_after_cleanup": required,
        "validation_mode": "append_only_prefix" if append_only else "exact",
    }
    return record


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_path(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return path.name


def display_path(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path.resolve())
