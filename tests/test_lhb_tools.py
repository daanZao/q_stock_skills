"""tools_lhb 核心层与 qstock-scheduler lhb 子命令测试（issue #114，spec #111）。

fake 适配器注 tools_* 层（既有接缝），真实 PG 测试库验落库；对拍以 fake
exchange/fake sina 驱动，不触网。CLI 测试走 scheduler.main 直调（同
test_scheduler.py 模式）。
"""

import json
from datetime import date

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
    "lhb_stock_statistic": [
        {
            "trade_date": TODAY, "stock_code": "002281", "stock_name": "光迅科技",
            "appear_count_3m": 3, "buy_amount_3m": 912693.89,
            "sell_amount_3m": 533110.91, "net_buy_3m": 379582.98,
            "buy_seat_count": 6, "sell_seat_count": 5,
        }
    ],
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
        # 行级 trade_date 以请求日为准（生产语义：上榜日=请求交易日；statistic
        # 快照日=采集执行日）——深度复制后盖戳，避免跨日用例互相污染
        return {
            table: [
                dict(row, trade_date=trade_date) if isinstance(row, dict) else row
                for row in rows
            ]
            for table, rows in self._payload.items()
            if isinstance(rows, list)
        }


class FakeSina:
    name = "sina_lhb_fake"

    def __init__(self, rows=None, exc=None, seat_exc=None, seat_nets=None):
        self._rows = rows if rows is not None else []
        self._exc = exc
        self._seat_exc = seat_exc
        # {code: 净买额万元}：脚本化席位净额，驱动 net_buy 对拍的 ok/diff 路径
        self._seat_nets = seat_nets or {}
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
    assert r["lhb"]["rows"] == 3  # basic 2 + statistic 1
    assert _count(pg_ready, "lhb_basic") == 2
    assert _count(pg_ready, "lhb_stock_statistic") == 1
    # 业务键含 lhb_reason：同股当日多榜单多行；day_amount 为适配器元数据键，
    # 非落库列（upsert 只写 LHB_BASIC_FIELDS，列不存在即证明被忽略）
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
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
    assert got == [
        ("000530", pytest.approx(3601.5072)),
        ("600302", pytest.approx(293.5474)),
    ]
    # 台账：lhb section 由 tools_board 内联记录
    assert ("lhb", "ok", "exchange_fake", 3) in _ledger(pg_ready)


def test_fetch_lhb_day_empty_is_ok_with_note(pg_ready):
    empty = {t: [] for t in EXCHANGE_LHB}
    r = fetch_lhb_day(TODAY, lhb_adapters=[FakeLhbAdapter(payload=empty)])
    assert r["lhb"]["status"] == "ok"
    assert r["lhb"]["empty_kind"] == "no_data_for_date"
    assert _count(pg_ready, "lhb_basic") == 0
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
        "lhb_stock_statistic": [],  # 子项失败：无行 + errors 记录原因
        "errors": ["lhb_stock_statistic: sina 改版"],
    }
    r = fetch_lhb_day(TODAY, lhb_adapters=[FakeLhbAdapter(payload=partial)])
    assert r["lhb"]["status"] == "ok"
    assert "sina 改版" in r["lhb"]["partial_error"]
    assert _count(pg_ready, "lhb_basic") == 2
    assert _count(pg_ready, "lhb_stock_statistic") == 0


# ---------------------------------------------------------------- backfill


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
    assert calls == ["20261016", "20261019", "20261020"]
    assert r["empty_days"] == 0
    # 逐日落库 + 逐日台账
    assert _count(pg_ready, "lhb_basic") == 2 * 3
    assert len([x for x in _ledger(pg_ready) if x[0] == "lhb"]) == 3


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
