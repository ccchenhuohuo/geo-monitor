"""Stable public facade for the optional DuckDB analysis store."""

from __future__ import annotations

from .duckdb_store.contracts import (
    CORE_RESULT_CSV_STEMS,
    DUCKDB_SCHEMA_VERSION,
    INTELLIGENCE_CSV_STEMS,
    TERMINAL_ATTEMPT_STATUSES,
    DuckDBError,
)
from .duckdb_store.ingest import build_duckdb
from .duckdb_store.query import connect_readonly, inspect_duckdb, query_duckdb, validate_schema

__all__ = [
    "CORE_RESULT_CSV_STEMS",
    "DUCKDB_SCHEMA_VERSION",
    "INTELLIGENCE_CSV_STEMS",
    "TERMINAL_ATTEMPT_STATUSES",
    "DuckDBError",
    "build_duckdb",
    "connect_readonly",
    "inspect_duckdb",
    "query_duckdb",
    "validate_schema",
]
