from pathlib import Path

import pytest
from typer.testing import CliRunner

from geo_monitor.cli import app
from geo_monitor.db import DuckDBError, inspect_duckdb, query_duckdb

pytest.importorskip("duckdb")


def _make_db(path: Path) -> None:
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("create table t(x int)")
    con.execute("insert into t values (1)")
    con.close()


def test_query_duckdb_allows_plain_select(tmp_path):
    db = tmp_path / "safe.duckdb"
    _make_db(db)

    columns, rows = query_duckdb(db, "select count(*) from t")

    assert columns == ["count_star()"]
    assert rows == [(1,)]


def test_query_duckdb_rejects_multiple_statements(tmp_path):
    db = tmp_path / "safe.duckdb"
    _make_db(db)

    with pytest.raises(DuckDBError, match="单条"):
        query_duckdb(db, "select count(*) from t; select 1")


def test_query_duckdb_rejects_external_file_functions(tmp_path):
    db = tmp_path / "safe.duckdb"
    _make_db(db)

    with pytest.raises(DuckDBError, match="外部文件"):
        query_duckdb(db, "select * from read_csv_auto('/etc/passwd')")


def test_query_duckdb_external_access_disabled(tmp_path):
    db = tmp_path / "safe.duckdb"
    _make_db(db)

    with pytest.raises(DuckDBError):
        query_duckdb(db, "select * from '/etc/passwd'")


def test_inspect_duckdb_quotes_malicious_table_names(tmp_path):
    import duckdb

    db = tmp_path / "malicious.duckdb"
    malicious_name = "evil; select * from read_csv_auto('/etc/passwd'); --"
    con = duckdb.connect(str(db))
    con.execute('create table "' + malicious_name.replace('"', '""') + '"(x int)')
    con.execute('insert into "' + malicious_name.replace('"', '""') + '" values (1)')
    con.close()

    result = inspect_duckdb(db)

    assert result["tables"] == [{"table": malicious_name, "row_count": 1}]


def test_db_query_cli_reports_rejected_sql_without_traceback(tmp_path):
    db = tmp_path / "safe.duckdb"
    _make_db(db)

    result = CliRunner().invoke(app, ["db", "query", "--db", str(db), "select * from read_csv_auto('/etc/passwd')"])

    assert result.exit_code != 0
    assert "外部文件" in result.output
    assert "Traceback" not in result.output
