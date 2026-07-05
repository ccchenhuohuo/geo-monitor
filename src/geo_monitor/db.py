from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from .job import JOB_MANIFEST, RAW_ATTEMPTS, load_job_manifest


DUCKDB_SCHEMA_VERSION = "duckdb-schema-v1"


class DuckDBError(ValueError):
    pass


def build_duckdb(runs_dir: str | Path, output_path: str | Path, *, query_manifest: str | Path | None = None) -> dict[str, Any]:
    duckdb = _duckdb()
    runs_root = Path(runs_dir)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(str(tmp))
    try:
        _create_schema(con)
        fallback_rows = _load_fallback_manifest(query_manifest)
        counts = {"runs": 0, "queries": 0, "attempts": 0, "quality_flags": 0}
        for run_dir in _iter_run_dirs(runs_root):
            run_counts = _ingest_run(con, run_dir, fallback_rows)
            for key, value in run_counts.items():
                counts[key] = counts.get(key, 0) + value
        _create_views(con)
        con.close()
        os.replace(tmp, output)
    except Exception:
        con.close()
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return {"db_path": str(output), **counts, "schema_version": DUCKDB_SCHEMA_VERSION}


def inspect_duckdb(db_path: str | Path) -> dict[str, Any]:
    duckdb = _duckdb()
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("select table_name from information_schema.tables where table_schema='main' order by table_name").fetchall()
        tables = []
        for (name,) in rows:
            count = con.execute(f"select count(*) from {name}").fetchone()[0]
            tables.append({"table": name, "row_count": count})
        return {"db_path": str(db_path), "tables": tables}
    finally:
        con.close()


def query_duckdb(db_path: str | Path, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    duckdb = _duckdb()
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        result = con.execute(sql)
        columns = [item[0] for item in result.description or []]
        return columns, result.fetchall()
    finally:
        con.close()


def _create_schema(con: Any) -> None:
    con.execute("create table schema_info(schema_version varchar)")
    con.execute("insert into schema_info values (?)", [DUCKDB_SCHEMA_VERSION])
    con.execute(
        """
        create table runs(
            job_id varchar primary key,
            status varchar,
            created_at varchar,
            completed_at varchar,
            target_brand varchar,
            model varchar,
            sample_count integer,
            query_manifest_sha256 varchar,
            query_manifest_source_type varchar,
            query_manifest_source_uri varchar,
            report_path varchar
        )
        """
    )
    con.execute(
        """
        create table queries(
            job_id varchar,
            query_id varchar,
            variant_id varchar,
            seed_id varchar,
            seed_query varchar,
            category varchar,
            intent varchar,
            persona varchar,
            template_id varchar,
            query varchar,
            language varchar,
            generation_method varchar,
            fanout_version varchar,
            manifest_version varchar
        )
        """
    )
    con.execute(
        """
        create table attempts(
            job_id varchar,
            attempt_id varchar,
            query_id varchar,
            status varchar,
            latency_ms integer,
            error varchar,
            model varchar,
            created_at varchar,
            completed_at varchar,
            response_preview varchar,
            response_length integer,
            raw_path varchar,
            raw_line_number integer
        )
        """
    )
    con.execute("create table brand_summary(job_id varchar, brand_name_canonical varchar, sov_event_share double, response_mention_rate double, query_coverage_rate double, is_target_brand integer)")
    con.execute("create table brand_by_query(job_id varchar, query_id varchar, brand_name_canonical varchar, responses_mentioned integer, mention_rate_within_query double)")
    con.execute("create table query_stability(job_id varchar, query_id varchar, successful_repeats integer, expected_repeats integer, brand_set_jaccard_avg double)")
    con.execute("create table source_domains(job_id varchar, domain varchar, response_coverage_rate double, query_coverage_rate double)")
    con.execute("create table source_urls(job_id varchar, url varchar, domain varchar, title varchar, parsed_source_occurrences integer)")
    con.execute("create table quality_flags(job_id varchar, type varchar, message varchar, path varchar, raw_line_number integer, query_id varchar)")


def _create_views(con: Any) -> None:
    con.execute(
        """
        create view metrics_by_seed as
        select q.seed_id, count(distinct q.query_id) as query_count, count(a.attempt_id) as attempt_count
        from queries q left join attempts a on q.job_id = a.job_id and q.query_id = a.query_id
        group by q.seed_id
        """
    )
    con.execute(
        """
        create view metrics_by_persona as
        select q.persona, count(distinct q.query_id) as query_count, count(a.attempt_id) as attempt_count
        from queries q left join attempts a on q.job_id = a.job_id and q.query_id = a.query_id
        group by q.persona
        """
    )
    con.execute("create view metrics_by_run as select job_id, count(*) as attempt_count from attempts group by job_id")


def _iter_run_dirs(runs_root: Path) -> list[Path]:
    if not runs_root.exists():
        return []
    candidates: list[Path] = []
    for path in sorted(item for item in runs_root.iterdir() if item.is_dir()):
        if path.name.startswith("."):
            continue
        if (path / JOB_MANIFEST).exists() or (path / RAW_ATTEMPTS).exists():
            candidates.append(path)
    return candidates


def _ingest_run(con: Any, run_dir: Path, fallback_rows: dict[str, dict[str, str]]) -> dict[str, int]:
    counts = {"runs": 0, "queries": 0, "attempts": 0, "quality_flags": 0}
    manifest_path = run_dir / JOB_MANIFEST
    if not manifest_path.exists():
        _quality(con, run_dir.name, "missing_job_manifest", "job_manifest.json missing", str(manifest_path))
        counts["quality_flags"] += 1
        return counts
    try:
        manifest = load_job_manifest(run_dir)
    except Exception as exc:
        _quality(con, run_dir.name, "bad_job_manifest", str(exc), str(manifest_path))
        counts["quality_flags"] += 1
        return counts
    job_id = str(manifest.get("job_id") or run_dir.name)
    raw_path = run_dir / RAW_ATTEMPTS
    attempts, query_rows, flags = _read_attempts(raw_path, job_id, fallback_rows)
    for flag in flags:
        _quality(con, job_id, **flag)
    counts["quality_flags"] += len(flags)
    if not raw_path.exists():
        _quality(con, job_id, "missing_raw_attempts", "raw/attempts.jsonl missing", str(raw_path))
        counts["quality_flags"] += 1
    info = manifest.get("query_manifest") if isinstance(manifest.get("query_manifest"), dict) else {}
    report_path = run_dir / "result" / "report.html"
    completed_at = _completed_at(run_dir, attempts)
    con.execute(
        "insert into runs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            job_id,
            manifest.get("status"),
            manifest.get("created_at"),
            completed_at,
            manifest.get("target_brand"),
            manifest.get("model"),
            len(attempts),
            info.get("sha256", ""),
            info.get("source_type", ""),
            info.get("source_uri", ""),
            str(report_path) if report_path.exists() else "",
        ],
    )
    counts["runs"] += 1
    seen_attempts: set[str] = set()
    for attempt in attempts:
        if attempt["attempt_id"] in seen_attempts:
            _quality(con, job_id, "duplicate_attempt_id", "duplicate attempt_id retained", attempt["raw_path"], attempt["raw_line_number"], attempt["query_id"])
            counts["quality_flags"] += 1
        else:
            seen_attempts.add(attempt["attempt_id"])
        con.execute(
            "insert into attempts values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job_id,
                attempt["attempt_id"],
                attempt["query_id"],
                attempt["status"],
                attempt["latency_ms"],
                attempt["error"],
                attempt["model"],
                attempt["created_at"],
                attempt["completed_at"],
                attempt["response_preview"],
                attempt["response_length"],
                attempt["raw_path"],
                attempt["raw_line_number"],
            ],
        )
        counts["attempts"] += 1
    for row in query_rows.values():
        con.execute(
            "insert into queries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [job_id, row["query_id"], row["variant_id"], row["seed_id"], row["seed_query"], row["category"], row["intent"], row["persona"], row["template_id"], row["query"], row["language"], row["generation_method"], row["fanout_version"], row["manifest_version"]],
        )
        counts["queries"] += 1
    counts["quality_flags"] += _ingest_csv_outputs(con, run_dir, job_id)
    return counts


def _read_attempts(raw_path: Path, job_id: str, fallback_rows: dict[str, dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    queries: dict[str, dict[str, str]] = {}
    query_field_priority: dict[str, dict[str, int]] = {}
    flags: list[dict[str, Any]] = []
    if not raw_path.exists():
        return attempts, queries, flags
    with raw_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                flags.append({"type": "malformed_jsonl", "message": str(exc), "path": str(raw_path), "raw_line_number": line_no, "query_id": ""})
                continue
            qid = str(record.get("query_id") or "")
            query = str(record.get("query") or record.get("input_query") or "")
            meta = record.get("query_meta") if isinstance(record.get("query_meta"), dict) else {}
            fallback = fallback_rows.get(qid)
            if not meta and fallback:
                meta = fallback
                flags.append({"type": "query_meta_fallback_used", "message": "query_meta filled from fallback manifest", "path": str(raw_path), "raw_line_number": line_no, "query_id": qid})
            elif not meta:
                flags.append({"type": "query_meta_missing", "message": "query_meta missing", "path": str(raw_path), "raw_line_number": line_no, "query_id": qid})
            elif fallback:
                conflicts = [key for key, value in fallback.items() if key in meta and str(meta.get(key) or "") and str(value or "") and str(meta.get(key)) != str(value)]
                if conflicts:
                    flags.append({"type": "query_meta_conflict", "message": ",".join(conflicts), "path": str(raw_path), "raw_line_number": line_no, "query_id": qid})
            error = record.get("error") if isinstance(record.get("error"), dict) else {}
            response_text = str(record.get("response_text") or "")
            attempt_id = str(record.get("attempt_id") or f"{job_id}__{qid}__r{record.get('repeat_index', 1)}__{record.get('request_hash', '')}")
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "query_id": qid,
                    "status": str(record.get("status") or ""),
                    "latency_ms": _to_int(record.get("latency_ms")),
                    "error": str(error.get("message") or error.get("type") or ""),
                    "model": str(record.get("model") or ""),
                    "created_at": str(record.get("started_at") or ""),
                    "completed_at": str(record.get("completed_at") or ""),
                    "response_preview": response_text[:500],
                    "response_length": len(response_text),
                    "raw_path": str(raw_path),
                    "raw_line_number": line_no,
                }
            )
            if qid:
                priority = 3 if record.get("status") in {"success", "mock"} and record.get("query_meta") else 2 if record.get("query_meta") else 1 if fallback else 0
                _merge_query_row(queries, query_field_priority, qid, query, meta, priority)
    return attempts, queries, flags


def _merge_query_row(
    queries: dict[str, dict[str, str]],
    priorities: dict[str, dict[str, int]],
    qid: str,
    query: str,
    meta: dict[str, Any],
    priority: int,
) -> None:
    row = queries.setdefault(
        qid,
        {
            "query_id": qid,
            "variant_id": "",
            "seed_id": "",
            "seed_query": "",
            "category": "",
            "intent": "",
            "persona": "",
            "template_id": "",
            "query": "",
            "language": "",
            "generation_method": "",
            "fanout_version": "",
            "manifest_version": "",
        },
    )
    field_priorities = priorities.setdefault(qid, {})
    values = {
        "query": query,
        "variant_id": str(meta.get("variant_id") or ""),
        "seed_id": str(meta.get("seed_id") or ""),
        "seed_query": str(meta.get("seed_query") or ""),
        "category": str(meta.get("category") or ""),
        "intent": str(meta.get("intent") or ""),
        "persona": str(meta.get("persona") or ""),
        "template_id": str(meta.get("template_id") or ""),
        "language": str(meta.get("language") or ""),
        "generation_method": str(meta.get("generation_method") or ""),
        "fanout_version": str(meta.get("fanout_version") or ""),
        "manifest_version": str(meta.get("manifest_version") or ""),
    }
    for key, value in values.items():
        if not value:
            continue
        current_priority = field_priorities.get(key, -1)
        if not row.get(key) or priority >= current_priority:
            row[key] = value
            field_priorities[key] = priority


def _ingest_csv_outputs(con: Any, run_dir: Path, job_id: str) -> int:
    result = run_dir / "result"
    quality_count = 0
    expected_files = [
        "brand_summary.csv",
        "brand_by_query.csv",
        "query_stability.csv",
        "source_domains.csv",
        "source_urls.csv",
    ]
    for name in expected_files:
        path = result / name
        if not path.exists():
            _quality(con, job_id, "missing_result_csv", f"result CSV missing: {name}", str(path))
            quality_count += 1
    for row in _read_csv(result / "brand_summary.csv"):
        con.execute("insert into brand_summary values (?, ?, ?, ?, ?, ?)", [job_id, row.get("brand_name_canonical", ""), _pct(row.get("sov_event_share")), _pct(row.get("response_mention_rate")), _pct(row.get("query_coverage_rate")), _to_int(row.get("is_target_brand"))])
    for row in _read_csv(result / "brand_by_query.csv"):
        con.execute("insert into brand_by_query values (?, ?, ?, ?, ?)", [job_id, row.get("query_id", ""), row.get("brand_name_canonical", ""), _to_int(row.get("responses_mentioned")), _pct(row.get("mention_rate_within_query"))])
    for row in _read_csv(result / "query_stability.csv"):
        con.execute("insert into query_stability values (?, ?, ?, ?, ?)", [job_id, row.get("query_id", ""), _to_int(row.get("successful_repeats")), _to_int(row.get("expected_repeats")), _to_float(row.get("brand_set_jaccard_avg"))])
    for row in _read_csv(result / "source_domains.csv"):
        con.execute("insert into source_domains values (?, ?, ?, ?)", [job_id, row.get("domain", ""), _pct(row.get("response_coverage_rate")), _pct(row.get("query_coverage_rate"))])
    for row in _read_csv(result / "source_urls.csv"):
        con.execute("insert into source_urls values (?, ?, ?, ?, ?)", [job_id, row.get("url", ""), row.get("domain", ""), row.get("title", ""), _to_int(row.get("parsed_source_occurrences"))])
    return quality_count


def _load_fallback_manifest(path: str | Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    rows: dict[str, dict[str, str]] = {}
    for row in _read_csv(Path(path)):
        qid = str(row.get("query_id") or "")
        if qid:
            rows[qid] = {key: str(value or "") for key, value in row.items()}
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _quality(con: Any, job_id: str, type: str, message: str, path: str, raw_line_number: int | None = None, query_id: str = "") -> None:
    con.execute("insert into quality_flags values (?, ?, ?, ?, ?, ?)", [job_id, type, message, path, raw_line_number, query_id])


def _completed_at(run_dir: Path, attempts: list[dict[str, Any]]) -> str:
    summary = run_dir / "logs" / "run_summary.json"
    if summary.exists():
        try:
            return str(json.loads(summary.read_text(encoding="utf-8")).get("completed_at") or "")
        except Exception:
            pass
    values = [str(row.get("completed_at") or "") for row in attempts if row.get("completed_at")]
    return max(values) if values else ""


def _pct(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("%"):
        return _to_float(text[:-1], scale=100.0)
    return _to_float(text)


def _to_float(value: Any, *, scale: float = 1.0) -> float | None:
    try:
        return float(value) / scale
    except Exception:
        return None


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


def _duckdb() -> Any:
    try:
        import duckdb
    except ModuleNotFoundError as exc:
        raise DuckDBError("缺少 duckdb 依赖，请安装项目依赖：pip install -e .") from exc
    return duckdb
