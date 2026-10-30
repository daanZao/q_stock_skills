"""tools_lhb 核心层与 qstock-scheduler lhb 子命令测试（issue #114，spec #111）。

fake 适配器注 tools_* 层（既有接缝），真实 PG 测试库验落库；对拍以 fake
exchange/fake sina 驱动，不触网。CLI 测试走 scheduler.main 直调（同
test_scheduler.py 模式）。
"""

import json
from datetime import date, datetime

import psycopg2
import pytest

from qstock_mcp import scheduler
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_lhb import (
    compare_lhb_sources,
    fetch_lhb_backfill,
    fetch_lhb_day,
)

TODAY = date.today().strftime("%Y%m%d")

EXCHANGE_LHB = {
    "lhb_basic": [
        {
            "trade_date": TODAY, "stock_code": "000530", "stock_name": "冰山冷热",
            "close_price": None, "change_percent": None, "turnover_rate": 18.39,
            "lhb_reason": "日价格振幅达到18.39%",
            "net_buy_amount": -1567.4644, "buy_amount": 3601.5072,
            "sell_amount": 5168.9716, "total_amount": 8770.4788,
            "day_amount": 204283.8632,
        },
        {
            "trade_date": TODAY, "stock_code": "600302", "stock_name": "ST标准",
            "close_price": None, "change_percent": None, "turnover_rate": None,
            "lhb_reason": "一、涨幅偏离值达7%的前五只证券",
            "net_buy_amount": -58.71, "buy_amount": 293.5474,
            "sell_amount": 352.2574, "total_amount": 645.8048,
            "day_amount": 4461.78,
        },
    ],
    # lhb_stock_statistic 不在适配器承载（issue #114 定稿）：库内 lhb_basic
    # 近 60 日历日窗口滚动自算
    "lhb_yyb_capital": [],
    "lhb_yyb_most": [],
}


class FakeLhbAdapter:
    name = "exchange_fake"

    def __init__(self, payload=None, exc=None):
        self._payload = payload if payload is not None else EXCHANGE_LHB
        self._exc = exc

    def fetch_lhb(self, trade_date):
        if self._exc is not None:
            raise self._exc
        # 行级 trade_date 以请求日为准（生产语义：上榜日=请求交易日）——
        # 深度复制后盖戳，避免跨日用例互相污染
        return {
            table: (
                [
                    dict(row, trade_date=trade_date) if isinstance(row, dict) else row
                    for row in rows
                ]
                if isinstance(rows, list) else rows
            )
            for table, rows in self._payload.items()
        }


class FakeSina:
    name = "sina_lhb_fake"

    def __init__(self, rows=None, exc=None, seat_exc=None, seat_nets=None,
                 ggtj_rows=None, ggtj_exc=None):
        self._rows = rows if rows is not None else []
        self._exc = exc
        self._seat_exc = seat_exc
        # {code: 净买额万元}：脚本化席位净额，驱动 net_buy 对拍的 ok/diff 路径
        self._seat_nets = seat_nets or {}
        self._ggtj_rows = ggtj_rows
        self._ggtj_exc = ggtj_exc
        self.seat_calls = []

    def daily_detail(self, trade_date):
        if self._exc is not None:
            raise self._exc
        return self._rows

    def seat_detail(self, code, trade_date, seat_type):
        self.seat_calls.append((code, seat_type))
        if self._seat_exc is not None:
            raise self._seat_exc
        net = self._seat_nets.get(code, 70.0)
        # fake 以"买侧全部买额/卖侧全部卖额"表达净额（与东财口径一致）
        if net >= 0:
            return {"buy": [{"buy_wan": net, "sell_wan": 0.0}],
                    "sell": [{"buy_wan": 0.0, "sell_wan": 0.0}]}
        return {"buy": [{"buy_wan": 0.0, "sell_wan": 0.0}],
                "sell": [{"buy_wan": 0.0, "sell_wan": -net}]}

    def ggtj(self):
        """ggtj 统计窗口（对拍校验位）；None 缺省 = 上游无数据。"""
        if self._ggtj_exc is not None:
            raise self._ggtj_exc
        return list(self._ggtj_rows or [])


def _matched_sina():
    """席位净额与交易所一致的 fake（net 对拍走 ok 路径）。"""
    return FakeSina(
        rows=_sina_rows(),
        seat_nets={"000530": -1567.4644, "600302": -58.71},
    )


@pytest.fixture
def pg_ready(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()
    return pg_test


def _count(dsn, table):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]
    finally:
        conn.close()


def _ledger(dsn):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT section, status, source, rows FROM collection_log"
                " ORDER BY id"
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- fetch_lhb_day


def test_fetch_lhb_day_lands_all_tables(pg_ready):
    r = fetch_lhb_day(TODAY, lhb_adapters=[FakeLhbAdapter()])
    assert r["status"] == "ok" and r["lhb"]["status"] == "ok"
    assert r["lhb"]["rows"] == 4  # basic 2 + statistic 自算 2
    assert _count(pg_ready, "lhb_basic") == 2
    # statistic 库内自算（issue #114 定稿）：近 60 日历日窗口按股聚合；
    # day_amount 为适配器元数据键，非落库列（upsert 只写 LHB_BASIC_FIELDS，
    # 列不存在即证明被忽略）；业务键含 lhb_reason：同股当日多榜单多行
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, appear_count_3m, buy_amount_3m,"
                "       sell_amount_3m, net_buy_3m, buy_seat_count"
                " FROM lhb_stock_statistic ORDER BY stock_code"
            )
            stat = cur.fetchall()
            cur.execute(
                "SELECT stock_code, buy_amount FROM lhb_basic ORDER BY stock_code"
            )
            got = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns"
                " WHERE table_name = 'lhb_basic' AND column_name = 'day_amount'"
            )
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()
    assert stat == [
        ("000530", 1, pytest.approx(3601.5072), pytest.approx(5168.9716),
         pytest.approx(-1567.4644), None),  # 席位计数自算恒 NULL（不伪造）
        ("600302", 1, pytest.approx(293.5474), pytest.approx(352.2574),
         pytest.approx(-58.71), None),
    ]
    assert got == [
        ("000530", pytest.approx(3601.5072)),
        ("600302", pytest.approx(293.5474)),
    ]
    # 台账：lhb section 由 tools_board 内联记录
    assert ("lhb", "ok", "exchange_fake", 4) in _ledger(pg_ready)


def test_fetch_lhb_day_empty_is_ok_with_note(pg_ready):
    empty = {t: [] for t in EXCHANGE_LHB}
    r = fetch_lhb_day(TODAY, lhb_adapters=[FakeLhbAdapter(payload=empty)])
    assert r["lhb"]["status"] == "ok"
    assert r["lhb"]["empty_kind"] == "no_data_for_date"
    assert _count(pg_ready, "lhb_basic") == 0
    assert _count(pg_ready, "lhb_stock_statistic") == 0  # 窗口无历史，自算零行
    assert ("lhb", "empty", "exchange_fake", 0) in _ledger(pg_ready)


def test_fetch_lhb_day_failure_structured(pg_ready):
    r = fetch_lhb_day(TODAY, lhb_adapters=[FakeLhbAdapter(exc=FetchError("boom"))])
    assert r["lhb"]["status"] == "error" and r["lhb"]["error_kind"] == "fetch_failed"
    assert _count(pg_ready, "lhb_basic") == 0
    # 台账 error 行不记 source（source 为成功路径的命中源语义）
    assert ("lhb", "error", None, None) in _ledger(pg_ready)


def test_fetch_lhb_day_partial_error_lands_rows(pg_ready):
    partial = {
        **EXCHANGE_LHB,
        "lhb_yyb_capital": [],  # 子项失败：无行 + errors 记录原因
        "errors": ["lhb_yyb_capital: 同花顺改版"],
    }
    r = fetch_lhb_day(TODAY, lhb_adapters=[FakeLhbAdapter(payload=partial)])
    assert r["lhb"]["status"] == "ok"
    assert "同花顺改版" in r["lhb"]["partial_error"]
    assert _count(pg_ready, "lhb_basic") == 2
    # statistic 自算不受 yyb 子项失败影响（由 lhb_basic 派生）
    assert _count(pg_ready, "lhb_stock_statistic") == 2


# ---------------------------------------------------------------- backfill


def test_fetch_lhb_backfill_default_history_adapters_are_shared(pg_ready, monkeypatch):
    calls = []

    class Recorder(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            calls.append(trade_date)
            return super().fetch_lhb(trade_date)

    recorder = Recorder()
    created = []

    def factory(ths=None):
        created.append(ths)
        return [recorder]

    monkeypatch.setattr("qstock_mcp.tools_lhb.default_lhb_history_adapters", factory)
    result = fetch_lhb_backfill("20261020", "20261020")

    assert result["status"] == "ok"
    assert len(created) == 1 and created[0] is not None
    assert "20261020" in calls
    assert len(calls) > 1  # staging 与目标窗口复用同一实例


def test_fetch_lhb_backfill_iterates_trading_days(pg_ready, monkeypatch):
    calls = []

    class Recorder(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            calls.append(trade_date)
            return super().fetch_lhb(trade_date)

    # 窗口含周末：只抓交易日
    r = fetch_lhb_backfill("2026-10-16", "2026-10-20",
                           lhb_adapters=[Recorder()])
    assert r["status"] == "ok" and r["total_days"] == 3  # 16/19/20（17/18 周末）
    assert [d for d in calls if d >= "20261016"] == [
        "20261016", "20261019", "20261020",
    ]
    assert all(d < "20261016" for d in calls[:-3])  # 统计前置只抓、不写库
    assert r["empty_days"] == 0
    # 逐日落库 + 逐日台账
    assert _count(pg_ready, "lhb_basic") == 2 * 3
    assert len([x for x in _ledger(pg_ready) if x[0] == "lhb"]) == 3


def test_fetch_lhb_backfill_ths_patches_only_failed_market(pg_ready):
    official = {
        **EXCHANGE_LHB,
        "lhb_basic": [EXCHANGE_LHB["lhb_basic"][1]],
        "errors": ["szse: 深交所断连"],
    }

    class PartialOnTargetDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            payload = official if trade_date == "20261020" else EXCHANGE_LHB
            return FakeLhbAdapter(payload=payload).fetch_lhb(trade_date)

    class FakeThs:
        name = "ths_fake"

        def __init__(self):
            self.calls = []

        def fetch_lhb_basic(self, trade_date):
            self.calls.append(trade_date)
            return [
                dict(EXCHANGE_LHB["lhb_basic"][0], trade_date=trade_date),
                dict(EXCHANGE_LHB["lhb_basic"][1], trade_date=trade_date),
            ]

    fallback = FakeThs()
    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[PartialOnTargetDay()], lhb_fallback=fallback,
    )

    assert result["status"] == "ok"
    assert fallback.calls == ["20261020"]
    assert result["results"][0]["source"] == "exchange_fake+ths_fake"
    assert result["results"][0]["source_detail"] == {
        "primary": "exchange_fake",
        "fallback": {"source": "ths_fake", "markets": ["szse"], "rows": 1},
    }
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, buy_amount FROM lhb_basic"
                " WHERE trade_date = '20261020' ORDER BY stock_code"
            )
            assert cur.fetchall() == [
                ("000530", pytest.approx(3601.5072)),
                ("600302", pytest.approx(293.5474)),
            ]
            cur.execute(
                "SELECT COUNT(*) FROM lhb_stock_statistic WHERE trade_date = '20261020'"
            )
            assert cur.fetchone()[0] == 2
            cur.execute(
                "SELECT source, detail FROM collection_log"
                " WHERE trade_date = '20261020' AND section = 'lhb'"
            )
            source, detail = cur.fetchone()
    finally:
        conn.close()
    assert source == "exchange_fake+ths_fake"
    assert "ths_fake:szse" in detail


def test_fetch_lhb_backfill_szse_primary_reuses_ths_page_for_other_markets(pg_ready):
    ths_rows = [
        dict(EXCHANGE_LHB["lhb_basic"][0], trade_date="20261020"),
        dict(EXCHANGE_LHB["lhb_basic"][1], trade_date="20261020"),
    ]
    payload = {
        **EXCHANGE_LHB,
        "lhb_basic": [ths_rows[0]],
        "errors": ["sse: 上交所断连"],
        "szse_primary": {"requested": "ths_fake", "source": "ths_fake", "rows": 1},
        "ths_basic_rows": ths_rows,
    }

    class UnexpectedThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, _trade_date):
            raise AssertionError("同一日期不得重复请求同花顺")

    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[FakeLhbAdapter(payload=payload)],
        lhb_fallback=UnexpectedThs(),
    )

    assert result["status"] == "ok"
    assert result["results"][0]["source"] == "ths_fake+exchange_fake"
    assert result["results"][0]["source_detail"]["szse_primary"] == {
        "requested": "ths_fake", "source": "ths_fake", "rows": 1,
    }
    assert result["results"][0]["source_detail"]["fallback"]["markets"] == ["sse"]
    assert _count(pg_ready, "lhb_basic") == 2


def test_fetch_lhb_backfill_error_preserves_szse_primary_provenance(pg_ready):
    payload = {
        **EXCHANGE_LHB,
        "lhb_basic": [dict(EXCHANGE_LHB["lhb_basic"][0], trade_date="20261020")],
        "errors": ["sse: 上交所断连"],
        "szse_primary": {"requested": "ths_fake", "source": "ths_fake", "rows": 1},
    }
    class PartialOnTargetDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            selected = payload if trade_date == "20261020" else EXCHANGE_LHB
            return FakeLhbAdapter(payload=selected).fetch_lhb(trade_date)

    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[PartialOnTargetDay()],
    )

    entry = result["results"][0]
    assert entry["status"] == "error"
    assert entry["source"] == "ths_fake+exchange_fake"
    assert entry["source_detail"]["szse_primary"]["source"] == "ths_fake"
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source, detail FROM collection_log"
                " WHERE trade_date = '20261020' AND section = 'lhb'"
            )
            source, detail = cur.fetchone()
    finally:
        conn.close()
    assert source == "ths_fake+exchange_fake"
    assert "szse_primary=ths_fake(1行)" in detail


def test_fetch_lhb_backfill_all_failed_preserves_szse_primary_provenance(pg_ready):
    class AllFailOnTargetDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            if trade_date != "20261020":
                return super().fetch_lhb(trade_date)
            error = FetchError("同花顺与交易所均失败")
            error.source_meta = {
                "szse_primary": {
                    "requested": "ths_fake", "source": "exchange", "rows": 0,
                    "error": "同花顺封禁",
                }
            }
            raise error

    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[AllFailOnTargetDay()],
    )

    entry = result["results"][0]
    assert entry["status"] == "error"
    assert entry["source"] == "ths_fake+exchange_fake"
    assert entry["source_detail"]["szse_primary"]["error"] == "同花顺封禁"
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source, detail FROM collection_log"
                " WHERE trade_date = '20261020' AND section = 'lhb'"
            )
            source, detail = cur.fetchone()
    finally:
        conn.close()
    assert source == "ths_fake+exchange_fake"
    assert "szse_primary=ths_fake->exchange(0行):同花顺封禁" in detail


def test_fetch_lhb_backfill_ths_recovers_all_markets_failed(pg_ready):
    class AllFailOnTargetDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            if trade_date == "20261020":
                raise FetchError("三所均断连")
            return super().fetch_lhb(trade_date)

    class FakeThs:
        name = "ths_fake"

        def __init__(self):
            self.calls = []

        def fetch_lhb_basic(self, trade_date):
            self.calls.append(trade_date)
            bse = dict(
                EXCHANGE_LHB["lhb_basic"][0],
                stock_code="920454", stock_name="同心传动",
            )
            return [
                dict(row, trade_date=trade_date)
                for row in [*EXCHANGE_LHB["lhb_basic"], bse]
            ]

    fallback = FakeThs()
    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[AllFailOnTargetDay()], lhb_fallback=fallback,
    )

    assert result["status"] == "ok"
    assert fallback.calls == ["20261020"]
    assert result["results"][0]["source_detail"]["fallback"] == {
        "source": "ths_fake", "markets": ["szse", "sse", "bse"], "rows": 3,
    }
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code FROM lhb_basic WHERE trade_date = '20261020'"
                " ORDER BY stock_code"
            )
            assert cur.fetchall() == [("000530",), ("600302",), ("920454",)]
            cur.execute(
                "SELECT COUNT(*) FROM lhb_stock_statistic WHERE trade_date = '20261020'"
            )
            assert cur.fetchone()[0] == 3
    finally:
        conn.close()


def test_fetch_lhb_backfill_ths_handles_nested_all_basic_failure(pg_ready):
    official = {
        **EXCHANGE_LHB,
        "lhb_basic": [],
        "lhb_yyb_capital": [{"rank": 1, "seat_name": "营业部仍在", "total_amount": 1.0}],
        "errors": ["lhb_basic: 三所均失败: szse: 断连; sse: 断连; bse: 断连"],
    }

    class NestedFailureOnTargetDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            payload = official if trade_date == "20261020" else EXCHANGE_LHB
            return FakeLhbAdapter(payload=payload).fetch_lhb(trade_date)

    class FakeThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, trade_date):
            bse = dict(EXCHANGE_LHB["lhb_basic"][0], stock_code="920454")
            return [dict(row, trade_date=trade_date)
                    for row in [*EXCHANGE_LHB["lhb_basic"], bse]]

    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[NestedFailureOnTargetDay()], lhb_fallback=FakeThs(),
    )

    assert result["status"] == "ok"
    assert result["results"][0]["source_detail"]["fallback"]["markets"] == [
        "szse", "sse", "bse",
    ]
    assert _count(pg_ready, "lhb_basic") == 3
    assert _count(pg_ready, "lhb_yyb_capital") == 0


def test_fetch_lhb_backfill_ths_repairs_staging_without_window_writes(pg_ready):
    class AllFailOnLookbackDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            if trade_date == "20261019":
                raise FetchError("三所均断连")
            return super().fetch_lhb(trade_date)

    class FakeThs:
        name = "ths_fake"

        def __init__(self):
            self.calls = []

        def fetch_lhb_basic(self, trade_date):
            self.calls.append(trade_date)
            bse = dict(
                EXCHANGE_LHB["lhb_basic"][0],
                stock_code="920454", stock_name="同心传动",
            )
            return [
                dict(row, trade_date=trade_date)
                for row in [*EXCHANGE_LHB["lhb_basic"], bse]
            ]

    fallback = FakeThs()
    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[AllFailOnLookbackDay()], lhb_fallback=fallback,
    )

    assert result["status"] == "ok"
    assert fallback.calls == ["20261019"]
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM lhb_basic")
            assert cur.fetchone() == ("20261020", "20261020", 2)
            cur.execute(
                "SELECT stock_code, appear_count_3m FROM lhb_stock_statistic"
                " WHERE trade_date = '20261020' ORDER BY stock_code"
            )
            assert cur.fetchall() == [
                ("000530", 37), ("600302", 37), ("920454", 1),
            ]
    finally:
        conn.close()


def test_fetch_lhb_backfill_ths_patches_sse_and_bse_only(pg_ready):
    official = {
        **EXCHANGE_LHB,
        "lhb_basic": [EXCHANGE_LHB["lhb_basic"][0]],
        "errors": ["sse: 上交所断连", "bse: 北交所断连"],
    }

    class PartialOnTargetDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            payload = official if trade_date == "20261020" else EXCHANGE_LHB
            return FakeLhbAdapter(payload=payload).fetch_lhb(trade_date)

    class FakeThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, trade_date):
            bse = dict(EXCHANGE_LHB["lhb_basic"][0], stock_code="920454")
            return [dict(row, trade_date=trade_date)
                    for row in [*EXCHANGE_LHB["lhb_basic"], bse]]

    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[PartialOnTargetDay()], lhb_fallback=FakeThs(),
    )

    assert result["status"] == "ok"
    assert result["results"][0]["source_detail"]["fallback"]["markets"] == ["sse", "bse"]
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT stock_code FROM lhb_basic ORDER BY stock_code")
            assert cur.fetchall() == [("000530",), ("600302",), ("920454",)]
    finally:
        conn.close()


def test_fetch_lhb_backfill_official_empty_does_not_call_ths(pg_ready):
    empty = {table: [] for table in EXCHANGE_LHB}

    class UnexpectedThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, _trade_date):
            raise AssertionError("官方正常空日不应请求后备")

    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[FakeLhbAdapter(payload=empty)], lhb_fallback=UnexpectedThs(),
    )

    assert result["status"] == "ok"
    assert result["empty_days"] == 1
    assert _count(pg_ready, "lhb_basic") == 0


def test_fetch_lhb_backfill_incomplete_staging_aborts_without_writes(pg_ready):
    class AllFailOnLookbackDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            if trade_date == "20261019":
                raise FetchError("三所均断连")
            return super().fetch_lhb(trade_date)

    class IncompleteThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, trade_date):
            return [dict(EXCHANGE_LHB["lhb_basic"][0], trade_date=trade_date)]

    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[AllFailOnLookbackDay()], lhb_fallback=IncompleteThs(),
    )

    assert result["status"] == "error"
    assert result["results"] == []
    assert result["error_kind"] == "partial_error"
    assert _count(pg_ready, "lhb_basic") == 0
    assert _count(pg_ready, "lhb_stock_statistic") == 0


def test_fetch_lhb_backfill_ths_mixed_dates_rejected(pg_ready):
    official = {
        **EXCHANGE_LHB,
        "lhb_basic": [EXCHANGE_LHB["lhb_basic"][1]],
        "errors": ["szse: 深交所断连"],
    }

    class PartialOnTargetDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            payload = official if trade_date == "20261020" else EXCHANGE_LHB
            return FakeLhbAdapter(payload=payload).fetch_lhb(trade_date)

    class WrongDateThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, trade_date):
            return [
                dict(EXCHANGE_LHB["lhb_basic"][0], trade_date=trade_date),
                dict(EXCHANGE_LHB["lhb_basic"][1], trade_date="20261019"),
            ]

    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[PartialOnTargetDay()], lhb_fallback=WrongDateThs(),
    )

    assert result["status"] == "error"
    assert "日期不符" in result["results"][0]["error"]
    assert _count(pg_ready, "lhb_basic") == 0
    assert _count(pg_ready, "lhb_stock_statistic") == 0


def test_fetch_lhb_backfill_ths_merge_exception_is_structured(pg_ready):
    official = {
        **EXCHANGE_LHB,
        "lhb_basic": [EXCHANGE_LHB["lhb_basic"][1]],
        "errors": ["szse: 深交所断连"],
    }

    class PartialOnTargetDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            payload = official if trade_date == "20261020" else EXCHANGE_LHB
            return FakeLhbAdapter(payload=payload).fetch_lhb(trade_date)

    class UnexpectedCodeThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, trade_date):
            return [dict(EXCHANGE_LHB["lhb_basic"][0],
                         stock_code="900901", trade_date=trade_date)]

    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[PartialOnTargetDay()], lhb_fallback=UnexpectedCodeThs(),
    )

    assert result["status"] == "error"
    assert "后备合并异常" in result["results"][0]["error"]
    assert _count(pg_ready, "lhb_basic") == 0


def test_fetch_lhb_backfill_ths_exception_preserves_old_day(pg_ready):
    fetch_lhb_day("20261020", lhb_adapters=[FakeLhbAdapter()])
    official = {
        **EXCHANGE_LHB,
        "lhb_basic": [dict(EXCHANGE_LHB["lhb_basic"][1], buy_amount=1.0)],
        "errors": ["szse: 深交所断连"],
    }

    class PartialOnTargetDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            payload = official if trade_date == "20261020" else EXCHANGE_LHB
            return FakeLhbAdapter(payload=payload).fetch_lhb(trade_date)

    class BrokenThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, _trade_date):
            raise ValueError("异常 HTML")

    result = fetch_lhb_backfill(
        "20261020", "20261020",
        lhb_adapters=[PartialOnTargetDay()], lhb_fallback=BrokenThs(), force=True,
    )

    assert result["status"] == "error"
    assert "异常 HTML" in result["results"][0]["error"]
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT stock_code, buy_amount FROM lhb_basic ORDER BY stock_code")
            assert cur.fetchall() == [
                ("000530", pytest.approx(3601.5072)),
                ("600302", pytest.approx(293.5474)),
            ]
            cur.execute("SELECT COUNT(*) FROM lhb_stock_statistic")
            assert cur.fetchone()[0] == 2
    finally:
        conn.close()


def test_fetch_lhb_backfill_preserves_complete_official_day(pg_ready):
    fetch_lhb_day("20261020", lhb_adapters=[FakeLhbAdapter()])
    calls = []

    class Recorder(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            calls.append(trade_date)
            return super().fetch_lhb(trade_date)

    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[Recorder()],
    )

    assert result["status"] == "ok"
    assert result["results"][0]["preserved"] is True
    assert "20261020" not in calls
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT buy_amount FROM lhb_basic WHERE stock_code = '000530'")
            assert cur.fetchone()[0] == pytest.approx(3601.5072)
    finally:
        conn.close()


def test_fetch_lhb_backfill_preserves_confirmed_official_empty_day(pg_ready):
    empty = {table: [] for table in EXCHANGE_LHB}
    fetch_lhb_day("20261020", lhb_adapters=[FakeLhbAdapter(payload=empty)])
    calls = []

    class Recorder(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            calls.append(trade_date)
            return super().fetch_lhb(trade_date)

    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[Recorder()],
    )

    assert result["status"] == "ok"
    assert result["results"][0]["preserved"] is True
    assert result["results"][0]["empty_kind"] == "no_data_for_date"
    assert "20261020" not in calls


def test_fetch_lhb_backfill_does_not_preserve_partial_official_day(pg_ready):
    partial = {**EXCHANGE_LHB, "errors": ["sse: 上交所断连"]}
    fetch_lhb_day("20261020", lhb_adapters=[FakeLhbAdapter(payload=partial)])
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail FROM collection_log WHERE trade_date = '20261020'"
                " AND section = 'lhb' ORDER BY id DESC LIMIT 1"
            )
            assert "partial_error" in cur.fetchone()[0]
    finally:
        conn.close()
    calls = []

    class Recorder(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            calls.append(trade_date)
            return super().fetch_lhb(trade_date)

    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[Recorder()],
    )

    assert result["status"] == "ok"
    assert "preserved" not in result["results"][0]
    assert "20261020" in calls


def test_fetch_lhb_backfill_staging_refreshes_unknown_source(pg_ready):
    fetch_lhb_day("20261019", lhb_adapters=[FakeLhbAdapter()])
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE collection_log SET source = NULL"
                " WHERE trade_date = '20261019' AND section = 'lhb'"
            )
        conn.commit()
    finally:
        conn.close()
    calls = []

    class Recorder(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            calls.append(trade_date)
            return super().fetch_lhb(trade_date)

    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[Recorder()],
    )

    assert result["status"] == "ok"
    assert "20261019" in calls


def test_fetch_lhb_backfill_force_replaces_old_reasons_and_excludes_yyb(pg_ready):
    old = {
        **EXCHANGE_LHB,
        "lhb_basic": [dict(EXCHANGE_LHB["lhb_basic"][0], lhb_reason="旧来源原因")],
    }
    fetch_lhb_day("20261020", lhb_adapters=[FakeLhbAdapter(payload=old)])
    new = {
        **EXCHANGE_LHB,
        "lhb_basic": [dict(EXCHANGE_LHB["lhb_basic"][0], lhb_reason="交易所官方原因")],
        "lhb_yyb_capital": [{"rank": 1, "seat_name": "不应写入", "total_amount": 1.0}],
        "lhb_yyb_most": [{"rank": 1, "seat_name": "不应写入", "appear_count": 1}],
    }

    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[FakeLhbAdapter(payload=new)],
        force=True,
    )

    assert result["status"] == "ok"
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT lhb_reason FROM lhb_basic WHERE trade_date = '20261020'")
            assert cur.fetchall() == [("交易所官方原因",)]
    finally:
        conn.close()
    assert _count(pg_ready, "lhb_yyb_capital") == 0
    assert _count(pg_ready, "lhb_yyb_most") == 0


def test_fetch_lhb_backfill_partial_failure_preserves_old_day(pg_ready):
    fetch_lhb_day("20261020", lhb_adapters=[FakeLhbAdapter()])
    partial = {
        **EXCHANGE_LHB,
        "lhb_basic": [dict(EXCHANGE_LHB["lhb_basic"][0], lhb_reason="残缺新行", buy_amount=1.0)],
        "errors": ["sse: 单所失败"],
    }

    class PartialOnTargetDay(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            payload = partial if trade_date == "20261020" else EXCHANGE_LHB
            return FakeLhbAdapter(payload=payload).fetch_lhb(trade_date)

    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[PartialOnTargetDay()], force=True,
    )

    assert result["status"] == "error"
    assert result["results"][0]["status"] == "error"
    assert "单所失败" in result["results"][0]["error"]
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT lhb_reason, buy_amount FROM lhb_basic ORDER BY stock_code")
            basics = cur.fetchall()
            cur.execute("SELECT buy_amount_3m FROM lhb_stock_statistic ORDER BY stock_code")
            statistics = cur.fetchall()
    finally:
        conn.close()
    assert basics == [
        ("日价格振幅达到18.39%", pytest.approx(3601.5072)),
        ("一、涨幅偏离值达7%的前五只证券", pytest.approx(293.5474)),
    ]
    assert statistics == [(pytest.approx(3601.5072),), (pytest.approx(293.5474),)]


def test_fetch_lhb_backfill_single_day_failure_continues(pg_ready):
    class Flaky(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            if trade_date == "20261019":
                raise FetchError("单日 boom")  # 每次调用都失败（含重试）
            return super().fetch_lhb(trade_date)

    r = fetch_lhb_backfill("2026-10-16", "2026-10-20",
                           lhb_adapters=[Flaky()])
    assert r["status"] == "partial_error" and r["failed"] == 1 and r["succeeded"] == 2
    by_day = {x["trade_date"]: x for x in r["results"]}
    assert by_day["20261019"]["status"] == "error"
    assert _count(pg_ready, "lhb_basic") == 2 * 2  # 失败日不落库，其余保留


def test_fetch_lhb_backfill_staging_reuses_complete_existing_day(pg_ready):
    fetch_lhb_day("20261019", lhb_adapters=[FakeLhbAdapter()])
    calls = []

    class Recorder(FakeLhbAdapter):
        def fetch_lhb(self, trade_date):
            calls.append(trade_date)
            return super().fetch_lhb(trade_date)

    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[Recorder()],
    )

    assert result["status"] == "ok"
    assert "20261019" not in calls
    assert "20261020" in calls
    assert _count(pg_ready, "lhb_basic") == 4


def test_fetch_lhb_backfill_stages_lookback_without_writing_outside_window(pg_ready):
    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[FakeLhbAdapter()]
    )

    assert result["status"] == "ok"
    assert result["statistic_lookback"] == {"start": "20260821", "days": 60}
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM lhb_basic")
            assert cur.fetchone() == ("20261020", "20261020", 2)
            cur.execute(
                "SELECT stock_code, appear_count_3m FROM lhb_stock_statistic"
                " WHERE trade_date = '20261020' ORDER BY stock_code"
            )
            assert cur.fetchall() == [("000530", 37), ("600302", 37)]
    finally:
        conn.close()


def test_fetch_lhb_backfill_does_not_double_count_existing_lookback_rows(pg_ready):
    fetch_lhb_day("20261019", lhb_adapters=[FakeLhbAdapter()])

    result = fetch_lhb_backfill(
        "20261020", "20261020", lhb_adapters=[FakeLhbAdapter()]
    )

    assert result["status"] == "ok"
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, appear_count_3m FROM lhb_stock_statistic"
                " WHERE trade_date = '20261020' ORDER BY stock_code"
            )
            assert cur.fetchall() == [("000530", 37), ("600302", 37)]
            cur.execute("SELECT COUNT(*) FROM lhb_basic WHERE trade_date < '20261020'")
            assert cur.fetchone()[0] == 2  # 既有窗前行保留，但未被重复写入
    finally:
        conn.close()


def test_fetch_lhb_backfill_bad_range(pg_ready):
    assert fetch_lhb_backfill("2026-10-20", "2026-10-16")["status"] == "error"
    assert fetch_lhb_backfill("2026-10-24", "2026-10-25")["status"] == "error"  # 周末区间无交易日


# ---------------------------------------------------------------- 对拍


def _sina_rows():
    return [
        {"stock_code": "000530", "stock_name": "冰山冷热", "close_price": 12.3,
         "amount_wan": 204283.8632, "volume_wan": 26651.62,
         "indicator": "振幅值达15%的证券", "seat_type": "03"},
        {"stock_code": "600302", "stock_name": "ST标准", "close_price": 9.93,
         "amount_wan": 4461.78, "volume_wan": 672.121,
         "indicator": "涨幅偏离值达7%的证券", "seat_type": "01"},
        {"stock_code": "000056", "stock_name": "*ST皇庭", "close_price": 2.09,
         "amount_wan": 13900.59, "volume_wan": 6612.81,
         "indicator": "跌幅偏离值达7%的证券", "seat_type": "02"},
    ]


def test_parity_match_and_set_diff(pg_ready):
    r = compare_lhb_sources(TODAY, exchange=FakeLhbAdapter(),
                            sina=_matched_sina())
    assert r["status"] == "ok" and r["mismatch_count"] == 0
    # 集合差双向报告（信息级）：交易所缺 000056（新浪独有，不算失败）
    assert r["stock_set_diff"]["sina_only"] == ["000056"]
    assert r["stock_set_diff"]["exchange_only"] == []
    codes = {x["stock_code"]: x for x in r["results"]}
    assert codes["000530"]["status"] == "ok"
    assert codes["600302"]["status"] == "ok"
    # results 携带双方原值供人工核对
    assert codes["000530"]["exchange"]["net_buy_amount"] == pytest.approx(-1567.4644)
    # 净买额对拍：新浪席位 JSONP 按 (code, seat_type) 调用
    sina = _matched_sina()
    compare_lhb_sources(TODAY, exchange=FakeLhbAdapter(), sina=sina)
    assert ("000530", "03") in sina.seat_calls and ("600302", "01") in sina.seat_calls


def test_parity_mismatch_on_amount(pg_ready):
    rows = _sina_rows()
    rows[0] = {**rows[0], "amount_wan": 999999.0}  # 成交额对不上
    r = compare_lhb_sources(TODAY, exchange=FakeLhbAdapter(),
                            sina=FakeSina(rows=rows, seat_nets={"000530": -1567.4644, "600302": -58.71}))
    codes = {x["stock_code"]: x for x in r["results"]}
    assert codes["000530"]["status"] == "mismatch"
    assert codes["000530"]["diffs"][0]["field"] == "day_amount"
    assert codes["600302"]["status"] == "ok"  # 净额对齐后其余股不误报
    assert r["status"] == "mismatch" and r["mismatch_count"] == 1


def test_parity_mismatch_on_net_buy(pg_ready):
    r = compare_lhb_sources(
        TODAY, exchange=FakeLhbAdapter(),
        sina=FakeSina(rows=_sina_rows(), seat_nets={"000530": 999.0, "600302": -58.71}),
    )
    codes = {x["stock_code"]: x for x in r["results"]}
    assert codes["000530"]["status"] == "mismatch"
    assert codes["000530"]["diffs"][0]["field"] == "net_buy_amount"
    assert r["mismatch_count"] == 1


def test_parity_single_side_failure_is_not_overall_error(pg_ready):
    r = compare_lhb_sources(
        TODAY, exchange=FakeLhbAdapter(),
        sina=FakeSina(exc=FetchError("sina boom")),
    )
    assert r["status"] == "ok"  # 单侧失败可人工核对另一侧，不算对拍失败
    assert r["sina"]["status"] == "error"
    assert all(x["status"] == "single_source" for x in r["results"])


def test_parity_both_sides_failed_is_error(pg_ready):
    r = compare_lhb_sources(
        TODAY,
        exchange=FakeLhbAdapter(exc=FetchError("ex boom")),
        sina=FakeSina(exc=FetchError("sina boom")),
    )
    assert r["status"] == "error"


def test_parity_seat_failure_only_notes(pg_ready):
    r = compare_lhb_sources(
        TODAY, exchange=FakeLhbAdapter(),
        sina=FakeSina(rows=_sina_rows(), seat_exc=FetchError("seat boom")),
    )
    assert r["status"] == "ok"
    codes = {x["stock_code"]: x for x in r["results"]}
    assert "seat boom" in codes["000530"]["sina"]["seat_note"]


# ---------------------------------------------------------------- CLI


def _run(capsys, argv, lhb_adapters=None):
    rc = scheduler.main(argv, lhb_adapters=lhb_adapters)
    out = json.loads(capsys.readouterr().out)
    return rc, out


class TestLhbCli:
    def test_fetch_mode_exit_0(self, pg_ready, capsys):
        rc, out = _run(capsys, ["lhb", "--date", TODAY],
                       lhb_adapters=[FakeLhbAdapter()])
        assert rc == 0 and out["job"] == "lhb" and out["mode"] == "fetch"
        assert out["lhb"]["status"] == "ok"
        assert _count(pg_ready, "lhb_basic") == 2

    def test_fetch_mode_failure_exit_1(self, pg_ready, capsys):
        rc, out = _run(capsys, ["lhb", "--date", TODAY],
                       lhb_adapters=[FakeLhbAdapter(exc=FetchError("boom"))])
        assert rc == 1 and out["lhb"]["status"] == "error"

    def test_backfill_mode(self, pg_ready, capsys):
        rc, out = _run(
            capsys, ["lhb", "--start", "2026-10-16", "--end", "2026-10-20"],
            lhb_adapters=[FakeLhbAdapter()],
        )
        assert rc == 0 and out["mode"] == "backfill" and out["total_days"] == 3

    def test_backfill_reports_staging_and_target_phases(self, pg_ready, capsys):
        rc = scheduler.main(
            ["lhb", "--start", "2026-10-16", "--end", "2026-10-20"],
            lhb_adapters=[FakeLhbAdapter()],
        )
        captured = capsys.readouterr()

        assert rc == 0
        assert json.loads(captured.out)["total_days"] == 3
        columns = [line.split(" | ") for line in captured.err.splitlines()]
        assert all(len(parts) == 4 for parts in columns)
        for timestamp, _task, _event, context in columns:
            assert datetime.fromisoformat(timestamp).tzinfo is not None
            assert context
        assert {parts[1] for parts in columns} == {"lhb.staging", "lhb.backfill"}
        events = [parts[2] for parts in columns]
        assert events.count("start") == 2
        assert events.count("done") == 2
        assert "3/3" in events

    def test_backfill_force(self, pg_ready, capsys):
        rc, out = _run(
            capsys,
            ["lhb", "--start", "20261020", "--end", "20261020", "--force"],
            lhb_adapters=[FakeLhbAdapter()],
        )
        assert rc == 0 and out["params"]["force"] is True

    def test_backfill_fallback_ths_is_explicit(self, pg_ready, capsys, monkeypatch):
        official = {
            **EXCHANGE_LHB,
            "lhb_basic": [EXCHANGE_LHB["lhb_basic"][1]],
            "errors": ["szse: 深交所断连"],
        }

        class PartialOnTargetDay(FakeLhbAdapter):
            def fetch_lhb(self, trade_date):
                payload = official if trade_date == "20261020" else EXCHANGE_LHB
                return FakeLhbAdapter(payload=payload).fetch_lhb(trade_date)

        class FakeThs:
            name = "ths_fake"

            def fetch_lhb_basic(self, trade_date):
                return [dict(EXCHANGE_LHB["lhb_basic"][0], trade_date=trade_date)]

        monkeypatch.setattr("qstock_mcp.tools_lhb.ThsLhbAdapter", FakeThs)
        rc, out = _run(
            capsys,
            ["lhb", "--start", "20261020", "--end", "20261020", "--fallback", "ths"],
            lhb_adapters=[PartialOnTargetDay()],
        )

        assert rc == 0
        assert out["params"]["fallback"] == "ths"
        assert out["results"][0]["source"] == "exchange_fake+ths_fake"

    def test_backfill_default_does_not_enable_ths(self, pg_ready, capsys, monkeypatch):
        def unexpected_ths():
            raise AssertionError("默认历史回溯不应构造同花顺后备")

        monkeypatch.setattr("qstock_mcp.tools_lhb.ThsLhbAdapter", unexpected_ths)
        rc, out = _run(
            capsys, ["lhb", "--start", "20261020", "--end", "20261020"],
            lhb_adapters=[FakeLhbAdapter()],
        )
        assert rc == 0
        assert "fallback" not in out["params"]
        assert out["results"][0]["source"] == "exchange_fake"

    def test_fallback_is_rejected_outside_history(self, pg_ready, capsys):
        rc, out = _run(capsys, ["lhb", "--date", TODAY, "--fallback", "ths"])
        assert rc == 1
        assert "仅用于历史回溯" in out["error"]
        assert _count(pg_ready, "collection_log") == 0

    def test_force_is_rejected_outside_history(self, pg_ready, capsys):
        rc, out = _run(
            capsys, ["lhb", "--date", TODAY, "--force"],
            lhb_adapters=[FakeLhbAdapter()],
        )
        assert rc == 1
        assert "仅用于历史回溯" in out["error"]
        assert _count(pg_ready, "collection_log") == 0

    def test_force_is_rejected_in_check_mode(self, pg_ready, capsys, monkeypatch):
        monkeypatch.setattr(
            scheduler, "compare_lhb_sources", lambda trade_date: {"status": "ok"}
        )
        rc, out = _run(
            capsys,
            ["lhb", "--start", "20261020", "--end", "20261020", "--check", "--force"],
            lhb_adapters=[FakeLhbAdapter()],
        )
        assert rc == 1
        assert "不用于 --check" in out["error"]

    def test_backfill_trading_day_window(self, pg_ready, capsys):
        rc, out = _run(
            capsys, ["lhb", "--trading-days", "3", "--end", "20261020"],
            lhb_adapters=[FakeLhbAdapter()],
        )
        assert rc == 0
        assert out["params"] == {"start": "20261016", "end": "20261020"}
        assert out["trading_days"] == 3
        assert out["total_days"] == 3

    def test_backfill_mode_partial_exit_1(self, pg_ready, capsys):
        class Flaky(FakeLhbAdapter):
            def fetch_lhb(self, trade_date):
                if trade_date == "20261016":
                    raise FetchError("boom")  # 每次调用都失败（含重试）
                return super().fetch_lhb(trade_date)

        rc, out = _run(
            capsys, ["lhb", "--start", "2026-10-16", "--end", "2026-10-20"],
            lhb_adapters=[Flaky()],
        )
        assert rc == 1 and out["failed"] == 1

    def test_check_mode_no_db_write(self, pg_ready, capsys, monkeypatch):
        monkeypatch.setattr(
            "qstock_mcp.tools_lhb.SinaLhbClient", lambda: _matched_sina()
        )
        # --check 路径用真实 ExchangeLhbAdapter 构造——换成 fake 走模块级注入
        from qstock_mcp.tools_lhb import compare_lhb_sources as cmp_fn
        monkeypatch.setattr(
            "qstock_mcp.scheduler.compare_lhb_sources",
            lambda trade_date=None: cmp_fn(
                trade_date, exchange=FakeLhbAdapter(), sina=_matched_sina()),
        )
        rc, out = _run(capsys, ["lhb", "--date", TODAY, "--check"])
        assert rc == 0 and out["mode"] == "check"
        assert _count(pg_ready, "lhb_basic") == 0  # 对拍不落库
        assert _count(pg_ready, "collection_log") == 0

    def test_date_and_start_end_mutually_exclusive(self, pg_ready, capsys):
        rc, out = _run(capsys, ["lhb", "--date", TODAY, "--start", "2026-10-16",
                                "--end", "2026-10-20"])
        assert rc == 1 and "互斥" in out["error"]


def test_parity_skips_day_amount_for_cumulative_reason(pg_ready):
    """累计类情形（异常期间/三日累计）：交易所 day_amount 为单日额、新浪为期间
    累计（实测 ~3 倍差），口径不同跳过比对（note 标注），不算 mismatch。"""
    payload = {
        **EXCHANGE_LHB,
        "lhb_basic": [
            {
                "trade_date": TODAY, "stock_code": "001216", "stock_name": "示例",
                "close_price": None, "change_percent": None, "turnover_rate": None,
                "lhb_reason": "异常期间价格涨跌幅偏离值累计达到-40.01%",
                "net_buy_amount": -1567.4644, "buy_amount": 3601.5072,
                "sell_amount": 5168.9716, "total_amount": 8770.4788,
                "day_amount": 114752.122,  # 单日额（新浪同股为期间累计 ~31 万万）
            }
        ],
    }
    sina = FakeSina(
        rows=[{"stock_code": "001216", "stock_name": "示例", "close_price": 10.0,
               "amount_wan": 313334.9917, "volume_wan": 1000.0,
               "indicator": "连续三个交易日内，跌幅偏离值累计达20%的证券",
               "seat_type": "05"}],
        seat_nets={"001216": -1567.4644},
    )
    r = compare_lhb_sources(TODAY, exchange=FakeLhbAdapter(payload=payload), sina=sina)
    assert r["status"] == "ok" and r["mismatch_count"] == 0
    (entry,) = r["results"]
    assert "day_amount_note" in entry


def test_parity_multi_reason_code_noted_not_mismatch(pg_ready):
    """多榜单同股（双方各按首行归并，席位可能属不同榜单）：展示原值但不计差异。"""
    base = EXCHANGE_LHB["lhb_basic"][0]
    multi = {
        **EXCHANGE_LHB,
        "lhb_basic": [
            dict(base, stock_code="002990", lhb_reason="日价格振幅达到20.33%"),
            dict(base, stock_code="002990", lhb_reason="日换手率达到38.59%",
                 net_buy_amount=999.0),
        ],
    }
    sina = FakeSina(
        rows=[{"stock_code": "002990", "stock_name": "示例", "close_price": 10.0,
               "amount_wan": 999999.0, "volume_wan": 1.0,  # 金额故意不符
               "indicator": "振幅值达15%的证券", "seat_type": "04"}],
        seat_nets={"002990": 12345.0},  # 净额也故意不符
    )
    r = compare_lhb_sources(TODAY, exchange=FakeLhbAdapter(payload=multi), sina=sina)
    assert r["status"] == "ok" and r["mismatch_count"] == 0
    (entry,) = r["results"]
    assert entry["status"] == "ok" and "多榜单同股" in entry["note"]
    assert entry["exchange"]["net_buy_amount"] == pytest.approx(-1567.4644)  # 首行原值仍在


# ---------------------------------------------------------------- statistic 库内自算


def test_statistic_selfcalc_rolling_window(pg_ready):
    """自算口径（issue #114 定稿）：近 60 日历日窗口（含当日）按股聚合——
    appear=窗口行数（同股当日多榜单各计一次），金额=窗口合计；回溯逐日快照
    各自独立；重跑幂等（DELETE+INSERT 自愈）。"""
    r = fetch_lhb_backfill("2026-08-03", "2026-08-07",
                           lhb_adapters=[FakeLhbAdapter()])
    assert r["status"] == "ok" and r["total_days"] == 5
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, stock_code, appear_count_3m"
                " FROM lhb_stock_statistic ORDER BY trade_date, stock_code"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    # staging 提供完整前置；周末没有旧行滑出，后两日窗口行数相应增加。
    assert len(rows) == 5 * 2  # 5 日 × 2 股
    by_day = {}
    for d, code, n in rows:
        by_day.setdefault(d, {})[code] = n
    expected = dict(zip(
        ["20260803", "20260804", "20260805", "20260806", "20260807"],
        [42, 42, 42, 43, 44],
    ))
    for d, count in expected.items():
        assert by_day[d] == {"000530": count, "600302": count}
    # 重跑末日：快照行数不变、计数不变（幂等自愈）
    fetch_lhb_day("2026-08-07", lhb_adapters=[FakeLhbAdapter()])
    assert _count(pg_ready, "lhb_stock_statistic") == 10
    assert _count(pg_ready, "lhb_basic") == 10


def test_statistic_selfcalc_window_excludes_old_history(pg_ready):
    """窗口滑出：早于 [交易日-60 日历日] 的上榜记录不入当日快照。"""
    fetch_lhb_day("20260820", lhb_adapters=[FakeLhbAdapter()])  # >TODAY-60 之外
    fetch_lhb_day(TODAY, lhb_adapters=[FakeLhbAdapter()])
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, appear_count_3m FROM lhb_stock_statistic"
                " WHERE stock_code = '000530' ORDER BY trade_date"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    assert rows == [("20260820", 1), (TODAY, 1)]  # 0820 行不入 TODAY 窗口


def test_statistic_selfcalc_multi_reason_counts_each_row(pg_ready):
    """业务键含 lhb_reason：同股当日多榜单，appear 按行数各计一次。"""
    base = EXCHANGE_LHB["lhb_basic"][0]
    multi = {
        **EXCHANGE_LHB,
        "lhb_basic": [
            dict(base, lhb_reason="日价格振幅达到20.33%"),
            dict(base, lhb_reason="日换手率达到38.59%"),
            EXCHANGE_LHB["lhb_basic"][1],
        ],
    }
    fetch_lhb_day(TODAY, lhb_adapters=[FakeLhbAdapter(payload=multi)])
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, appear_count_3m, buy_amount_3m"
                " FROM lhb_stock_statistic ORDER BY stock_code"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    assert rows == [
        ("000530", 2, pytest.approx(3601.5072 * 2)),
        ("600302", 1, pytest.approx(293.5474)),
    ]


# ---------------------------------------------------------------- statistic 对拍（ggtj 校验位）


def _ggtj_rows():
    return [
        {"stock_code": "000530", "stock_name": "冰山冷热",
         "appear_count_3m": 1, "buy_amount_3m": 3601.5072,
         "sell_amount_3m": 5168.9716, "net_buy_3m": -1567.4644,
         "buy_seat_count": 2, "sell_seat_count": 2},
        {"stock_code": "600302", "stock_name": "ST标准",
         "appear_count_3m": 1, "buy_amount_3m": 293.5474,
         "sell_amount_3m": 352.2574, "net_buy_3m": -58.71,
         "buy_seat_count": 1, "sell_seat_count": 1},
        {"stock_code": "002281", "stock_name": "光迅科技",
         "appear_count_3m": 3, "buy_amount_3m": 912693.89,
         "sell_amount_3m": 533110.91, "net_buy_3m": 379582.98,
         "buy_seat_count": 6, "sell_seat_count": 5},
    ]


def test_statistic_parity_match_and_set_diff(pg_ready):
    """ggtj 校验位对拍：库内自算快照 vs 新浪 ggtj 逐股对照；集合差信息级。"""
    fetch_lhb_day(TODAY, lhb_adapters=[FakeLhbAdapter()])
    r = compare_lhb_sources(TODAY, exchange=FakeLhbAdapter(),
                            sina=FakeSina(ggtj_rows=_ggtj_rows()))
    st = r["statistic"]
    assert st["db"] == {"status": "ok", "row_count": 2}
    assert st["sina_ggtj"] == {"status": "ok", "row_count": 3}
    assert st["mismatch_count"] == 0 and r["status"] == "ok"
    # ggtj 有 002281 而库内无（该股未落库）→ sina_only 信息级，不算失败
    assert st["stock_set_diff"]["sina_only"] == ["002281"]
    assert st["stock_set_diff"]["db_only"] == []
    # 席位计数 ggtj 侧有值、自算恒 NULL：不参与比对（note 声明）
    assert "seat_count" in st["note"]


def test_statistic_parity_mismatch_counts_into_overall(pg_ready):
    fetch_lhb_day(TODAY, lhb_adapters=[FakeLhbAdapter()])
    ggtj = _ggtj_rows()
    ggtj[0] = {**ggtj[0], "appear_count_3m": 2}  # 上榜次数不符
    r = compare_lhb_sources(TODAY, exchange=FakeLhbAdapter(),
                            sina=FakeSina(ggtj_rows=ggtj))
    assert r["statistic"]["mismatch_count"] == 1
    assert r["status"] == "mismatch"  # 整体状态吸收 statistic 差异
    (entry,) = [x for x in r["statistic"]["results"]
                if x["stock_code"] == "000530"]
    assert entry["diffs"][0]["field"] == "appear_count_3m"


def test_statistic_parity_ggtj_failure_not_overall_error(pg_ready):
    """校验位单侧失败只记该侧（与 basic 对拍同构），不判整体失败。"""
    r = compare_lhb_sources(TODAY, exchange=FakeLhbAdapter(),
                            sina=FakeSina(ggtj_exc=FetchError("ggtj boom")))
    assert r["statistic"]["sina_ggtj"]["status"] == "error"
    assert r["status"] == "ok"
