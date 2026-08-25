"""repository 接缝测试：真实 PG 测试库，覆盖 upsert 幂等与区间读取（issue #3）。

业务键 (stock_code, trade_date, adj)：重复 upsert 不产生重复行，且更新为最新值；
每行记录实际来源 source。
"""

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import select_daily, select_dates, upsert_daily

ROWS = [
    {"trade_date": "20240102", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
     "volume": 1000, "amount": 10200.0, "amplitude": 7.0, "change_percent": 2.0,
     "change_amount": 0.2, "turnover_rate": 1.5},
    {"trade_date": "20240103", "open": 10.2, "high": 10.6, "low": 10.1, "close": 10.4,
     "volume": 1200, "amount": 12480.0, "amplitude": 4.9, "change_percent": 1.96,
     "change_amount": 0.2, "turnover_rate": 1.8},
]


def _conn(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    return conn


def test_upsert_then_select_roundtrip(pg_test):
    conn = _conn(pg_test)
    upsert_daily(conn, "600519", "qfq", ROWS, "efinance")
    rows = select_daily(conn, "600519", "qfq", "20240101", "20240131")
    assert [r["trade_date"] for r in rows] == ["20240102", "20240103"]
    assert rows[0]["close"] == 10.2
    assert rows[0]["source"] == "efinance"
    conn.close()


def test_upsert_is_idempotent_no_duplicate_rows(pg_test):
    conn = _conn(pg_test)
    upsert_daily(conn, "600519", "qfq", ROWS, "efinance")
    upsert_daily(conn, "600519", "qfq", ROWS, "efinance")
    rows = select_daily(conn, "600519", "qfq", "20240101", "20240131")
    assert len(rows) == 2
    conn.close()


def test_upsert_updates_existing_row_and_source(pg_test):
    conn = _conn(pg_test)
    upsert_daily(conn, "600519", "qfq", ROWS, "efinance")
    corrected = [dict(ROWS[0], close=99.9)]
    upsert_daily(conn, "600519", "qfq", corrected, "akshare")
    rows = select_daily(conn, "600519", "qfq", "20240101", "20240131")
    assert len(rows) == 2
    assert rows[0]["close"] == 99.9
    assert rows[0]["source"] == "akshare"
    conn.close()


def test_select_dates_only_returns_trade_dates(pg_test):
    conn = _conn(pg_test)
    upsert_daily(conn, "600519", "qfq", ROWS, "efinance")
    assert select_dates(conn, "600519", "qfq", "20240101", "20240131") == {
        "20240102",
        "20240103",
    }
    # 区间外的不算
    assert select_dates(conn, "600519", "qfq", "20240103", "20240131") == {"20240103"}
    conn.close()


def test_adj_isolates_rows(pg_test):
    conn = _conn(pg_test)
    upsert_daily(conn, "600519", "qfq", ROWS, "efinance")
    assert select_daily(conn, "600519", "hfq", "20240101", "20240131") == []
    conn.close()
