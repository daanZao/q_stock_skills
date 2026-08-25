"""query_snapshot 接缝测试：fake 适配器落数据 + 真实 PG 测试库（issue #4）。

覆盖：按日期查询、按代码过滤、日期缺省取库内最新交易日、自描述 JSON。
查询不做自愈补抓（快照是全市场单次调用，无逐只补齐语义）。
"""

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_snapshot import fetch_market_snapshot, query_snapshot

from fakes import SNAPSHOT_ROWS, FakeSnapshotAdapter


def _seed(pg_test, trade_date: str):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()
    fetch_market_snapshot(adapters=[FakeSnapshotAdapter("efinance", trade_date=trade_date)])


def test_query_by_date_returns_self_describing_rows(pg_test):
    _seed(pg_test, "20240105")
    result = query_snapshot(trade_date="2024-01-05")
    assert result["status"] == "ok"
    assert result["tool"] == "query_snapshot"
    assert result["params"] == {"trade_date": "2024-01-05", "stock_code": None}
    assert result["trade_date"] == "20240105"
    assert result["count"] == len(SNAPSHOT_ROWS)
    row = result["rows"][0]
    assert row["trade_date"] == "20240105"
    assert row["source"] == "efinance"
    assert {r["stock_code"] for r in result["rows"]} == {"600519", "000001"}


def test_query_filters_by_stock_code(pg_test):
    _seed(pg_test, "20240105")
    result = query_snapshot(trade_date="20240105", stock_code="600519")
    assert result["count"] == 1
    assert result["rows"][0]["stock_code"] == "600519"
    assert result["rows"][0]["stock_name"] == "贵州茅台"


def test_query_defaults_to_latest_date_in_db(pg_test):
    _seed(pg_test, "20240104")
    _seed(pg_test, "20240105")
    result = query_snapshot()
    assert result["status"] == "ok"
    assert result["trade_date"] == "20240105"
    assert result["count"] == len(SNAPSHOT_ROWS)


def test_query_empty_db_returns_zero_count(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()
    result = query_snapshot()
    assert result["status"] == "ok"
    assert result["trade_date"] is None
    assert result["count"] == 0
    assert result["rows"] == []


def test_query_rejects_bad_date_param(pg_test):
    result = query_snapshot(trade_date="not-a-date")
    assert result["status"] == "error"
