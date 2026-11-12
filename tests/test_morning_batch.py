"""次晨批测试：raw + 逐日累计因子 + hfq/anchor + 指数与申万估值补采。

fetch_morning_batch 使用 fake 适配器与真实 PG 测试库覆盖完整 morning、
pctChg 降级、双路径对账、raw 缺口、无效 close、pending 门控与增量重建；
既有 legacy 日线、指数和申万部分保持独立成败并逐部分落采集台账。
"""

import json
from datetime import datetime, timezone, timedelta

import psycopg2
import pytest

from qstock_mcp import scheduler
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import select_daily, select_index_daily, upsert_index_daily
from qstock_mcp.tools_morning import fetch_morning_batch
from fakes import FakeInitAdapter, FakeSwBoardsAdapter

_TZ8 = timezone(timedelta(hours=8))

TARGET = "20261016"  # 周五；2026-10-19（周一）开盘前跑次晨批的目标日
NOW_MONDAY_AM = datetime(2026, 10, 19, 8, 30, tzinfo=_TZ8)


def _sw(fail=(), analysis=None):
    """申万估值 part 的 fake 注入：默认返回两条估值行（trade_date 盖戳为目标日）。"""
    return [FakeSwBoardsAdapter(analysis=analysis, fail=fail)]


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


@pytest.fixture
def pg_ready(pg_test):
    _init(pg_test)
    return pg_test


class FakeMorningAdapter:
    """一把抓全市场日线与逐日因子的 fake 源。"""

    def __init__(self, name="baostock", rows=None, exc=None, events=None, factor_exc=None):
        self.name = name
        self._rows = rows
        self._exc = exc
        self._events = events
        self._factor_exc = factor_exc
        self.calls = []
        self.factor_calls = []

    def fetch_astock_daily(self, trade_date):
        self.calls.append(trade_date)
        if self._exc is not None:
            raise self._exc
        return list(self._rows or [])

    def fetch_daily_adjust_factor(self, trade_date):
        self.factor_calls.append(trade_date)
        if self._factor_exc is not None:
            raise self._factor_exc
        return list(self._events or [])


def _morning_rows(target=TARGET):
    return [
        {
            "stock_code": "600519", "trade_date": target,
            "open": 1700.0, "high": 1720.0, "low": 1690.0, "close": 1710.0,
            "volume": 3000000, "amount": 513000.0,
            "amplitude": None, "change_percent": 0.59, "change_amount": None,
            "turnover_rate": 0.24, "tradestatus": 1, "adj": "none",
        },
        {
            "stock_code": "002813", "trade_date": target,
            "open": 26.38, "high": 26.38, "low": 26.38, "close": 26.38,
            "volume": None, "amount": None,
            "amplitude": None, "change_percent": None, "change_amount": None,
            "turnover_rate": None, "tradestatus": 0, "adj": "none",
        },
    ]


def _stock_daily_rows(dsn, stock_code):
    conn = psycopg2.connect(dsn)
    try:
        return select_daily(conn, stock_code, "none", "20000101", "20991231")
    finally:
        conn.close()


def _ledger(dsn, section=None):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            sql = ("SELECT trade_date, section, status, source, rows, error_kind,"
                   " detail FROM collection_log")
            args = ()
            if section:
                sql += " WHERE section = %s"
                args = (section,)
            cur.execute(sql + " ORDER BY id", args)
            return cur.fetchall()
    finally:
        conn.close()


class TestStockDailyPart:
    def test_baostock_locked_returns_structured_error(self, pg_ready):
        from qstock_mcp.baostock_errors import BaostockLocked

        adapter = FakeMorningAdapter(
            exc=BaostockLocked("未持有有效的 baostock 租约")
        )
        out = fetch_morning_batch(
            trade_date=TARGET,
            adapter=adapter,
            adapters=[FakeInitAdapter("fake")],
            sw_adapters=_sw(),
            now=NOW_MONDAY_AM,
        )

        assert out["status"] == "error"
        assert out["error_kind"] == "baostock_locked"
        assert adapter.calls == [TARGET]

    def test_full_market_rows_land(self, pg_ready):
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=_morning_rows()),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)
        assert out["status"] == "ok"
        part = out["parts"]["stock_daily"]
        assert part["status"] == "ok"
        assert part["rows"] == 2
        assert part["source"] == "baostock"

        rows = _stock_daily_rows(pg_ready, "600519")
        assert len(rows) == 1
        r = rows[0]
        assert r["tradestatus"] == 1
        assert r["turnover_rate"] == 0.24
        assert r["volume"] == 3000000  # 股口径透传
        assert r["amount"] == 513000.0  # 万元
        assert r["source"] == "baostock"
        suspended = _stock_daily_rows(pg_ready, "002813")[0]
        assert suspended["tradestatus"] == 0  # 停牌权威标注落库

    def test_new_mode_keeps_fact_ingestion_without_legacy_double_write(
        self, pg_ready, monkeypatch, tmp_path
    ):
        from qstock_mcp.adjustment_switch import run_adjustment_switch

        assert run_adjustment_switch(
            "cutover", report_dir=str(tmp_path / "cutover")
        )["status"] == "ok"
        monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")
        try:
            out = fetch_morning_batch(
                trade_date=TARGET,
                adapter=FakeMorningAdapter(rows=_morning_rows()),
                adapters=[FakeInitAdapter("fake")],
                sw_adapters=_sw(),
                now=NOW_MONDAY_AM,
            )

            assert out["status"] == "ok"
            part = out["parts"]["stock_daily"]
            assert part["rows"] == 2
            assert part["legacy_rows"] == 0
            conn = psycopg2.connect(pg_ready)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM stock_daily_legacy")
                assert cur.fetchone()[0] == 0
                cur.execute("SELECT COUNT(*) FROM stock_daily_raw")
                assert cur.fetchone()[0] == 2
                cur.execute("SELECT COUNT(*) FROM stock_factor_state")
                assert cur.fetchone()[0] == 2
            conn.close()
        finally:
            run_adjustment_switch("rollback", report_dir=str(tmp_path / "rollback"))

    def test_ledger_recorded(self, pg_ready):
        fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=_morning_rows()),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)
        rows = _ledger(pg_ready, section="stock_daily")
        assert len(rows) == 1
        assert rows[0][0] == TARGET
        assert rows[0][2] == "ok"
        assert rows[0][3] == "baostock"
        assert rows[0][4] == 2

    def test_rerun_idempotent(self, pg_ready):
        for _ in range(2):
            fetch_morning_batch(
                trade_date=TARGET,
                adapter=FakeMorningAdapter(rows=_morning_rows()),
                adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)
        assert len(_stock_daily_rows(pg_ready, "600519")) == 1  # 业务键幂等覆盖

    def test_baostock_unreachable_three_state(self, pg_ready):
        """baostock 不可达：三态契约 error_kind + 台账 error 行，指数补漏不受影响。"""
        exc = FetchError("baostock 登录失败")
        exc.__cause__ = ConnectionRefusedError("connection refused")
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(exc=exc),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)
        part = out["parts"]["stock_daily"]
        assert part["status"] == "error"
        assert part["error_kind"] == "upstream_unreachable"
        assert out["status"] == "partial_error"  # 指数部分照常
        rows = _ledger(pg_ready, section="stock_daily")
        assert rows[0][2] == "error"
        assert rows[0][5] == "upstream_unreachable"
        assert _stock_daily_rows(pg_ready, "600519") == []

    def test_raw_and_factor_failures_remain_both_errors(self, pg_ready):
        out = fetch_morning_batch(
            trade_date=TARGET,
            adapter=FakeMorningAdapter(
                exc=FetchError("raw unavailable"),
                factor_exc=FetchError("factor unavailable"),
            ),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert out["status"] == "partial_error"
        assert out["parts"]["stock_daily"]["status"] == "error"
        factor = out["parts"]["stock_adjust_factor"]
        assert factor["status"] == "error"
        assert factor["error_kind"] == "pctchg_derivation_failed"
        assert out["messages"] == ["数据处理完成"]
        assert all(
            row[2] == "error" for row in _ledger(pg_ready, "stock_adjust_factor")
        )

    def test_empty_result_not_error(self, pg_ready):
        """非交易日/上游确认无数据：empty 落台账，非故障。"""
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=[]),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)
        assert out["parts"]["stock_daily"]["status"] == "empty"
        assert _ledger(pg_ready, section="stock_daily")[0][2] == "empty"


class TestAdjustmentMorning:
    def test_raw_no_event_rows_initialize_hfq_and_anchor(self, pg_ready):
        adapter = FakeMorningAdapter(rows=_morning_rows(), events=[])
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=adapter,
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert adapter.calls == [TARGET]
        assert adapter.factor_calls == [TARGET]
        assert out["status"] == "ok"
        assert out["stage"] == "verified"
        assert out["messages"] == ["数据处理完成", "数据可以使用"]
        assert out["parts"]["stock_adjust_factor"]["status"] == "empty"

        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM stock_daily_raw WHERE trade_date=%s", (TARGET,))
                assert cur.fetchone()[0] == 2
                cur.execute("SELECT COUNT(*) FROM stock_adjust_factor WHERE ex_date=%s", (TARGET,))
                assert cur.fetchone()[0] == 0
                cur.execute(
                    "SELECT baseline_factor, factor_latest, latest_trade_date, rebuild_pending"
                    " FROM stock_factor_state WHERE stock_code='600519'"
                )
                baseline, latest, anchor, pending = cur.fetchone()
                assert float(baseline) == 1.0
                assert float(latest) == 1.0
                assert anchor == TARGET
                assert pending is False
                cur.execute(
                    "SELECT close, factor_cum, tradestatus FROM stock_daily_hfq"
                    " WHERE stock_code='002813' AND trade_date=%s",
                    (TARGET,),
                )
                close, factor, tradestatus = cur.fetchone()
                assert close == 26.38
                assert float(factor) == 1.0
                assert tradestatus == 0
        finally:
            conn.close()

    def test_authoritative_event_rebuilds_from_ex_date(self, pg_ready):
        from qstock_mcp.adjustment import rebuild_stock

        conn = psycopg2.connect(pg_ready)
        try:
            rebuild_stock(
                conn, "688300",
                [{**_morning_rows("20261015")[0], "stock_code": "688300",
                  "open": 136.01, "high": 136.01, "low": 136.01, "close": 136.01}],
                [{"ex_date": "20260610", "factor_cum": 2.965589}],
                baseline_factor=2.965589,
            )
        finally:
            conn.close()
        current = {
            **_morning_rows()[0], "stock_code": "688300",
            "open": 104.62, "high": 105.0, "low": 100.0, "close": 102.70,
            "preclose": 104.62, "change_percent": -1.8352,
        }
        out = fetch_morning_batch(
            trade_date=TARGET,
            adapter=FakeMorningAdapter(
                rows=[current],
                events=[{"stock_code": "688300", "ex_date": TARGET,
                         "factor_cum": 3.855379}],
            ),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert out["status"] == "ok"
        assert out["parts"]["stock_adjust_factor"]["rows"] == 1
        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT factor_cum, source FROM stock_adjust_factor"
                    " WHERE stock_code='688300' AND ex_date=%s",
                    (TARGET,),
                )
                factor, source = cur.fetchone()
                assert float(factor) == 3.855379
                assert source == "baostock-adjust-factor"
                cur.execute(
                    "SELECT close, factor_cum FROM stock_daily_hfq"
                    " WHERE stock_code='688300' AND trade_date=%s",
                    (TARGET,),
                )
                close, normalized = cur.fetchone()
                assert close == pytest.approx(133.51392364215)
                assert float(normalized) == pytest.approx(1.300038204889484)
                cur.execute(
                    "SELECT baseline_factor, factor_latest, rebuild_pending"
                    " FROM stock_factor_state WHERE stock_code='688300'"
                )
                baseline, latest, pending = cur.fetchone()
                assert float(baseline) == pytest.approx(2.965589)
                assert float(latest) == pytest.approx(1.300038204889484)
                assert pending is False
        finally:
            conn.close()

    def test_factor_failure_uses_authoritative_pctchg_fallback(self, pg_ready):
        from qstock_mcp.adjustment import rebuild_stock

        conn = psycopg2.connect(pg_ready)
        try:
            rebuild_stock(
                conn, "688300",
                [{**_morning_rows("20261015")[0], "stock_code": "688300",
                  "open": 136.01, "high": 136.01, "low": 136.01, "close": 136.01}],
                [{"ex_date": "20260610", "factor_cum": 2.965589}],
                baseline_factor=2.965589,
            )
        finally:
            conn.close()
        current = {
            **_morning_rows()[0], "stock_code": "688300",
            "open": 104.62, "high": 105.0, "low": 100.0, "close": 102.70,
            "preclose": 104.62, "change_percent": -1.8352,
        }
        out = fetch_morning_batch(
            trade_date=TARGET,
            adapter=FakeMorningAdapter(
                rows=[current], factor_exc=FetchError("factor unavailable")
            ),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert out["status"] == "ok"
        assert out["stage"] == "verified"
        part = out["parts"]["stock_adjust_factor"]
        assert part["status"] == "ok"
        assert part["mode"] == "baostock-pctchg-derived"
        assert part["rows"] == 1
        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT factor_cum, source FROM stock_adjust_factor"
                    " WHERE stock_code='688300' AND ex_date=%s",
                    (TARGET,),
                )
                factor, source = cur.fetchone()
                assert float(factor) == pytest.approx(3.8553795, abs=1e-6)
                assert source == "baostock-pctchg-derived"
        finally:
            conn.close()

    def test_authoritative_pctchg_mismatch_is_pending_and_not_verified(self, pg_ready):
        from qstock_mcp.adjustment import rebuild_stock

        conn = psycopg2.connect(pg_ready)
        try:
            rebuild_stock(
                conn, "688300",
                [{**_morning_rows("20261015")[0], "stock_code": "688300",
                  "open": 136.01, "high": 136.01, "low": 136.01, "close": 136.01}],
                [{"ex_date": "20260610", "factor_cum": 2.965589}],
                baseline_factor=2.965589,
            )
        finally:
            conn.close()
        current = {
            **_morning_rows()[0], "stock_code": "688300",
            "open": 104.62, "high": 105.0, "low": 100.0, "close": 102.70,
            "preclose": 104.62, "change_percent": -1.8352,
        }
        out = fetch_morning_batch(
            trade_date=TARGET,
            adapter=FakeMorningAdapter(
                rows=[current],
                events=[{"stock_code": "688300", "ex_date": TARGET,
                         "factor_cum": 3.0}],
            ),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert out["status"] == "partial_error"
        assert out["stage"] == "rebuilt"
        assert out["messages"] == ["数据处理完成"]
        assert out["parts"]["stock_adjust_factor"]["error_kind"] == "factor_mismatch"
        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT factor_cum FROM stock_adjust_factor"
                    " WHERE stock_code='688300' AND ex_date=%s",
                    (TARGET,),
                )
                assert float(cur.fetchone()[0]) == 3.0
                cur.execute(
                    "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='688300'"
                )
                assert cur.fetchone()[0] is True
        finally:
            conn.close()
        mismatch = [row for row in _ledger(pg_ready, "stock_adjust_factor")
                    if row[5] == "factor_mismatch"]
        assert len(mismatch) == 1

    def test_pctchg_fallback_missing_evidence_is_unresolved(self, pg_ready):
        from qstock_mcp.adjustment import rebuild_stock

        conn = psycopg2.connect(pg_ready)
        try:
            rebuild_stock(
                conn, "688300",
                [{**_morning_rows("20261015")[0], "stock_code": "688300",
                  "open": 136.01, "high": 136.01, "low": 136.01, "close": 136.01}],
                [], baseline_factor=1.0,
            )
        finally:
            conn.close()
        current = {
            **_morning_rows()[0], "stock_code": "688300",
            "open": 104.62, "high": 105.0, "low": 100.0, "close": 102.70,
            "preclose": 104.62, "change_percent": None,
        }
        out = fetch_morning_batch(
            trade_date=TARGET,
            adapter=FakeMorningAdapter(
                rows=[current], factor_exc=FetchError("factor unavailable")
            ),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert out["status"] == "partial_error"
        assert out["stage"] == "rebuilt"
        assert out["messages"] == ["数据处理完成"]
        assert out["parts"]["stock_adjust_factor"]["error_kind"] == "pctchg_derivation_failed"
        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='688300'"
                )
                assert cur.fetchone()[0] is True
                cur.execute(
                    "SELECT COUNT(*) FROM stock_adjust_factor"
                    " WHERE stock_code='688300' AND ex_date=%s",
                    (TARGET,),
                )
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()

    def test_unknown_fallback_does_not_retract_existing_event(self, pg_ready):
        from qstock_mcp.adjustment import rebuild_stock

        previous = {
            **_morning_rows("20261015")[0], "stock_code": "688300",
            "open": 136.01, "high": 136.01, "low": 136.01, "close": 136.01,
        }
        current = {
            **_morning_rows()[0], "stock_code": "688300",
            "open": 104.62, "high": 105.0, "low": 100.0, "close": 102.70,
            "preclose": 104.62, "change_percent": -1.8352,
        }
        conn = psycopg2.connect(pg_ready)
        try:
            rebuild_stock(
                conn, "688300", [previous, current],
                [{"ex_date": "20260610", "factor_cum": 2.965589},
                 {"ex_date": TARGET, "factor_cum": 3.855379}],
                baseline_factor=2.965589,
            )
        finally:
            conn.close()

        out = fetch_morning_batch(
            trade_date=TARGET,
            adapter=FakeMorningAdapter(
                rows=[{**current, "preclose": None}],
                factor_exc=FetchError("factor unavailable"),
            ),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert out["status"] == "partial_error"
        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT factor_cum, source FROM stock_adjust_factor"
                    " WHERE stock_code='688300' AND ex_date=%s",
                    (TARGET,),
                )
                factor, source = cur.fetchone()
                assert float(factor) == 3.855379
                assert source == "baostock-adjust-factor"
                cur.execute(
                    "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='688300'"
                )
                assert cur.fetchone()[0] is True
        finally:
            conn.close()

    def test_unknown_first_baseline_blocks_verified(self, pg_ready):
        current = {
            **_morning_rows()[0], "stock_code": "688300",
            "open": 104.62, "high": 105.0, "low": 100.0, "close": 102.70,
            "preclose": 104.62, "change_percent": -1.8352,
        }
        out = fetch_morning_batch(
            trade_date=TARGET,
            adapter=FakeMorningAdapter(
                rows=[current],
                events=[{"stock_code": "688300", "ex_date": TARGET,
                         "factor_cum": 3.855379}],
            ),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert out["status"] == "partial_error"
        assert out["stage"] == "rebuilt"
        assert out["messages"] == ["数据处理完成"]
        assert out["parts"]["stock_adjust_factor"]["error_kind"] == "rebuild_pending"
        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT baseline_factor, factor_latest, rebuild_pending"
                    " FROM stock_factor_state WHERE stock_code='688300'"
                )
                baseline, latest, pending = cur.fetchone()
                assert baseline is None and latest is None
                assert pending is True
        finally:
            conn.close()

    def test_invalid_close_is_quarantined_and_blocks_verified(self, pg_ready):
        current = {
            **_morning_rows()[0], "open": 0.0, "high": 0.0, "low": 0.0,
            "close": 0.0,
        }
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=[current]),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert out["status"] == "partial_error"
        assert out["stage"] == "rebuilt"
        assert out["parts"]["stock_adjust_factor"]["error_kind"] == "invalid_close"
        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM stock_daily_raw WHERE stock_code='600519'")
                assert cur.fetchone()[0] == 1
                cur.execute("SELECT COUNT(*) FROM stock_daily_hfq WHERE stock_code='600519'")
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()
        invalid = [row for row in _ledger(pg_ready, "stock_daily_raw")
                   if row[5] == "invalid_close"]
        assert len(invalid) == 1

    def test_no_event_day_only_writes_current_hfq_row(self, pg_ready):
        from qstock_mcp.adjustment import rebuild_stock

        conn = psycopg2.connect(pg_ready)
        try:
            rebuild_stock(
                conn, "600519",
                [{**_morning_rows("20261015")[0], "close": 1700.0}], []
            )
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE stock_daily_hfq SET rebuilt_at=%s"
                    " WHERE stock_code='600519' AND trade_date='20261015'",
                    (datetime(2000, 1, 1, tzinfo=timezone.utc),),
                )
            conn.commit()
        finally:
            conn.close()

        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=_morning_rows()[:1]),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)
        assert out["status"] == "ok"

        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rebuilt_at FROM stock_daily_hfq"
                    " WHERE stock_code='600519' AND trade_date='20261015'"
                )
                assert cur.fetchone()[0] == datetime(2000, 1, 1, tzinfo=timezone.utc)
                cur.execute(
                    "SELECT COUNT(*) FROM stock_daily_hfq"
                    " WHERE stock_code='600519' AND trade_date=%s",
                    (TARGET,),
                )
                assert cur.fetchone()[0] == 1
        finally:
            conn.close()

    def test_raw_trading_day_gap_blocks_verified(self, pg_ready):
        from qstock_mcp.adjustment import rebuild_stock

        conn = psycopg2.connect(pg_ready)
        try:
            rebuild_stock(
                conn, "600519",
                [{**_morning_rows("20261014")[0], "close": 1700.0}], []
            )
        finally:
            conn.close()
        current = {**_morning_rows()[0], "preclose": 1700.0}
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=[current]),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert out["status"] == "partial_error"
        assert out["stage"] == "rebuilt"
        assert out["parts"]["stock_adjust_factor"]["error_kind"] == "raw_gap"
        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='600519'"
                )
                assert cur.fetchone()[0] is True
        finally:
            conn.close()

    def test_first_authoritative_event_recomputes_no_event_baseline(self, pg_ready):
        from qstock_mcp.adjustment import rebuild_stock

        conn = psycopg2.connect(pg_ready)
        try:
            rebuild_stock(
                conn, "688300",
                [{**_morning_rows("20261015")[0], "stock_code": "688300",
                  "open": 136.01, "high": 136.01, "low": 136.01, "close": 136.01}],
                [], baseline_factor=1.0,
            )
        finally:
            conn.close()
        current = {
            **_morning_rows()[0], "stock_code": "688300",
            "open": 104.62, "high": 105.0, "low": 100.0, "close": 102.70,
            "preclose": 104.62, "change_percent": -1.8352,
        }
        out = fetch_morning_batch(
            trade_date=TARGET,
            adapter=FakeMorningAdapter(
                rows=[current],
                events=[{"stock_code": "688300", "ex_date": TARGET,
                         "factor_cum": 3.855379}],
            ),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=NOW_MONDAY_AM)

        assert out["status"] == "ok"
        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT baseline_factor, factor_latest FROM stock_factor_state"
                    " WHERE stock_code='688300'"
                )
                baseline, latest = cur.fetchone()
                assert float(baseline) == pytest.approx(2.965588537153224)
                assert float(latest) == pytest.approx(1.300038204889484)
        finally:
            conn.close()


class TestDefaultTargetDate:
    def test_default_is_prev_trading_day(self, pg_ready):
        adapter = FakeMorningAdapter(rows=_morning_rows())
        out = fetch_morning_batch(
            adapter=adapter, adapters=[FakeInitAdapter("fake")],
            sw_adapters=_sw(), now=NOW_MONDAY_AM)
        assert out["trade_date"] == TARGET  # 周一开盘前 → 上周五
        assert adapter.calls == [TARGET]

    def test_intraday_today_rejected(self, pg_ready):
        """盘中禁写防线：显式 --date 当日且正在交易时段 → error 零落库。"""
        now = datetime(2026, 10, 19, 10, 0, tzinfo=_TZ8)
        out = fetch_morning_batch(
            trade_date="20261019",
            adapter=FakeMorningAdapter(rows=_morning_rows("20261019")),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(), now=now)
        assert out["status"] == "error"
        assert _ledger(pg_ready) == []


class TestIndexTailFill:
    def _seed_index(self, dsn, code, rows):
        conn = psycopg2.connect(dsn)
        try:
            upsert_index_daily(conn, code, rows, "seed")
        finally:
            conn.close()

    def _index_row(self, d):
        return {
            "trade_date": d, "open": 1.0, "high": 1.0, "low": 1.0,
            "close": 1.0, "volume": 1, "amount": 1.0, "amplitude": None,
            "change_percent": 0.1, "change_amount": None, "turnover_rate": None,
        }

    def test_missing_tail_filled(self, pg_ready):
        """000001 尾行停在 10-14：补抓 10-15..10-16；399001 已到目标日零补抓。"""
        for code in ("000001", "399001", "399006", "000300", "000016",
                     "000905", "000688"):
            last = "20261014" if code == "000001" else TARGET
            self._seed_index(pg_ready, code, [self._index_row(last)])
        filler = FakeInitAdapter("fake", index_rows=[self._index_row(TARGET)])
        fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=[]),
            adapters=[filler], sw_adapters=_sw(), now=NOW_MONDAY_AM)
        conn = psycopg2.connect(pg_ready)
        try:
            rows = select_index_daily(conn, "000001", limit=2)
            assert rows[-1]["trade_date"] == TARGET
            rows399 = select_index_daily(conn, "399001", limit=2)
            assert len(rows399) == 1  # 未补抓（尾行已在目标日）
        finally:
            conn.close()
        # 补抓请求区间从尾行次日起
        assert filler.calls == ["index"]

    def test_index_fill_ledger(self, pg_ready):
        for code in ("000001", "399001", "399006", "000300", "000016",
                     "000905", "000688"):
            last = "20261014" if code == "000001" else TARGET
            self._seed_index(pg_ready, code, [self._index_row(last)])
        fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=[]),
            adapters=[FakeInitAdapter("fake", index_rows=[self._index_row(TARGET)])],
            sw_adapters=_sw(), now=NOW_MONDAY_AM)
        rows = _ledger(pg_ready, section="index_daily")
        assert len(rows) == 1
        assert rows[0][2] == "ok"
        assert rows[0][4] == 1
        assert "000001" in rows[0][6]

    def test_index_fill_failure_independent(self, pg_ready):
        """指数补漏全源失败：index_daily 部分 error + 台账，个股部分不受影响。"""
        self._seed_index(pg_ready, "000001", [self._index_row("20261014")])
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=_morning_rows()),
            adapters=[FakeInitAdapter("fake", fail=("index",))],
            sw_adapters=_sw(), now=NOW_MONDAY_AM)
        assert out["parts"]["stock_daily"]["status"] == "ok"
        assert out["parts"]["index_daily"]["status"] == "error"
        assert out["status"] == "partial_error"
        assert _ledger(pg_ready, section="index_daily")[0][2] == "error"


class TestSchedulerMorningCli:
    def test_morning_subcommand(self, pg_ready, capsys, monkeypatch):
        monkeypatch.setattr(
            scheduler, "fetch_morning_batch",
            lambda **kw: {"status": "ok", "trade_date": TARGET, "parts": {}})
        rc = scheduler.main(["morning"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["job"] == "morning"
        assert out["status"] == "ok"

    def test_morning_partial_failure_exit_1(self, pg_ready, capsys, monkeypatch):
        monkeypatch.setattr(
            scheduler, "fetch_morning_batch",
            lambda **kw: {"status": "partial_error", "trade_date": TARGET,
                          "parts": {"stock_daily": {"status": "error"}}})
        rc = scheduler.main(["morning"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "partial_error"


class TestSwBoardValuationPart:
    """申万板块分析报表估值维度（issue #118）：T+1 落 sw_board_valuation。"""

    def _valuation_rows(self, dsn, trade_date):
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT board_code, board_name, level, close_index,"
                    " turnover_rate, pe_ratio, pb_ratio, float_cap,"
                    " dividend_yield FROM sw_board_valuation"
                    " WHERE trade_date = %s ORDER BY board_code",
                    (trade_date,),
                )
                return cur.fetchall()
        finally:
            conn.close()

    def test_valuation_rows_land(self, pg_ready):
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=[]),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(),
            now=NOW_MONDAY_AM)
        part = out["parts"]["sw_board_valuation"]
        assert part["status"] == "ok"
        assert part["rows"] == 2
        assert part["source"] == "swsresearch"
        rows = self._valuation_rows(pg_ready, TARGET)
        assert len(rows) == 2
        code, name, level, close, turnover, pe, pb, float_cap, dp = rows[0]
        assert code == "801010" and name == "农林牧渔" and level == 1
        assert close == 2491.17
        assert turnover == 4.65 and pe == 78.88 and pb == 2.37
        assert float_cap == 5_664_540_000.0  # 流通市值万元口径（ADR 0006）
        assert dp == 1.43
        # 已注册 query_board_data 白名单（BOARD_QUERY_TABLES）
        from qstock_mcp.tools_board import query_board_data

        q = query_board_data("sw_board_valuation", trade_date=TARGET)
        assert q["status"] == "ok" and q["count"] == 2
        assert q["rows"][0]["board_code"] == "801010"

    def test_valuation_ledger_recorded(self, pg_ready):
        fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=[]),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(),
            now=NOW_MONDAY_AM)
        rows = _ledger(pg_ready, section="sw_board_valuation")
        assert len(rows) == 1
        assert rows[0][0] == TARGET
        assert rows[0][2] == "ok"
        assert rows[0][3] == "swsresearch"
        assert rows[0][4] == 2

    def test_valuation_rerun_idempotent(self, pg_ready):
        for _ in range(2):
            fetch_morning_batch(
                trade_date=TARGET, adapter=FakeMorningAdapter(rows=[]),
                adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(),
                now=NOW_MONDAY_AM)
        assert len(self._valuation_rows(pg_ready, TARGET)) == 2

    def test_valuation_empty_not_error(self, pg_ready):
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=[]),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(analysis=[]),
            now=NOW_MONDAY_AM)
        assert out["parts"]["sw_board_valuation"]["status"] == "empty"
        assert out["parts"]["sw_board_valuation"]["empty_kind"] == "no_data_for_date"
        assert _ledger(pg_ready, section="sw_board_valuation")[0][2] == "empty"

    def test_valuation_failure_independent(self, pg_ready):
        """申万不可达：估值 part error + 台账，其余 part 不受影响。"""
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=_morning_rows()),
            adapters=[FakeInitAdapter("fake")], sw_adapters=_sw(fail=("analysis",)),
            now=NOW_MONDAY_AM)
        assert out["parts"]["stock_daily"]["status"] == "ok"
        assert out["parts"]["sw_board_valuation"]["status"] == "error"
        assert out["status"] == "partial_error"
        rows = _ledger(pg_ready, section="sw_board_valuation")
        assert rows[0][2] == "error"
        assert rows[0][5] == "fetch_failed"
