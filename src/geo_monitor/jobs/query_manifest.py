"""Frozen query-manifest materialization, integrity, and source trust."""

from __future__ import annotations

import csv
import hashlib
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ..dataset import encode_manifest_csv_cell, load_queries
from ..filesystem import ensure_private_directory, open_private_text, prepare_private_output, secure_private_file
from ..schemas import QueryRecord
from .bundle_files import ensure_bundle_regular_file
from .contracts import (
    GEO_JOB_V2,
    GEO_JOB_V3,
    QUERY_MANIFEST,
    QUERY_MANIFEST_V1,
    RUNS_DIR,
    JobError,
    QueryManifestIntegrityError,
    QueryManifestSourceError,
)

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def query_manifest_path(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir) / QUERY_MANIFEST


def load_job_queries(bundle_dir: str | Path, manifest: dict[str, Any] | None = None, *, materialize: bool = False) -> list[QueryRecord]:
    root = Path(bundle_dir)
    if manifest is None:
        from .manifest import load_job_manifest

        manifest = load_job_manifest(root)
    current = resolve_query_manifest(root, manifest, materialize=materialize)
    if current.exists():
        return load_queries(current)
    source = resolve_query_manifest_source(manifest, bundle_dir=root)
    if source is not None and source.exists():
        ensure_manifest_file_fingerprint(source, manifest)
        return load_queries(source)
    return query_records_from_manifest(manifest)


def resolve_query_manifest(
    bundle_dir: str | Path,
    manifest: dict[str, Any] | None = None,
    *,
    materialize: bool = False,
) -> Path:
    root = Path(bundle_dir)
    current = root / QUERY_MANIFEST
    if manifest is None:
        from .manifest import load_job_manifest

        manifest = load_job_manifest(root)
    if current.is_symlink():
        raise QueryManifestIntegrityError(f"bundle query manifest 不能是 symlink：{current}")
    if current.exists():
        ensure_bundle_regular_file(root, current, "query_manifest.csv")
        ensure_query_manifest_matches(current, manifest)
        return current
    queries = manifest.get("queries")
    if not isinstance(queries, list) or not queries:
        if materialize:
            source = resolve_query_manifest_source(manifest, bundle_dir=root)
            if source is not None and source.exists():
                ensure_manifest_file_fingerprint(source, manifest)
                prepare_private_output(current)
                shutil.copyfile(source, current)
                secure_private_file(current)
                return current
        return current
    if materialize:
        write_query_manifest(current, queries)
    return current


def ensure_query_manifest(
    bundle_dir: str | Path,
    manifest: dict[str, Any] | None = None,
    *,
    replacement_path: str | Path | None = None,
) -> Path:
    root = Path(bundle_dir)
    if manifest is None:
        from .manifest import load_job_manifest

        manifest = load_job_manifest(root)
    current = root / QUERY_MANIFEST
    if replacement_path is not None:
        replacement = Path(replacement_path)
        ensure_manifest_file_fingerprint(replacement, manifest)
        prepare_private_output(current)
        shutil.copyfile(replacement, current)
        secure_private_file(current)
    return resolve_query_manifest(root, manifest, materialize=True)


def write_query_manifest(path: Path, queries: list[dict[str, Any]]) -> None:
    ensure_private_directory(path.parent)
    preferred = ["query_id", "query", "locale", "market", "category", "tags", "stage", "persona"]
    extra = sorted({key for row in queries for key in row} - set(preferred))
    fieldnames = [key for key in preferred if any(key in row for row in queries)] + extra
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open_private_text(tmp_path, encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(
                [
                    {key: encode_manifest_csv_cell(value) if isinstance(value, str) else value for key, value in row.items()}
                    for row in queries
                ]
            )
        os.replace(tmp_path, path)
        secure_private_file(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def query_rows_from_records(records: list[QueryRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        validate_slug_id(record.query_id, "query_id")
        row: dict[str, Any] = {"query_id": record.query_id, "query": record.query}
        if record.locale:
            row["locale"] = record.locale
        if record.market:
            row["market"] = record.market
        if record.category:
            row["category"] = record.category
        if record.tags:
            row["tags"] = ",".join(record.tags)
        row.update(record.metadata)
        for key in ["seed_id", "persona", "template_id", "variant_id"]:
            if row.get(key):
                validate_slug_id(str(row[key]), key)
        rows.append(row)
    return rows


def query_manifest_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise JobError(f"query manifest 不存在：{path}")
    records = load_queries(path)
    query_rows_from_records(records)
    return {
        "source_type": "external_file",
        "source_uri": str(path),
        "source_uri_base": str(Path.cwd()),
        "schema_version": QUERY_MANIFEST_V1,
        "sha256": file_sha256(path),
        "row_count": len(records),
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_query_manifest_source(manifest: dict[str, Any], *, bundle_dir: str | Path | None = None) -> Path | None:
    info = manifest.get("query_manifest")
    if not isinstance(info, dict) or info.get("source_type") != "external_file":
        return None
    source_uri = info.get("source_uri")
    if not source_uri:
        return None
    path = Path(str(source_uri))
    if not path.is_absolute():
        base = info.get("source_uri_base")
        path = Path(str(base)) / path if base else path
    if bundle_dir is not None:
        ensure_trusted_query_manifest_source(path, Path(bundle_dir))
    return path


def ensure_trusted_query_manifest_source(path: Path, bundle_dir: Path) -> None:
    try:
        resolved = path.resolve(strict=False)
    except Exception as exc:
        raise QueryManifestSourceError(f"query_manifest.source_uri 无法解析：{path}") from exc
    trusted_roots = trusted_query_manifest_roots(bundle_dir)
    if any(is_relative_to(resolved, root) for root in trusted_roots):
        return
    roots = ", ".join(str(root) for root in trusted_roots)
    raise QueryManifestSourceError(f"query_manifest.source_uri 不在可信目录内：{path}；可信目录：{roots}。如需使用该文件，请显式传入 --query-manifest。")


def trusted_query_manifest_roots(bundle_dir: Path) -> list[Path]:
    candidates = [bundle_dir, bundle_dir.parent]
    if bundle_dir.parent.name in {"runs", RUNS_DIR}:
        candidates.append(bundle_dir.parent.parent)
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
        except Exception:
            continue
        if resolved != resolved.parent and resolved not in roots:
            roots.append(resolved)
    return roots


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_manifest_file_fingerprint(path: Path, manifest: dict[str, Any]) -> None:
    if not path.exists():
        raise JobError(f"query manifest 不存在：{path}")
    info = manifest.get("query_manifest")
    if not isinstance(info, dict):
        return
    expected_sha = str(info.get("sha256") or "")
    expected_count = int(info.get("row_count") or 0)
    if expected_sha and file_sha256(path) != expected_sha:
        raise QueryManifestIntegrityError(f"query manifest sha256 不匹配：{path}")
    records = load_queries(path)
    if expected_count and len(records) != expected_count:
        raise QueryManifestIntegrityError(f"query manifest row_count 不匹配：{path}")
    query_rows_from_records(records)


def validate_slug_id(value: str, field: str) -> None:
    if not SLUG_RE.fullmatch(value):
        raise JobError(f"{field} 只能包含 [a-zA-Z0-9_-]：{value}")


def query_records_from_manifest(manifest: dict[str, Any]) -> list[QueryRecord]:
    records: list[QueryRecord] = []
    known = {"query_id", "query", "locale", "market", "category", "tags"}
    for row in manifest.get("queries", []):
        tags = normalize_metadata_value(row.get("tags"))
        records.append(
            QueryRecord(
                query_id=str(row.get("query_id") or "").strip(),
                query=str(row.get("query") or "").strip(),
                locale=str(row["locale"]).strip() if row.get("locale") else None,
                market=str(row["market"]).strip() if row.get("market") else None,
                category=str(row["category"]).strip() if row.get("category") else None,
                tags=[item.strip() for item in tags.split(",") if item.strip()],
                metadata={key: value for key, value in row.items() if key not in known and value not in (None, "")},
            )
        )
    return records


def ensure_query_manifest_matches(path: Path, manifest: dict[str, Any]) -> None:
    info = manifest.get("query_manifest") if isinstance(manifest.get("query_manifest"), dict) else {}
    if str(manifest.get("schema_version")) in {GEO_JOB_V2, GEO_JOB_V3} and info.get("source_type") == "external_file":
        ensure_manifest_file_fingerprint(path, manifest)
        return
    loaded_queries = load_queries(path)
    query_pairs = [(row.query_id, row.query) for row in loaded_queries]
    manifest_pairs = [(str(row.get("query_id")), str(row.get("query"))) for row in manifest.get("queries", [])]
    if query_pairs != manifest_pairs:
        raise JobError(f"{path.name} 与 job_manifest queries 不一致，请删除旧文件或重新 build-job")
    loaded_by_id = {row.query_id: row for row in loaded_queries}
    for manifest_row in manifest.get("queries", []):
        query_id = str(manifest_row.get("query_id"))
        loaded = loaded_by_id.get(query_id)
        if loaded is None:
            continue
        metadata = loaded.metadata_with_tags()
        for key, expected in manifest_row.items():
            if key in {"query_id", "query"}:
                continue
            actual = metadata.get(key)
            if normalize_metadata_value(actual) != normalize_metadata_value(expected):
                raise JobError(f"{path.name} 与 job_manifest query 元数据不一致：{query_id}.{key}")


def normalize_metadata_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()
