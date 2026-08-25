"""fetch_daily 接缝测试：fake 适配器 + 真实 PG 测试库（issue #3）。

覆盖：参数校验、自描述 JSON 契约、头尾分段补抓、fallback、全失败报错、upsert 幂等。
"""

import psycopg2

from qstock_mcp.adapters import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import select_daily, upsert_daily
from qstock_mcp.tools_daily import fetch_daily

from fakes import FakeAdapter, weekday_rows


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _rows_in_db(pg_test, code="600519"):
    conn = psycopg2.connect(pg_test)
    rows = select_daily(conn, code, "qfq", "20000101", "20991231")
    conn.close()
    return rows


def test_missing_pg_dsn_returns_error(monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    result = fetch_daily("600519", start="20240101", end="20240131", adapters=[FakeAdapter("ef")])
    assert result["status"] == "error"
    assert "PG_DSN" in result["error"]


def test_params_required(pg_test):
    result = fetch_daily("600519", adapters=[FakeAdapter("ef")])
    assert result["status"] == "error"


def test_fetch_into_empty_db(pg_test):
    _init(pg_test)
    ef = FakeAdapter("efinance")
    result = fetch_daily("600519", start="20240101", end="20240110", adapters=[ef])
    assert result["status"] == "ok"
    assert result["tool"] == "fetch_daily"
    assert result["params"] == {
        "stock_code": "600519",
        "adj": "qfq",
        "days": None,
        "start": "20240101",
        "end": "20240110",
    }
    assert result["range"] == {"start": "20240101", "end": "20240110"}
    assert result["segments"] == [
        {"start": "20240101", "end": "20240110", "source": "efinance", "rows": 8}
    ]
    assert result["rows_upserted"] == 8
    assert len(_rows_in_db(pg_test)) == 8


def test_fetch_fallback_records_actual_source(pg_test):
    _init(pg_test)
    ef = FakeAdapter("efinance", fail_times=99)
    ak = FakeAdapter("akshare")
    result = fetch_daily("600519", start="20240101", end="20240110", adapters=[ef, ak])
    assert result["status"] == "ok"
    assert result["segments"][0]["source"] == "akshare"
    assert {r["source"] for r in _rows_in_db(pg_test)} == {"akshare"}


def test_fetch_only_fills_head_and_tail_gaps(pg_test):
    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    upsert_daily(conn, "600519", "qfq", weekday_rows("20240108", "20240112"), "efinance")
    conn.close()
    ef = FakeAdapter("efinance")
    result = fetch_daily("600519", start="20240101", end="20240119", adapters=[ef])
    assert result["status"] == "ok"
    # 只补头（01-01~01-07）尾（01-13~01-19）两段，已有中段不重抓
    assert [(c["start"], c["end"]) for c in ef.calls] == [
        ("20240101", "20240107"),
        ("20240113", "20240119"),
    ]
    assert len(_rows_in_db(pg_test)) == len(weekday_rows("20240101", "20240119"))


def test_fetch_all_sources_failed(pg_test):
    _init(pg_test)
    adapters = [FakeAdapter(n, fail_times=99) for n in ("efinance", "akshare", "baostock")]
    result = fetch_daily("600519", start="20240101", end="20240110", adapters=adapters)
    assert result["status"] == "error"
    assert [a["source"] for a in result["attempted_sources"]] == [
        "efinance",
        "akshare",
        "baostock",
    ]
    assert result["failed_segment"] == {"start": "20240101", "end": "20240110"}
    assert _rows_in_db(pg_test) == []  # 不落任何数据，更不伪造


def test_fetch_is_idempotent(pg_test):
    _init(pg_test)
    ef = FakeAdapter("efinance")
    kwargs = dict(start="20240101", end="20240110", adapters=[ef])
    first = fetch_daily("600519", **kwargs)
    second = fetch_daily("600519", **kwargs)
    assert first["status"] == second["status"] == "ok"
    assert len(_rows_in_db(pg_test)) == 8  # 重复调用不产生重复行
    assert len(ef.calls) == 1  # 第二次无缺口，不触发抓取
    assert second["segments"] == []


def test_fetch_with_days_param(pg_test):
    _init(pg_test)
    ef = FakeAdapter("efinance")
    result = fetch_daily("600519", days=5, adapters=[ef])
    assert result["status"] == "ok"
    assert result["params"]["days"] == 5
    assert result["data_range"] is not None  # 数据区间回显
    assert len(_rows_in_db(pg_test)) >= 5


def test_fetch_error_reports_healed_segments(pg_test):
    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    upsert_daily(conn, "600519", "qfq", weekday_rows("20240108", "20240112"), "efinance")
    conn.close()

    # 头部抓取成功、尾部全失败：错误里要带已补的分段，不能静默丢弃
    class HeadOnlyAdapter:
        name = "headonly"

        def fetch_daily(self, stock_code, start, end, adj="qfq"):
            if start == "20240101":
                return weekday_rows(start, end)
            raise FetchError("tail boom")

    result = fetch_daily(
        "600519", start="20240101", end="20240119", adapters=[HeadOnlyAdapter()]
    )
    assert result["status"] == "error"
    assert result["failed_segment"] == {"start": "20240113", "end": "20240119"}
    assert result["healed"] == [
        {"start": "20240101", "end": "20240107", "source": "headonly", "rows": 5}
    ]
