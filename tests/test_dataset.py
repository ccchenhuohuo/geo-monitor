from pathlib import Path

import pytest

from geo_monitor.dataset import DatasetError, load_queries, select_queries

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_csv_queries():
    records = load_queries(FIXTURES / "queries.small.csv")
    assert len(records) == 2
    assert records[0].query_id == "q001"
    assert records[0].tags == ["a", "b"]


def test_select_queries_by_limit():
    records = load_queries(FIXTURES / "queries.small.csv")
    selected = select_queries(records, limit=1)
    assert [record.query_id for record in selected] == ["q001"]


def test_select_queries_by_ids():
    records = load_queries(FIXTURES / "queries.small.csv")
    selected = select_queries(records, only_query_ids=["q002"])
    assert [record.query_id for record in selected] == ["q002"]


def test_missing_file_raises():
    with pytest.raises(DatasetError):
        load_queries(FIXTURES / "missing.csv")
