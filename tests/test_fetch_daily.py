"""fetch_daily 接缝测试：fake 适配器 + 真实 PG 测试库（issue #3）。

覆盖：参数校验、自描述 JSON 契约、头尾分段补抓、fallback、全失败报错、upsert 幂等。
"""

import psycopg2

from qstock_mcp.adapters import FetchError
from qstock_mcp.dates import is_trading_day
from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import select_daily, upsert_daily
from qstock_mcp.tools_daily import fetch_daily

from fakes import FakeAdapter, trading_rows, weekday_rows


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


# ---------------------------------------------------------------- issue #51 交易日对齐


def _pin_today(monkeypatch, ymd: str):
    """固定 dates 模块的"今天"（today_str 为东八区当日口径）。"""
    from qstock_mcp import dates

    monkeypatch.setattr(dates, "today_str", lambda now=None: ymd)


def test_fetch_with_days_empty_db_start_aligned(pg_test, monkeypatch):
    """库空：days 换算落点 20260927（周日），start 向前对齐到交易日 20260928。"""
    _init(pg_test)
    _pin_today(monkeypatch, "20261014")
    ef = FakeAdapter("efinance", rows_fn=trading_rows)
    result = fetch_daily("600519", days=5, adapters=[ef])
    assert result["status"] == "ok"
    assert result["range"] == {"start": "20260928", "end": "20261014"}
    assert result["segments"] == [
        {"start": "20260928", "end": "20261014", "source": "efinance", "rows": 8}
    ]
    assert result["data_range"] == {"start": "20260928", "end": "20261014"}


def test_fetch_with_days_partial_db_no_empty_head_segment(pg_test, monkeypatch):
    """工单 P2-2 场景：库已有数据且最早日即换算区间内的首个交易日，
    start 对齐后与库内最早日相接，不再产生全非交易日的空抓分段。"""
    _init(pg_test)
    _pin_today(monkeypatch, "20261014")
    conn = psycopg2.connect(pg_test)
    upsert_daily(conn, "600519", "qfq", trading_rows("20260928", "20261014"), "baostock")
    conn.close()
    ef = FakeAdapter("efinance", rows_fn=trading_rows)
    result = fetch_daily("600519", days=5, adapters=[ef])
    assert result["status"] == "ok"
    assert result["range"]["start"] == "20260928"
    assert result["segments"] == []
    assert ef.calls == []  # 零上游请求
    assert result["rows_upserted"] == 0


def test_fetch_with_days_head_gap_starts_on_trading_day(pg_test, monkeypatch):
    """库有部分数据且存在真实头部缺口：分段起点落在交易日，且分段非空。"""
    _init(pg_test)
    _pin_today(monkeypatch, "20261014")
    conn = psycopg2.connect(pg_test)
    upsert_daily(conn, "600519", "qfq", trading_rows("20261008", "20261014"), "baostock")
    conn.close()
    ef = FakeAdapter("efinance", rows_fn=trading_rows)
    result = fetch_daily("600519", days=5, adapters=[ef])
    assert result["status"] == "ok"
    assert result["segments"] == [
        {"start": "20260928", "end": "20261007", "source": "efinance", "rows": 3}
    ]
    assert all(is_trading_day(s["start"]) for s in result["segments"])


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


def test_fetch_daily_defaults_to_daily_chain(pg_test, monkeypatch):
    """未注入 adapters 时走 腾讯→新浪→baostock 日线链（issue #112，东财退出）。"""
    _init(pg_test)
    tencent = FakeAdapter("tencent")
    monkeypatch.setattr(
        "qstock_mcp.tools_daily.default_daily_adapters", lambda: [tencent]
    )
    result = fetch_daily("600519", start="20240101", end="20240110")
    assert result["status"] == "ok"
    assert result["segments"] == [
        {"start": "20240101", "end": "20240110", "source": "tencent", "rows": 8}
    ]


def test_fetch_daily_adjust_fallback_visible(pg_test):
    """复权回落可见化（issue #112）：segment 带标记、响应带 note、台账 detail 留痕。"""
    _init(pg_test)
    tencent = FakeAdapter("tencent")
    tencent.adjust_fallback = True
    result = fetch_daily("920047", start="20240101", end="20240110", adapters=[tencent])
    assert result["status"] == "ok"
    assert result["segments"][0]["adjust_fallback"] is True
    assert "复权" in result["note"]
    conn = psycopg2.connect(pg_test)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail FROM collection_log WHERE section = 'stock_daily'"
            )
            details = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    assert any("复权回落" in d for d in details)


def test_fetch_daily_no_fallback_no_note(pg_test):
    """无回落时响应不带 note、segment 不带标记（不误报）。"""
    _init(pg_test)
    result = fetch_daily(
        "600519", start="20240101", end="20240110", adapters=[FakeAdapter("tencent")]
    )
    assert result["status"] == "ok"
    assert "note" not in result
    assert "adjust_fallback" not in result["segments"][0]
