"""fetch_market_snapshot 接缝测试：fake 适配器 + 真实 PG 测试库（issue #4）。

覆盖：单次全市场调用落库、API 返回的最新交易日优先于传入日期、
重复执行幂等（upsert 不产生重复行）、全失败报错（attempted_sources）。
"""

import psycopg2

import qstock_mcp.tools_snapshot as ts
from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_snapshot import fetch_market_snapshot

from fakes import FakeSnapshotAdapter, SNAPSHOT_ROWS


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _snapshot_count(pg_test, trade_date: str) -> int:
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market_snapshot WHERE trade_date = %s", (trade_date,)
        )
        n = cur.fetchone()[0]
    conn.close()
    return n


def test_fetch_lands_rows_under_api_trade_date_not_passed_date(pg_test):
    _init(pg_test)
    ef = FakeSnapshotAdapter("efinance", trade_date="20240105")
    result = fetch_market_snapshot(trade_date="20240101", adapters=[ef])
    assert result["status"] == "ok"
    assert result["tool"] == "fetch_market_snapshot"
    assert result["params"] == {"trade_date": "20240101"}
    # API 返回的最新交易日优先于传入日期
    assert result["trade_date"] == "20240105"
    assert result["trade_date_origin"] == "api"
    assert result["source"] == "efinance"
    assert result["rows_upserted"] == len(SNAPSHOT_ROWS)
    assert ef.calls == 1  # 单次全市场调用
    assert _snapshot_count(pg_test, "20240105") == len(SNAPSHOT_ROWS)
    assert _snapshot_count(pg_test, "20240101") == 0


def test_fetch_repeated_is_idempotent_and_updates_values(pg_test):
    _init(pg_test)
    ef = FakeSnapshotAdapter("efinance", trade_date="20240105")
    fetch_market_snapshot(adapters=[ef])
    # 第二次执行：同一交易日，价格变化 → 更新原行，不产生重复行
    changed = [dict(r, latest_price=r["latest_price"] + 1.0) for r in SNAPSHOT_ROWS]
    ef2 = FakeSnapshotAdapter("efinance", rows=changed, trade_date="20240105")
    second = fetch_market_snapshot(adapters=[ef2])
    assert second["status"] == "ok"
    assert _snapshot_count(pg_test, "20240105") == len(SNAPSHOT_ROWS)
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT latest_price FROM market_snapshot "
            "WHERE trade_date = '20240105' AND stock_code = '600519'"
        )
        assert cur.fetchone()[0] == 1701.0
    conn.close()


def test_fetch_falls_back_to_passed_date_when_api_silent(pg_test):
    _init(pg_test)
    ef = FakeSnapshotAdapter("efinance", trade_date=None)  # API 未报告交易日
    result = fetch_market_snapshot(trade_date="2024-01-05", adapters=[ef])
    assert result["status"] == "ok"
    assert result["trade_date"] == "20240105"
    assert result["trade_date_origin"] == "param"
    assert _snapshot_count(pg_test, "20240105") == len(SNAPSHOT_ROWS)


def test_fetch_falls_back_to_today_when_api_silent_and_no_param(pg_test, monkeypatch):
    _init(pg_test)
    # 固定"今天"使测试确定性
    real_date = ts.date

    class _FakeDate:
        @staticmethod
        def today():
            return real_date(2024, 1, 5)

    monkeypatch.setattr(ts, "date", _FakeDate)
    ef = FakeSnapshotAdapter("efinance", trade_date=None)
    result = fetch_market_snapshot(adapters=[ef])
    assert result["status"] == "ok"
    assert result["trade_date"] == "20240105"
    assert result["trade_date_origin"] == "today"
    assert _snapshot_count(pg_test, "20240105") == len(SNAPSHOT_ROWS)


def test_fetch_all_sources_failed_returns_error_without_writes(pg_test):
    _init(pg_test)
    adapters = [
        FakeSnapshotAdapter(n, fail_times=99) for n in ("efinance", "akshare", "baostock")
    ]
    result = fetch_market_snapshot(adapters=adapters)
    assert result["status"] == "error"
    assert result["tool"] == "fetch_market_snapshot"
    assert [a["source"] for a in result["attempted_sources"]] == [
        "efinance",
        "akshare",
        "baostock",
    ]
    assert all(a["attempts"] == 3 for a in result["attempted_sources"])  # 每源最多重试 2 次
    assert "rows_upserted" not in result  # 不返回任何疑似成果
    assert _snapshot_count(pg_test, "20240105") == 0


def test_fetch_fallback_to_next_source(pg_test):
    _init(pg_test)
    ef = FakeSnapshotAdapter("efinance", fail_times=99)
    ak = FakeSnapshotAdapter("akshare", trade_date="20240105")
    result = fetch_market_snapshot(adapters=[ef, ak])
    assert result["status"] == "ok"
    assert result["source"] == "akshare"
    assert [a["source"] for a in result["attempted_sources"]] == ["efinance"]
    assert _snapshot_count(pg_test, "20240105") == len(SNAPSHOT_ROWS)


def test_fetch_rejects_bad_date_param(pg_test):
    _init(pg_test)
    ef = FakeSnapshotAdapter("efinance")
    result = fetch_market_snapshot(trade_date="2024-13-45", adapters=[ef])
    assert result["status"] == "error"
    assert ef.calls == 0  # 参数错误时不触发抓取
