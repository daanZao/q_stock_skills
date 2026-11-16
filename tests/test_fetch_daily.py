"""fetch_daily 接缝测试：fake 适配器 + 真实 PG 测试库（issue #3）。

覆盖：参数校验、自描述 JSON 契约、头尾分段补抓、fallback、全失败报错、upsert 幂等。
"""

import psycopg2

from qstock_mcp.adapters import FetchError
from qstock_mcp.dates import is_trading_day
from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import select_daily, upsert_daily
from qstock_mcp.tools_daily import fetch_daily

from fakes import FakeAdjustmentAdapter, FakeAdapter, trading_rows, weekday_rows


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _seed_baseline(pg_test, code="600519"):
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stock_factor_state (stock_code, baseline_factor, factor_latest,"
            " latest_trade_date, rebuild_pending) VALUES (%s, 1.0, 1.0, '20231229', false)"
            " ON CONFLICT (stock_code) DO UPDATE SET baseline_factor=1.0,"
            " factor_latest=1.0, rebuild_pending=false",
            (code,),
        )
    conn.commit()
    conn.close()


def _rows_in_db(pg_test, code="600519"):
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, close, source FROM stock_daily_raw"
            " WHERE stock_code=%s ORDER BY trade_date",
            (code,),
        )
        rows = [
            {"trade_date": day, "close": close, "source": source}
            for day, close, source in cur.fetchall()
        ]
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
    adapter = FakeAdjustmentAdapter()
    result = fetch_daily("600519", start="20240102", end="20240105", adapters=[adapter])
    assert result["status"] == "partial_error"
    assert result["adjust"]["coverage"] == "partial"
    assert result["tool"] == "fetch_daily"
    assert result["params"] == {
        "stock_code": "600519",
        "adj": "qfq",
        "days": None,
        "start": "20240102",
        "end": "20240105",
    }
    assert result["range"] == {"start": "20240102", "end": "20240105"}
    assert result["segments"] == [
        {"start": "20240102", "end": "20240105", "source": "baostock", "rows": 4}
    ]
    assert result["rows_upserted"] == 4
    assert [call["adj"] for call in adapter.calls] == ["none"]
    assert adapter.factor_calls == ["20240102", "20240103", "20240104", "20240105"]
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM stock_daily")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM stock_daily_raw")
        assert cur.fetchone()[0] == 4
        cur.execute("SELECT count(*) FROM stock_daily_hfq")
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT baseline_factor, factor_latest, rebuild_pending"
            " FROM stock_factor_state WHERE stock_code='600519'"
        )
        assert cur.fetchone() == (None, None, True)
    conn.close()


def test_fetch_fallback_records_actual_source(pg_test):
    _init(pg_test)
    bs = FakeAdjustmentAdapter(fail_times=99)
    sn = FakeAdapter("sina")
    result = fetch_daily("600519", start="20240102", end="20240105", adapters=[bs, sn])
    assert result["status"] == "partial_error"
    assert result["segments"][0]["source"] == "sina"
    assert {r["source"] for r in _rows_in_db(pg_test)} == {"sina"}


def test_fetch_only_fills_head_and_tail_gaps(pg_test):
    from qstock_mcp.adjustment import rebuild_stock

    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    rebuild_stock(
        conn, "600519", weekday_rows("20240108", "20240112"), [],
        baseline_factor=1.0,
    )
    conn.close()
    adapter = FakeAdjustmentAdapter()
    result = fetch_daily("600519", start="20240101", end="20240119", adapters=[adapter])
    assert result["status"] == "ok"
    assert [(c["start"], c["end"]) for c in adapter.calls] == [
        ("20240101", "20240107"),
        ("20240113", "20240119"),
    ]
    assert len(_rows_in_db(pg_test)) == len(trading_rows("20240101", "20240119"))


def test_fetch_all_sources_failed(pg_test):
    _init(pg_test)
    adapters = [
        FakeAdjustmentAdapter(fail_times=99),
        FakeAdapter("tencent", fail_times=99),
        FakeAdapter("sina", fail_times=99),
    ]
    result = fetch_daily("600519", start="20240102", end="20240105", adapters=adapters)
    assert result["status"] == "error"
    assert [a["source"] for a in result["attempted_sources"]] == [
        "baostock",
        "tencent",
        "sina",
    ]
    assert result["failed_segment"] == {"start": "20240102", "end": "20240105"}
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM stock_daily_raw")
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT status FROM collection_log WHERE section='stock_daily_raw'"
        )
        assert cur.fetchone()[0] == "error"
    conn.close()


def test_fetch_baostock_locked_returns_structured_error(pg_test):
    from qstock_mcp.baostock_errors import BaostockLocked

    class LockedAdapter:
        name = "baostock"

        def fetch_daily(self, *args):
            raise BaostockLocked("未持有有效的 baostock 租约")

    _init(pg_test)
    result = fetch_daily(
        "600519",
        start="20240101",
        end="20240110",
        adapters=[LockedAdapter()],
    )

    assert result["status"] == "error"
    assert result["error_kind"] == "baostock_locked"
    assert _rows_in_db(pg_test) == []


def test_fetch_force_refreshes_whole_window_and_preserves_outside(pg_test):
    from qstock_mcp.adjustment import rebuild_stock

    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    rebuild_stock(
        conn, "600519", weekday_rows("20231229", "20240112", close=99.0), [],
        baseline_factor=1.0,
    )
    conn.close()
    adapter = FakeAdjustmentAdapter()

    result = fetch_daily(
        "600519", start="20240101", end="20240110", adapters=[adapter], force=True
    )

    assert result["status"] == "ok"
    assert result["params"]["force"] is True
    assert [(c["start"], c["end"]) for c in adapter.calls] == [("20240101", "20240110")]
    rows = _rows_in_db(pg_test)
    by_date = {r["trade_date"]: r for r in rows}
    assert all(
        by_date[r["trade_date"]]["close"] == 10.0
        for r in trading_rows("20240101", "20240110")
    )
    assert by_date["20231229"]["close"] == 99.0
    assert by_date["20240111"]["close"] == 99.0
    assert by_date["20240112"]["close"] == 99.0


def test_fetch_is_idempotent(pg_test):
    _init(pg_test)
    _seed_baseline(pg_test)
    adapter = FakeAdjustmentAdapter()
    kwargs = dict(start="20240102", end="20240105", adapters=[adapter])
    first = fetch_daily("600519", **kwargs)
    second = fetch_daily("600519", **kwargs)
    assert first["status"] == second["status"] == "ok"
    assert len(adapter.calls) == 1
    assert len(adapter.factor_calls) == 4
    assert second["segments"] == []
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        for table in ("stock_daily_raw", "stock_daily_hfq"):
            cur.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 4
    conn.close()


def test_fetch_with_days_param(pg_test):
    _init(pg_test)
    _seed_baseline(pg_test)
    ef = FakeAdjustmentAdapter()
    result = fetch_daily("600519", days=5, adapters=[ef])
    assert result["status"] == "ok"
    assert result["params"]["days"] == 5
    assert result["data_range"] is not None
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
    _seed_baseline(pg_test)
    ef = FakeAdjustmentAdapter()
    result = fetch_daily("600519", days=5, adapters=[ef])
    assert result["status"] == "ok"
    assert result["range"] == {"start": "20260928", "end": "20261014"}
    assert result["segments"] == [
        {"start": "20260928", "end": "20261014", "source": "baostock", "rows": 8}
    ]
    assert result["data_range"] == {"start": "20260928", "end": "20261014"}


def test_fetch_with_days_partial_db_no_empty_head_segment(pg_test, monkeypatch):
    """工单 P2-2 场景：库已有数据且最早日即换算区间内的首个交易日，
    start 对齐后与库内最早日相接，不再产生全非交易日的空抓分段。"""
    from qstock_mcp.adjustment import rebuild_stock

    _init(pg_test)
    _pin_today(monkeypatch, "20261014")
    conn = psycopg2.connect(pg_test)
    rebuild_stock(
        conn, "600519", trading_rows("20260928", "20261014"), [],
        baseline_factor=1.0,
    )
    conn.close()
    ef = FakeAdjustmentAdapter()
    result = fetch_daily("600519", days=5, adapters=[ef])
    assert result["status"] == "ok"
    assert result["range"]["start"] == "20260928"
    assert result["segments"] == []
    assert ef.calls == []
    assert result["rows_upserted"] == 0


def test_fetch_with_days_head_gap_starts_on_trading_day(pg_test, monkeypatch):
    """库有部分数据且存在真实头部缺口：分段起点落在交易日，且分段非空。"""
    from qstock_mcp.adjustment import rebuild_stock

    _init(pg_test)
    _pin_today(monkeypatch, "20261014")
    conn = psycopg2.connect(pg_test)
    rebuild_stock(
        conn, "600519", trading_rows("20261008", "20261014"), [],
        baseline_factor=1.0,
    )
    conn.close()
    ef = FakeAdjustmentAdapter()
    result = fetch_daily("600519", days=5, adapters=[ef])
    assert result["status"] == "ok"
    assert result["segments"] == [
        {"start": "20260928", "end": "20261007", "source": "baostock", "rows": 3}
    ]
    assert all(is_trading_day(s["start"]) for s in result["segments"])


def test_fetch_error_reports_healed_segments(pg_test):
    from qstock_mcp.adjustment import rebuild_stock

    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    rebuild_stock(
        conn, "600519", weekday_rows("20240108", "20240112"), [],
        baseline_factor=1.0,
    )
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
    assert len(_rows_in_db(pg_test)) == 10


def test_fetch_daily_defaults_to_adjustment_chain(pg_test, monkeypatch):
    _init(pg_test)
    _seed_baseline(pg_test)
    baostock = FakeAdjustmentAdapter()
    monkeypatch.setattr(
        "qstock_mcp.tools_daily.default_adjustment_adapters", lambda: [baostock]
    )
    result = fetch_daily("600519", start="20240102", end="20240105")
    assert result["status"] == "ok"
    assert result["segments"] == [
        {"start": "20240102", "end": "20240105", "source": "baostock", "rows": 4}
    ]


def test_fetch_daily_no_fallback_no_note(pg_test):
    _init(pg_test)
    _seed_baseline(pg_test)
    result = fetch_daily(
        "600519", start="20240102", end="20240105", adapters=[FakeAdjustmentAdapter()]
    )
    assert result["status"] == "ok"
    assert "note" not in result
    assert "adjust_fallback" not in result["segments"][0]


def test_factor_unknown_preserves_raw_without_provisional_derivation(pg_test):
    class FactorUnavailable(FakeAdjustmentAdapter):
        def fetch_daily_adjust_factor(self, trade_date):
            self.factor_calls.append(trade_date)
            raise FetchError("factor unavailable", kind="connection")

    _init(pg_test)
    adapter = FactorUnavailable()
    result = fetch_daily(
        "600519", start="20240102", end="20240105", adapters=[adapter]
    )

    assert result["status"] == "partial_error"
    assert result["adjust"]["coverage"] == "partial"
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM stock_daily_raw")
        assert cur.fetchone()[0] == 4
        cur.execute("SELECT count(*) FROM stock_daily_hfq")
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT baseline_factor, factor_latest, rebuild_pending"
            " FROM stock_factor_state WHERE stock_code='600519'"
        )
        assert cur.fetchone() == (None, None, True)
        cur.execute(
            "SELECT status, error_kind FROM collection_log"
            " WHERE section='stock_adjust_factor' AND trade_date='20240102'"
            " ORDER BY id DESC LIMIT 1"
        )
        assert cur.fetchone() == ("error", "upstream_unreachable")
    conn.close()


def test_alternative_raw_source_remains_partial_without_authoritative_pctchg(pg_test):
    _init(pg_test)
    baostock = FakeAdjustmentAdapter(fail_times=99)
    tencent = FakeAdapter("tencent")

    result = fetch_daily(
        "600519",
        start="20240102",
        end="20240105",
        adapters=[baostock, tencent],
    )

    assert result["status"] == "partial_error"
    assert result["adjust"]["coverage"] == "partial"
    assert result["segments"][0]["source"] == "tencent"
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT source FROM stock_daily_raw WHERE stock_code='600519'"
        )
        assert cur.fetchall() == [("tencent",)]
        cur.execute(
            "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='600519'"
        )
        assert cur.fetchone()[0] is True
    conn.close()


def test_fetch_raw_middle_gap_does_not_report_complete(pg_test):
    from qstock_mcp.adjustment import rebuild_stock

    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    rebuild_stock(
        conn,
        "600519",
        trading_rows("20240102", "20240102") + trading_rows("20240105", "20240105"),
        [],
        baseline_factor=1.0,
    )
    conn.close()

    result = fetch_daily(
        "600519",
        start="20240102",
        end="20240105",
        adapters=[FakeAdjustmentAdapter()],
    )

    assert result["status"] == "ok"
    assert result["adjust"]["coverage"] == "partial"


def test_factor_day_response_is_shared_and_throttled(pg_test, monkeypatch):
    _init(pg_test)
    _seed_baseline(pg_test)
    adapter = FakeAdjustmentAdapter(
        factor_events=[{
            "stock_code": "000001",
            "ex_date": "20240102",
            "factor_cum": 2.0,
        }]
    )
    sleeps = []
    monkeypatch.setattr(
        "qstock_mcp.adjustment_pipeline.sleep", lambda seconds: sleeps.append(seconds)
    )
    monkeypatch.setattr("qstock_mcp.adjustment_pipeline.monotonic", lambda: 0.0)

    first = fetch_daily(
        "000001", start="20240102", end="20240102", adapters=[adapter]
    )
    second = fetch_daily(
        "600519", start="20240102", end="20240102", adapters=[adapter]
    )
    third = fetch_daily(
        "000001", start="20240103", end="20240103", adapters=[adapter]
    )

    assert first["status"] != "error"
    assert second["status"] == "ok"
    assert third["status"] != "error"
    assert adapter.factor_calls == ["20240102", "20240103"]
    assert sleeps == [1.5, 1.5, 1.5, 1.5]
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT stock_code FROM stock_adjust_factor")
        assert cur.fetchall() == [("000001",)]
    conn.close()


def test_factor_recovery_does_not_clear_alternative_raw_pending(pg_test):
    class RecoveringFactor(FakeAdjustmentAdapter):
        def fetch_daily_adjust_factor(self, trade_date):
            self.factor_calls.append(trade_date)
            if len(self.factor_calls) == 1:
                raise FetchError("temporary factor failure", kind="connection")
            return []

    _init(pg_test)
    baostock = RecoveringFactor(fail_times=99)
    tencent = FakeAdapter("tencent")
    kwargs = dict(
        start="20240102", end="20240105", adapters=[baostock, tencent]
    )

    first = fetch_daily("600519", **kwargs)
    second = fetch_daily("600519", **kwargs)

    assert first["status"] == "partial_error"
    assert second["status"] == "partial_error"
    assert second["adjust"]["coverage"] == "partial"
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='600519'"
        )
        assert cur.fetchone()[0] is True
    conn.close()


def test_daily_factor_mismatch_sets_pending_and_repair(pg_test):
    _init(pg_test)
    _seed_baseline(pg_test)
    adapter = FakeAdjustmentAdapter(
        rows=trading_rows("20240102", "20240103"),
        factor_events=[{
            "stock_code": "600519", "ex_date": "20240103", "factor_cum": 2.0
        }],
    )

    result = fetch_daily(
        "600519", start="20240102", end="20240103", adapters=[adapter]
    )

    assert result["status"] == "partial_error"
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='600519'"
        )
        assert cur.fetchone()[0] is True
        cur.execute(
            "SELECT status, error_kind FROM collection_log"
            " WHERE section='stock_factor_repair' ORDER BY id DESC LIMIT 1"
        )
        assert cur.fetchone() == ("error", "factor_mismatch")
    conn.close()


def test_daily_narrow_rebuild_preserves_existing_pending(pg_test):
    _init(pg_test)
    _seed_baseline(pg_test)
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE stock_factor_state SET rebuild_pending=true WHERE stock_code='600519'"
        )
    conn.commit()
    conn.close()

    result = fetch_daily(
        "600519",
        start="20240102",
        end="20240105",
        adapters=[FakeAdjustmentAdapter()],
    )

    assert result["status"] == "partial_error"
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='600519'"
        )
        assert cur.fetchone()[0] is True
    conn.close()


def test_baostock_raw_upgrades_all_alternative_rows_and_clears_t0_pending(pg_test):
    _init(pg_test)
    _seed_baseline(pg_test)
    baostock = FakeAdjustmentAdapter(fail_times=99)
    tencent = FakeAdapter("tencent")
    kwargs = dict(
        start="20240102", end="20240105", adapters=[baostock, tencent]
    )

    first = fetch_daily("600519", **kwargs)
    baostock._fail_times = 0
    second = fetch_daily("600519", **kwargs)

    assert first["status"] == "partial_error"
    assert second["status"] == "ok"
    assert second["segments"][0]["source"] == "baostock"
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT source FROM stock_daily_raw WHERE stock_code='600519'"
        )
        assert cur.fetchall() == [("baostock",)]
        cur.execute(
            "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='600519'"
        )
        assert cur.fetchone()[0] is False
    conn.close()


def test_fetch_pre_window_returns_structured_error_without_upstream(pg_test):
    _init(pg_test)
    adapter = FakeAdjustmentAdapter()

    result = fetch_daily(
        "600519", start="20221230", end="20221230", adapters=[adapter]
    )

    assert result["status"] == "error"
    assert result["error_kind"] == "invalid_range"
    assert adapter.calls == []
    assert adapter.factor_calls == []


def test_upgrade_preserves_pending_with_unrelated_repair_reason(pg_test):
    from qstock_mcp.ledger import record_collection

    _init(pg_test)
    _seed_baseline(pg_test)
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE stock_factor_state SET rebuild_pending=true WHERE stock_code='600519'"
        )
    conn.commit()
    record_collection(
        conn,
        trade_date="20240102",
        section="stock_factor_repair",
        status="error",
        source="adj-backfill",
        error_kind="factor_mismatch",
        detail="adj-backfill repair 600519 factor_mismatch: test",
    )
    conn.close()
    baostock = FakeAdjustmentAdapter(fail_times=99)
    tencent = FakeAdapter("tencent")
    kwargs = dict(
        start="20240102", end="20240105", adapters=[baostock, tencent]
    )

    fetch_daily("600519", **kwargs)
    baostock._fail_times = 0
    result = fetch_daily("600519", **kwargs)

    assert result["status"] == "partial_error"
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='600519'"
        )
        assert cur.fetchone()[0] is True
    conn.close()
