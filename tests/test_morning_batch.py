"""次晨批测试（issue #83，spec #71/W5）：baostock 一把抓全市场日线 + 指数尾行补漏。

fetch_morning_batch：默认目标日 = 前一交易日（时钟可注入）；一把抓结果按
业务键 (stock_code, trade_date, adj) 幂等落 stock_daily（tradestatus/turn
等字段映射在适配器映射层）；7 大指数白名单尾行缺失即补（fetch_index_daily
fallback 链）；各部分独立成败、逐部分落采集台账；baostock 不可达按三态
契约报错落台账，不影响指数补漏步骤。
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
from fakes import FakeInitAdapter

_TZ8 = timezone(timedelta(hours=8))

TARGET = "20261016"  # 周五；2026-10-19（周一）开盘前跑次晨批的目标日
NOW_MONDAY_AM = datetime(2026, 10, 19, 8, 30, tzinfo=_TZ8)


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


@pytest.fixture
def pg_ready(pg_test):
    _init(pg_test)
    return pg_test


class FakeMorningAdapter:
    """一把抓全市场日线的 fake 源：rows 为已映射行（含 stock_code/adj）。"""

    def __init__(self, name="baostock", rows=None, exc=None):
        self.name = name
        self._rows = rows
        self._exc = exc
        self.calls = []

    def fetch_astock_daily(self, trade_date):
        self.calls.append(trade_date)
        if self._exc is not None:
            raise self._exc
        return list(self._rows or [])


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
    def test_full_market_rows_land(self, pg_ready):
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=_morning_rows()),
            adapters=[FakeInitAdapter("fake")], now=NOW_MONDAY_AM)
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

    def test_ledger_recorded(self, pg_ready):
        fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=_morning_rows()),
            adapters=[FakeInitAdapter("fake")], now=NOW_MONDAY_AM)
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
                adapters=[FakeInitAdapter("fake")], now=NOW_MONDAY_AM)
        assert len(_stock_daily_rows(pg_ready, "600519")) == 1  # 业务键幂等覆盖

    def test_baostock_unreachable_three_state(self, pg_ready):
        """baostock 不可达：三态契约 error_kind + 台账 error 行，指数补漏不受影响。"""
        exc = FetchError("baostock 登录失败")
        exc.__cause__ = ConnectionRefusedError("connection refused")
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(exc=exc),
            adapters=[FakeInitAdapter("fake")], now=NOW_MONDAY_AM)
        part = out["parts"]["stock_daily"]
        assert part["status"] == "error"
        assert part["error_kind"] == "upstream_unreachable"
        assert out["status"] == "partial_error"  # 指数部分照常
        rows = _ledger(pg_ready, section="stock_daily")
        assert rows[0][2] == "error"
        assert rows[0][5] == "upstream_unreachable"
        assert _stock_daily_rows(pg_ready, "600519") == []

    def test_empty_result_not_error(self, pg_ready):
        """非交易日/上游确认无数据：empty 落台账，非故障。"""
        out = fetch_morning_batch(
            trade_date=TARGET, adapter=FakeMorningAdapter(rows=[]),
            adapters=[FakeInitAdapter("fake")], now=NOW_MONDAY_AM)
        assert out["parts"]["stock_daily"]["status"] == "empty"
        assert _ledger(pg_ready, section="stock_daily")[0][2] == "empty"


class TestDefaultTargetDate:
    def test_default_is_prev_trading_day(self, pg_ready):
        adapter = FakeMorningAdapter(rows=_morning_rows())
        out = fetch_morning_batch(
            adapter=adapter, adapters=[FakeInitAdapter("fake")],
            now=NOW_MONDAY_AM)
        assert out["trade_date"] == TARGET  # 周一开盘前 → 上周五
        assert adapter.calls == [TARGET]

    def test_intraday_today_rejected(self, pg_ready):
        """盘中禁写防线：显式 --date 当日且正在交易时段 → error 零落库。"""
        now = datetime(2026, 10, 19, 10, 0, tzinfo=_TZ8)
        out = fetch_morning_batch(
            trade_date="20261019",
            adapter=FakeMorningAdapter(rows=_morning_rows("20261019")),
            adapters=[FakeInitAdapter("fake")], now=now)
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
            adapters=[filler], now=NOW_MONDAY_AM)
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
            now=NOW_MONDAY_AM)
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
            now=NOW_MONDAY_AM)
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
