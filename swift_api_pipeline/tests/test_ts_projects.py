"""Tests for ts_projects helpers (dynamic TS project lists)."""
import asyncio
import pytest

from ts_projects import fetch_ts_projects, fetch_qa_export_projects, partition_by_rows


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = None
        self.last_args = None

    async def fetch(self, query, *args):
        self.last_query = " ".join(query.split())
        self.last_args = args
        return self.rows


def test_fetch_ts_projects_maps_rows_and_filters_by_number():
    rows = [
        {"project_name": "TECH-OPS: TS13", "project_did": "-A", "project_number": 13},
        {"project_name": "TECH-OPS: TS20", "project_did": "-B", "project_number": 20},
    ]
    conn = FakeConn(rows)
    out = asyncio.run(fetch_ts_projects(conn))
    assert out == rows
    assert "ref_ontel_techops_projects" in conn.last_query
    assert "project_number >= $1" in conn.last_query
    assert conn.last_args == (13,)


def test_fetch_ts_projects_custom_min_number():
    conn = FakeConn([])
    asyncio.run(fetch_ts_projects(conn, min_number=17))
    assert conn.last_args == (17,)


def test_fetch_qa_export_projects_joins_registry():
    rows = [{"project_name": "TECH-OPS: TS13", "project_did": "-A"}]
    conn = FakeConn(rows)
    out = asyncio.run(fetch_qa_export_projects(conn))
    assert out == [("TECH-OPS: TS13", "-A")]
    assert "ref_qa_forms" in conn.last_query
    assert "active" in conn.last_query


def test_partition_by_rows_splits_empty_projects():
    with_rows, empty = partition_by_rows(
        ["TS13", "TS19", "TS20"], {"TS13": 100, "TS19": 5, "TS20": 0}
    )
    assert with_rows == ["TS13", "TS19"]
    assert empty == ["TS20"]


def test_partition_by_rows_missing_count_is_empty():
    with_rows, empty = partition_by_rows(["TS13", "TS20"], {"TS13": 1})
    assert empty == ["TS20"]
