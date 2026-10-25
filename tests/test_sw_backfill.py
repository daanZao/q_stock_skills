"""申万板块历史回补测试（issue #118，spec #111）：fake 申万适配器 + 真实 PG。

覆盖：申万双级板块清单驱动回补、回补行 source='swsresearch' 标注、业务键
幂等重跑、断点续跑（sw-backfill 台账前缀）、连续失败熔断、板块清单抓取
失败 error 契约、默认窗口 19990101 起（历史深至 1999，5/10 日涨跌数据
基础）、scheduler backfill --source sw 接线。
"""

import json
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from qstock_mcp import scheduler
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.ledger import record_collection
from qstock_mcp.tools_backfill import fetch_sw_boards_backfill

from fakes import FakeSwBoardsAdapter, SW_BOARD_LIST

_TZ8 = timezone(timedelta(hours=8))
END = "20261023"  # 周五
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=_TZ8)  # 周六


def _history(board, days):
    """构造某板块 window 内逐日历史行（trade_date 倒序生成后正序返回）。"""
    from fakes import _sw_history_row

    return [
        dict(
            _sw_history_row(day, close=1000.0 + i),
            board_type={1: "industry_l1", 2: "industry_l2"}[board["level"]],
            board_code=board["board_code"],
            board_name=board["board_name"],
        )
        for i, day in enumerate(days)
    ]


# 11 个交易日窗口（20261009~20261023，含周末跳过）
WINDOW_DAYS = [
    "20261009", "20261012", "20261013", "20261014", "20261015",
    "20261016", "20261019", "20261020", "20261021", "20261022",
    "20261023",
]


@pytest.fixture
def pg_ready(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()
    return pg_test


class _ScriptedSwAdapter(FakeSwBoardsAdapter):
    """按板块脚本返回历史行的 fake：history_map {board_code: rows}。"""

    def __init__(self, history_map=None, **kw):
        super().__init__(**kw)
        self._history_map = history_map or {}

    def fetch_sw_board_history(self, board, start, end):
        self.calls.append("history")
        if "history" in self._fail:
            raise FetchError(f"{self.name} history boom")
        return [dict(r) for r in self._history_map.get(board["board_code"], [])]


def _adapter_with_window(boards=None, days=None, **kw):
    boards = boards or SW_BOARD_LIST
    days = days or WINDOW_DAYS
    return _ScriptedSwAdapter(
        history_map={b["board_code"]: _history(b, days) for b in boards}, **kw)


def _board_rows(dsn, trade_date=None):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            if trade_date is None:
                cur.execute(
                    "SELECT board_type, board_code, board_name, latest_price,"
                    " source FROM market_boards ORDER BY board_code, trade_date")
            else:
                cur.execute(
                    "SELECT board_type, board_code, board_name, latest_price,"
                    " source FROM market_boards WHERE trade_date = %s"
                    " ORDER BY board_code", (trade_date,))
            return cur.fetchall()
    finally:
        conn.close()


def _ledger(dsn):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, source, rows, detail FROM collection_log"
                " WHERE section = 'market_boards' ORDER BY id")
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- 回补落库

def test_sw_backfill_lands_rows_with_source_tag(pg_ready):
    adapters = [_adapter_with_window()]
    out = fetch_sw_boards_backfill(
        start="20261001", end=END, adapters=adapters, now=NOW)
    assert out["status"] == "ok"
    assert out["fetch_source"] == "swsresearch"
    assert out["window"] == {"start": "20261001", "end": END}
    assert len(out["boards"]) == len(SW_BOARD_LIST)
    assert out["failed_boards"] == []
    rows = _board_rows(pg_ready)
    # 4 板块 × 11 交易日窗口内行（20261009 起）
    assert len(rows) == len(SW_BOARD_LIST) * len(WINDOW_DAYS)
    assert all(r[4] == "swsresearch" for r in rows)  # source 标注
    l2 = [r for r in rows if r[0] == "industry_l2"]
    assert l2  # 二级口径行落库
    # 台账逐板块 ok
    ledger = _ledger(pg_ready)
    assert len(ledger) == len(SW_BOARD_LIST)
    assert all(x[0] == "ok" and x[1] == "swsresearch" for x in ledger)
    assert all(x[3].startswith("sw-backfill 801") for x in ledger)


def test_sw_backfill_rerun_idempotent_and_resumes(pg_ready):
    adapters = [_adapter_with_window()]
    first = fetch_sw_boards_backfill(
        start="20261001", end=END, adapters=adapters, now=NOW)
    assert first["status"] == "ok"
    n_first = len(_board_rows(pg_ready))
    # 重跑：台账同窗口已 ok → 全部跳过，零抓取
    again = fetch_sw_boards_backfill(
        start="20261001", end=END, adapters=adapters, now=NOW)
    assert again["status"] == "ok"
    assert sorted(again["skipped_boards"]) == sorted(
        b["board_name"] for b in SW_BOARD_LIST)
    assert len(_board_rows(pg_ready)) == n_first  # 幂等
    assert adapters[0].calls.count("history") == len(SW_BOARD_LIST)  # 只抓一轮


def test_sw_backfill_default_window_from_1999(pg_ready):
    """默认窗口 19990101 起：历史深度直达 1999（5/10 日涨跌数据基础）。"""
    adapters = [_adapter_with_window()]
    out = fetch_sw_boards_backfill(end=END, adapters=adapters, now=NOW)
    assert out["window"]["start"] == "19990101"
    assert out["status"] == "ok"


def test_sw_backfill_five_ten_day_foundation(pg_ready):
    """回补 ≥11 个交易日后，板块逐日历史可支撑 5/10 日涨跌（AC2 数据基础）。"""
    adapters = [_adapter_with_window()]
    fetch_sw_boards_backfill(
        start="20261001", end=END, adapters=adapters, now=NOW)
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT board_code, count(DISTINCT trade_date)"
                " FROM market_boards GROUP BY board_code")
            for _code, n in cur.fetchall():
                assert n == len(WINDOW_DAYS)  # 每板块 11 个交易日全覆盖
    finally:
        conn.close()


def test_sw_backfill_board_list_failure_is_error(pg_ready):
    adapters = [FakeSwBoardsAdapter(fail=("board_list",))]
    out = fetch_sw_boards_backfill(end=END, adapters=adapters, now=NOW)
    assert out["status"] == "error"
    assert "板块清单" in out["error"]
    assert _board_rows(pg_ready) == []
    ledger = _ledger(pg_ready)
    assert ledger[-1][0] == "error"


def test_sw_backfill_circuit_breaker(pg_ready):
    """连续 CIRCUIT_BREAKER_CONSECUTIVE_FAILURES 板全源失败 → 熔断中止。"""
    boards = [
        {"board_code": f"8019{i:02d}", "board_name": f"测试{i}", "level": 1}
        for i in range(12)
    ]
    adapters = [_adapter_with_window(boards=boards, fail=("history",))]
    out = fetch_sw_boards_backfill(
        start="20261001", end=END, adapters=adapters, boards=boards, now=NOW)
    assert out["status"] == "error"
    assert out["aborted"] is True
    assert "熔断" in out["abort_reason"]
    assert len(out["boards"]) == 10  # 熔断于第 10 板
    ledger = _ledger(pg_ready)
    assert ledger[-1][3].startswith("sw-backfill aborted")


def test_sw_backfill_single_board_failure_isolated(pg_ready):
    """单板块失败不中断其余板块（断点续跑只补失败板）。"""
    boards = list(SW_BOARD_LIST)
    adapters = [_adapter_with_window(fail=())]
    # 第二个板块历史抓取失败
    real = adapters[0].fetch_sw_board_history

    def _partial(board, start, end):
        if board["board_code"] == boards[1]["board_code"]:
            raise FetchError("trend boom")
        return real(board, start, end)

    adapters[0].fetch_sw_board_history = _partial
    out = fetch_sw_boards_backfill(
        start="20261001", end=END, adapters=adapters, now=NOW)
    assert out["status"] == "partial_error"
    assert out["failed_boards"] == [boards[1]["board_name"]]
    ok_codes = {
        b["board_code"] for b in boards if b["board_code"] != boards[1]["board_code"]}
    landed = {r[1] for r in _board_rows(pg_ready)}
    assert ok_codes <= landed
    # 续跑：失败板重抓成功则补齐
    adapters2 = [_adapter_with_window()]
    out2 = fetch_sw_boards_backfill(
        start="20261001", end=END, adapters=adapters2, now=NOW)
    assert out2["status"] == "ok"
    assert len(_board_rows(pg_ready)) == len(boards) * len(WINDOW_DAYS)


# ---------------------------------------------------------------- scheduler 接线

def test_cli_backfill_source_sw(pg_ready, capsys, monkeypatch):
    monkeypatch.setattr(
        "qstock_mcp.adapters.sw_adapter.SwSResearchAdapter",
        lambda *a, **k: _adapter_with_window(),
    )
    rc = scheduler.main(["backfill", "--source", "sw",
                         "--start", "20261001", "--end", END])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["job"] == "backfill"
    assert out["source"] == "sw"
    assert out["status"] == "ok"
    assert len(_board_rows(pg_ready)) == len(SW_BOARD_LIST) * len(WINDOW_DAYS)


def test_cli_backfill_default_is_sw(pg_ready, capsys, monkeypatch):
    """--source 缺省 = sw（issue #121 定稿）：走申万回补入口，不再回落东财。"""
    monkeypatch.setattr(
        "qstock_mcp.adapters.sw_adapter.SwSResearchAdapter",
        lambda *a, **k: _adapter_with_window(),
    )
    rc = scheduler.main(["backfill", "--start", "20261001", "--end", END])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["job"] == "backfill"
    assert out["source"] == "sw"
    assert len(_board_rows(pg_ready)) == len(SW_BOARD_LIST) * len(WINDOW_DAYS)


def test_cli_backfill_source_em_rejected(pg_ready, capsys):
    """--source em 已随 issue #121 摘除：argparse 参数错误 exit 2。"""
    with pytest.raises(SystemExit) as ei:
        scheduler.main(["backfill", "--source", "em"])
    assert ei.value.code == 2
