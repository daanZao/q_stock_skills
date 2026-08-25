"""query_daily 接缝测试：fake 适配器 + 真实 PG 测试库（issue #3）。

覆盖：自愈补抓后返回完整区间、重复调用幂等、中段缺口不重抓、
全失败报错（attempted_sources，不返回过期/伪造数据）、days 截取、自描述 JSON。
"""

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import select_daily, upsert_daily
from qstock_mcp.tools_daily import query_daily

from fakes import FakeAdapter, weekday_rows


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def test_query_heals_empty_db_and_returns_full_range(pg_test):
    _init(pg_test)
    ef = FakeAdapter("efinance")
    result = query_daily("600519", start="20240101", end="20240110", adapters=[ef])
    assert result["status"] == "ok"
    assert result["tool"] == "query_daily"
    assert result["params"]["stock_code"] == "600519"
    assert result["range"] == {"start": "20240101", "end": "20240110"}
    assert result["count"] == 8
    assert result["data_range"] == {"start": "20240101", "end": "20240110"}
    assert result["healed"] == [
        {"start": "20240101", "end": "20240110", "source": "efinance", "rows": 8}
    ]
    assert result["rows"][0]["source"] == "efinance"
    assert [r["trade_date"] for r in result["rows"]] == [
        r["trade_date"] for r in weekday_rows("20240101", "20240110")
    ]


def test_query_repeated_calls_are_idempotent(pg_test):
    _init(pg_test)
    ef = FakeAdapter("efinance")
    first = query_daily("600519", start="20240101", end="20240110", adapters=[ef])
    second = query_daily("600519", start="20240101", end="20240110", adapters=[ef])
    assert first["count"] == second["count"] == 8
    assert len(ef.calls) == 1  # 第二次全部本地命中
    assert second["healed"] == []
    conn = psycopg2.connect(pg_test)
    assert len(select_daily(conn, "600519", "qfq", "20240101", "20240110")) == 8
    conn.close()


def test_query_does_not_refetch_mid_gap(pg_test):
    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    # 中段缺口（01-09~01-10 缺失）视为停牌/非交易日，不补抓
    upsert_daily(conn, "600519", "qfq", weekday_rows("20240105", "20240108"), "efinance")
    upsert_daily(conn, "600519", "qfq", weekday_rows("20240111", "20240112"), "efinance")
    conn.close()
    ef = FakeAdapter("efinance")
    result = query_daily("600519", start="20240105", end="20240112", adapters=[ef])
    assert result["status"] == "ok"
    assert ef.calls == []
    assert result["count"] == 4


def test_query_all_sources_failed_returns_error_without_data(pg_test):
    _init(pg_test)
    adapters = [FakeAdapter(n, fail_times=99) for n in ("efinance", "akshare", "baostock")]
    result = query_daily("600519", start="20240101", end="20240110", adapters=adapters)
    assert result["status"] == "error"
    assert [a["source"] for a in result["attempted_sources"]] == [
        "efinance",
        "akshare",
        "baostock",
    ]
    assert "rows" not in result  # 不返回任何疑似数据


def test_query_days_returns_last_n_rows(pg_test):
    _init(pg_test)
    ef = FakeAdapter("efinance")
    result = query_daily("600519", days=5, adapters=[ef])
    assert result["status"] == "ok"
    assert result["count"] == 5
    dates = [r["trade_date"] for r in result["rows"]]
    assert dates == sorted(dates)
    # 取的是最近 5 根，而不是区间头部 5 根
    all_rows = query_daily("600519", days=60, adapters=[ef])["rows"]
    assert dates == [r["trade_date"] for r in all_rows[-5:]]
