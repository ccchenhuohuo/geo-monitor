"""Safe connections and read-only queries for DuckDB artifacts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .contracts import DUCKDB_SCHEMA_VERSION, INTELLIGENCE_CSV_STEMS, DuckDBError

SAFE_QUERY_START_RE = re.compile(r"^\s*(select|with|show|describe|explain)\b", re.IGNORECASE | re.DOTALL)
FORBIDDEN_SQL_RE = re.compile(
    r"\b(attach|copy|create|delete|detach|drop|export|import|insert|install|load|merge|pragma|set|update|alter|call)\b"
    r"|\b(read_csv|read_csv_auto|read_json|read_ndjson|read_parquet|read_text|read_blob|parquet_scan|glob)\s*\(",
    re.IGNORECASE,
)


def inspect_duckdb(db_path: str | Path) -> dict[str, Any]:
    con = _connect_readonly(db_path)
    try:
        rows = con.execute("select table_name from information_schema.tables where table_schema='main' order by table_name").fetchall()
        tables = []
        for (name,) in rows:
            try:
                count = con.execute(f"select count(*) from {_quote_identifier(str(name))}").fetchone()[0]
            except Exception as exc:
                raise DuckDBError(f"无法 inspect DuckDB 表 {name!r}: {exc}") from exc
            tables.append({"table": name, "row_count": count})
        return {"db_path": str(db_path), "tables": tables}
    finally:
        con.close()


def query_duckdb(db_path: str | Path, sql: str, *, max_rows: int = 10_000) -> tuple[list[str], list[tuple[Any, ...]]]:
    _validate_safe_query_sql(sql)
    con = _connect_readonly(db_path)
    try:
        result = con.execute(sql)
        columns = [item[0] for item in result.description or []]
        rows = result.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            raise DuckDBError(f"查询结果超过 max_rows={max_rows}，请添加过滤或 LIMIT")
        return columns, rows
    except Exception as exc:
        raise DuckDBError(str(exc)) from exc
    finally:
        con.close()


def connect_readonly(db_path: str | Path) -> Any:
    return _connect_readonly(db_path)


def validate_schema(con: Any) -> None:
    try:
        row = con.execute("select schema_version from schema_info limit 1").fetchone()
    except Exception as exc:
        raise DuckDBError(f"DuckDB schema_info 缺失或不可读：{exc}") from exc
    if not row or str(row[0]) != DUCKDB_SCHEMA_VERSION:
        raise DuckDBError(f"DuckDB schema_version 不支持：{row[0] if row else 'unknown'}；请重建 DuckDB")
    required = {
        "runs": {
            "job_id",
            "status",
            "sample_mode",
            "partial_sample",
            "study_fingerprint",
            "sampling_fingerprint",
            "job_conclusion_strength",
            "created_at_ts",
            "completed_at_ts",
        },
        "queries": {"job_id", "query_id", "query", "query_metadata_json"},
        "attempts": {
            "job_id",
            "attempt_id",
            "query_id",
            "repeat_index",
            "run_generation",
            "diagnostic_generation",
            "execution_mode",
            "completed_at_ts",
            "web_search_requirement_status",
            "source_parse_status",
        },
        "quality_summary": {"job_id", "sample_mode", "stats_record_count"},
        "attempt_facts": {"job_id", "run_generation", "query_id", "repeat_index", "latest_status", "completed_at_ts", "stats_included"},
        "query_facts": {"job_id", "query_id", "planned_attempts", "usable_sample_rate"},
        "brand_attempt_facts": {"job_id", "query_id", "brand_name_canonical", "stats_included"},
        "latest_attempts": {"job_id", "attempt_id", "query_id", "repeat_index", "completed_at_ts"},
        "current_attempts": {"job_id", "attempt_id", "query_id", "repeat_index", "fact_source"},
        "attempt_quality_by_run": {"job_id", "latest_terminal_attempt_count", "stats_included_attempt_count"},
        "intelligence_artifacts": {"job_id", "run_generation", "artifact_stem", "columns_json", "column_types_json"},
        "intelligence_rows": {"job_id", "run_generation", "artifact_stem", "row_number", "row_json"},
        "comparison_cohorts": {"query_manifest_sha256", "comparison_conclusion_strength", "source_metrics_comparable"},
    }
    for stem in INTELLIGENCE_CSV_STEMS:
        required[stem] = {"job_id", "run_generation", "artifact_stem", "row_number", "row_json"}
    for table, columns in required.items():
        try:
            rows = con.execute(
                "select column_name from information_schema.columns where table_schema='main' and table_name=?",
                [table],
            ).fetchall()
        except Exception as exc:
            raise DuckDBError(f"DuckDB schema 校验失败：{table}: {exc}") from exc
        existing = {str(item[0]) for item in rows}
        missing = sorted(columns - existing)
        if missing:
            raise DuckDBError(f"DuckDB schema 缺少 {table} 字段：{', '.join(missing)}；请重建 DuckDB")


def _connect_readonly(db_path: str | Path) -> Any:
    path = Path(db_path)
    if path.is_symlink() or not path.is_file():
        raise DuckDBError(f"DuckDB 必须是普通非 symlink 文件：{path}")
    duckdb = _duckdb()
    try:
        return duckdb.connect(str(db_path), read_only=True, config={"enable_external_access": "false", "memory_limit": "512MB"})
    except TypeError:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            con.execute("set enable_external_access=false")
            con.execute("set memory_limit='512MB'")
        except Exception:
            con.close()
            raise
        return con


def _validate_safe_query_sql(sql: str) -> None:
    text = str(sql or "").strip()
    if not text:
        raise DuckDBError("SQL 查询不能为空")
    without_trailing = text[:-1].strip() if text.endswith(";") else text
    if ";" in without_trailing:
        raise DuckDBError("只允许单条只读 SQL 查询")
    if not SAFE_QUERY_START_RE.search(text):
        raise DuckDBError("只允许 SELECT/WITH/SHOW/DESCRIBE/EXPLAIN 只读查询")
    if FORBIDDEN_SQL_RE.search(text):
        raise DuckDBError("SQL 包含不允许的 DuckDB 语句或外部文件读取函数")


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _duckdb() -> Any:
    try:
        import duckdb
    except ModuleNotFoundError as exc:
        raise DuckDBError("缺少可选 duckdb 依赖；请安装 geo-monitor[duckdb]") from exc
    return duckdb
