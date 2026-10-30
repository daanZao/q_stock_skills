"""指数日线固定入口与覆盖核验（issue #117，spec #111）工具层测试。

覆盖：fetch_index_backfill（腾讯→baostock 链、逐指数独立成败、幂等、台账、
盘中 defer）、verify_index_coverage（交易日历缺口定位）、CLI cmd_index
（fetch 落库 / --verify 纯读）。fake 适配器注 tools_* 层，真实 PG 测试库
验证落库；不触网。
"""

import json
from datetime import datetime, timedelta, timezone

import psycopg2

from qstock_mcp import scheduler
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import select_index_daily, upsert_index_daily
from qstock_mcp.tools_index import fetch_index_backfill, verify_index_coverage

_TZ8 = timezone(timedelta(hours=8))
# 2026-10-22（周四）15:00 后：非盘中，当日行不被 defer
NOW_AFTER_CLOSE = datetime(2026, 10, 22, 16, 0, tzinfo=_TZ8)
# 2026-10-22（周四）10:00：盘中，当日行 defer
NOW_INTRADAY = datetime(2026, 10, 22, 10, 0, tzinfo=_TZ8)

KC50_ROWS = [
    {
        "trade_date": d, "open": 1400.0, "high": 1450.0, "low": 1380.0,
        "close": 1420.0, "volume": 7_000_000, "amount": 7_000_000.0,
        "amplitude": 4.9, "change_percent": 0.5, "change_amount": 7.0,
        "turnover_rate": 1.1,
    }
    for d in ("20261013", "20261014", "20261015", "20261016",
              "20261019", "20261020", "20261021", "20261022")
]


class FakeIndexDailyAdapter:
    """指数日线 fake 源：rows 为已映射 bar（code → 行），按脚本成败。"""

    def __init__(self, name, *, rows=None, exc=None):
        self.name = name
        self._rows = rows or {}
        self._exc = exc
        self.calls: list[tuple[str, str, str]] = []

    def fetch_index_daily(self, index_code, start, end):
        self.calls.append((index_code, start, end))
        if self._exc is not None:
            raise self._exc
        return [dict(r) for r in self._rows.get(index_code, [])
                if start <= r["trade_date"] <= end]


def _conn(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    return conn


def _ledger_rows(pg_test):
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT section, status, source, rows FROM collection_log"
            " ORDER BY id"
        )
        rows = cur.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------- 回填（fetch）


def test_backfill_kc50_gap_lands_and_is_idempotent(pg_test):
    """科创50 断档回填：腾讯 fake 供 10-13~10-22 行，落库幂等，台账逐指数。"""
    conn = _conn(pg_test)
    conn.close()
    adapters = [FakeIndexDailyAdapter("tencent", rows={"000688": KC50_ROWS})]
    out = fetch_index_backfill(
        codes="000688", start="20261013", end="20261022",
        adapters=adapters, now=NOW_AFTER_CLOSE,
    )
    assert out["status"] == "ok"
    entry = out["indices"][0]
    assert entry["source"] == "tencent" and entry["rows"] == len(KC50_ROWS)
    conn = psycopg2.connect(pg_test)
    landed = select_index_daily(conn, "000688", "20261013", "20261022")
    conn.close()
    assert [r["trade_date"] for r in landed] == [r["trade_date"] for r in KC50_ROWS]
    assert {r["source"] for r in landed} == {"tencent"}
    # 重复执行幂等（业务键 upsert，行数不变）
    again = fetch_index_backfill(
        codes="000688", start="20261013", end="20261022",
        adapters=adapters, now=NOW_AFTER_CLOSE,
    )
    assert again["status"] == "ok" and again["indices"][0]["rows"] == len(KC50_ROWS)
    ledger = _ledger_rows(pg_test)
    assert ledger == [
        ("index_daily", "ok", "tencent", len(KC50_ROWS)),
        ("index_daily", "ok", "tencent", len(KC50_ROWS)),
    ]


def test_backfill_preserves_rows_outside_requested_window(pg_test):
    conn = _conn(pg_test)
    outside = dict(KC50_ROWS[0], trade_date="20261012", close=999.0)
    upsert_index_daily(conn, "000688", [outside], "legacy")
    conn.close()

    out = fetch_index_backfill(
        codes="000688", start="20261013", end="20261022",
        adapters=[FakeIndexDailyAdapter("tencent", rows={"000688": KC50_ROWS})],
        now=NOW_AFTER_CLOSE,
    )

    assert out["status"] == "ok"
    conn = psycopg2.connect(pg_test)
    rows = select_index_daily(conn, "000688", "20261012", "20261012")
    conn.close()
    assert len(rows) == 1
    assert rows[0]["close"] == 999.0
    assert rows[0]["source"] == "legacy"


def test_backfill_falls_back_to_baostock_and_partial_failure(pg_test):
    """链序 腾讯→baostock：腾讯挂走 baostock；单指数失败不拖垮其他。"""
    conn = _conn(pg_test)
    conn.close()
    exc = FetchError("tencent boom")  # kind=None → other
    adapters = [
        FakeIndexDailyAdapter("tencent", exc=exc),
        FakeIndexDailyAdapter("baostock", rows={"000688": KC50_ROWS}),
    ]
    out = fetch_index_backfill(
        codes="000688", start="20261013", end="20261022",
        adapters=adapters, now=NOW_AFTER_CLOSE,
    )
    assert out["status"] == "ok"
    assert out["indices"][0]["source"] == "baostock"
    # 失败源按重试策略尝试 3 次（最多重试 2 次），成功后链终止
    assert len(adapters[0].calls) == 3 and len(adapters[1].calls) == 1

    out = fetch_index_backfill(
        codes="000688,000001", start="20261013", end="20261022",
        adapters=[FakeIndexDailyAdapter("tencent", exc=exc)],
        now=NOW_AFTER_CLOSE,
    )
    assert out["status"] == "error"  # 全指数失败
    assert all(i["status"] == "error" for i in out["indices"])


def test_backfill_intraday_defers_today_row(pg_test):
    """盘中禁写（issue #78）：交易时段内当日行不落库，deferred 计数报告。"""
    conn = _conn(pg_test)
    conn.close()
    adapters = [FakeIndexDailyAdapter("tencent", rows={"000688": KC50_ROWS})]
    out = fetch_index_backfill(
        codes="000688", start="20261013", end="20261022",
        adapters=adapters, now=NOW_INTRADAY,
    )
    assert out["status"] == "ok"
    assert out["indices"][0]["deferred"] == 1
    assert out["indices"][0]["rows"] == len(KC50_ROWS) - 1
    conn = psycopg2.connect(pg_test)
    landed = select_index_daily(conn, "000688", "20261013", "20261022")
    conn.close()
    assert "20261022" not in {r["trade_date"] for r in landed}


def test_backfill_default_codes_are_major_indices(pg_test):
    """缺省代码 = 7 大指数白名单（复盘清单逐一对照的载体）。"""
    conn = _conn(pg_test)
    conn.close()
    rows_by_code = {
        c: [dict(r, trade_date="20261021") for r in KC50_ROWS[:1]]
        for c, _ in (
            ("000001", ""), ("399001", ""), ("399006", ""), ("000300", ""),
            ("000016", ""), ("000905", ""), ("000688", ""),
        )
    }
    out = fetch_index_backfill(
        start="20261021", end="20261021",
        adapters=[FakeIndexDailyAdapter("tencent", rows=rows_by_code)],
        now=NOW_AFTER_CLOSE,
    )
    assert out["status"] == "ok"
    assert [i["index_code"] for i in out["indices"]] == [
        "000001", "399001", "399006", "000300", "000016", "000905", "000688",
    ]


def test_backfill_input_errors(pg_test):
    out = fetch_index_backfill(codes="000688", start="20261023", end="20261013")
    assert out["status"] == "error" and "晚于" in out["error"]
    out = fetch_index_backfill(codes="000688", start="bad-date")
    assert out["status"] == "error"


# ---------------------------------------------------------------- 覆盖核验


def test_verify_reports_gap_then_ok_after_backfill(pg_test):
    """核验：空库 → gap 且 missing_days 对照交易日历；回填后 → ok。"""
    conn = _conn(pg_test)
    conn.close()
    out = verify_index_coverage(codes="000688", start="20261013", end="20261022")
    assert out["status"] == "error"
    entry = out["indices"][0]
    assert entry["index_name"] == "科创50"
    assert entry["rows"] == 0
    # 10-13~10-22 的交易日历：13/14/15/16/19/20/21/22 共 8 天全缺
    assert entry["missing_days"] == [
        "20261013", "20261014", "20261015", "20261016",
        "20261019", "20261020", "20261021", "20261022",
    ]
    conn = _conn(pg_test)
    upsert_index_daily(conn, "000688", KC50_ROWS, "tencent")
    conn.close()
    out = verify_index_coverage(codes="000688", start="20261013", end="20261022")
    assert out["status"] == "ok"
    entry = out["indices"][0]
    assert entry["rows"] == len(KC50_ROWS)
    assert entry["last_date"] == "20261022"
    assert entry["sources"] == ["tencent"]
    assert entry["missing_days"] == []


# ---------------------------------------------------------------- CLI 入口


def _run(capsys, argv, index_adapters):
    rc = scheduler.main(argv, index_adapters=index_adapters)
    return rc, json.loads(capsys.readouterr().out)


def test_cli_index_trading_day_window(pg_test, capsys):
    conn = _conn(pg_test)
    conn.close()
    adapters = [FakeIndexDailyAdapter("tencent", rows={"000688": KC50_ROWS})]
    rc, out = _run(
        capsys,
        ["index", "--codes", "000688", "--trading-days", "3", "--end", "20261022"],
        adapters,
    )
    assert rc == 0
    assert out["range"] == {"start": "20261020", "end": "20261022"}
    assert out["trading_days"] == 3
    assert adapters[0].calls == [("000688", "20261020", "20261022")]


def test_cli_index_fetch_and_verify(pg_test, capsys):
    """CLI：index 抓取落库（exit 0）+ --verify 纯读核验（先 gap 后 ok）。"""
    conn = _conn(pg_test)
    conn.close()
    adapters = [FakeIndexDailyAdapter("tencent", rows={"000688": KC50_ROWS})]
    rc, out = _run(
        capsys,
        ["index", "--codes", "000688", "--start", "20261013", "--end", "20261022"],
        adapters,
    )
    assert rc == 0
    assert out["job"] == "index" and out["mode"] == "fetch"
    assert out["indices"][0]["rows"] == len(KC50_ROWS)
    conn = psycopg2.connect(pg_test)
    landed = select_index_daily(conn, "000688", "20261013", "20261022")
    conn.close()
    assert len(landed) == len(KC50_ROWS)

    rc, out = _run(
        capsys,
        ["index", "--codes", "000688", "--start", "20261013",
         "--end", "20261022", "--verify"],
        adapters,
    )
    assert rc == 0
    assert out["mode"] == "verify"
    assert out["indices"][0]["status"] == "ok"
    # verify 不写库：行数仍为回填值
    conn = psycopg2.connect(pg_test)
    landed = select_index_daily(conn, "000688", "20261013", "20261022")
    conn.close()
    assert len(landed) == len(KC50_ROWS)


def test_cli_index_verify_gap_exit_1(pg_test, capsys):
    conn = _conn(pg_test)
    conn.close()
    rc, out = _run(
        capsys,
        ["index", "--codes", "000688", "--start", "20261013",
         "--end", "20261022", "--verify"],
        [],
    )
    assert rc == 1
    assert out["indices"][0]["status"] == "gap"


def test_cli_index_fetch_failure_exit_1(pg_test, capsys):
    conn = _conn(pg_test)
    conn.close()
    rc, out = _run(
        capsys,
        ["index", "--codes", "000688", "--date", "20261022"],
        [FakeIndexDailyAdapter("tencent", exc=FetchError("boom"))],
    )
    assert rc == 1
    assert out["indices"][0]["status"] == "error"


def test_cli_index_bad_args_exit_1(capsys):
    rc, out = _run(
        capsys,
        ["index", "--codes", "000688", "--date", "20261022", "--start", "20261013"],
        [],
    )
    assert rc == 1 and "互斥" in out["error"]
