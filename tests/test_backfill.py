"""backfill 子命令与概念成分积累测试（issue #84，spec #71/W5）。

backfill：push2his 板块 K 线同口径回补行业+概念板块历史入 market_boards
（CLI 专用路径，不经 fetch 工具的历史日期防线）；回补行带 source='push2his'
标注，与实时采集行（source 为 NULL）可区分；业务键幂等可重跑。
概念成分积累：东财健康时概念板块成分（cons）积累入成分字典
（sw_industry_class，source_system='em'），独立成败落台账、不拖垮主批。
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from qstock_mcp import scheduler
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import BOARD_SECTION_TABLES, upsert_board_rows
from qstock_mcp.tools_backfill import accumulate_em_concepts, fetch_boards_backfill
from fakes import FakeBoardAdapter, FakeBoardHistoryAdapter, _board_history_row

_TZ8 = timezone(timedelta(hours=8))

END = "20261016"  # 周五
NOW = datetime(2026, 10, 19, 8, 30, tzinfo=_TZ8)  # 周一开盘前

BOARDS = [
    {"board_type": "industry", "board_code": "BK0477", "board_name": "酿酒行业"},
    {"board_type": "concept", "board_code": "BK0816", "board_name": "白酒概念"},
]


@pytest.fixture
def pg_ready(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()
    return pg_test


def _seed_realtime_boards(dsn, trade_date=END):
    """模拟实时采集落库（无 source 标注）的板块行。"""
    table, date_col, key_cols, fields = BOARD_SECTION_TABLES["boards"][0]
    conn = psycopg2.connect(dsn)
    try:
        upsert_board_rows(
            conn, table, date_col, key_cols, fields, trade_date,
            [dict(b, latest_price=2000.0, change_percent=1.5) for b in BOARDS],
        )
    finally:
        conn.close()


def _board_rows(dsn, trade_date):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT board_type, board_name, latest_price, source"
                " FROM market_boards WHERE trade_date = %s ORDER BY board_name",
                (trade_date,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _ledger(dsn, section):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, status, source, rows, error_kind, detail"
                " FROM collection_log WHERE section = %s ORDER BY id",
                (section,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _cons_rows(dsn):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_system, stock_code, board_code, effective_date"
                " FROM sw_industry_class ORDER BY stock_code"
            )
            return cur.fetchall()
    finally:
        conn.close()


def _hist(adapter_boards=BOARDS, days=("20261013", "20261014", "20261015")):
    return {
        (b["board_type"], b["board_name"]): [_board_history_row(d) for d in days]
        for b in adapter_boards
    }


class TestBackfill:
    def test_happy_path_rows_land_with_source(self, pg_ready):
        _seed_realtime_boards(pg_ready)
        out = fetch_boards_backfill(
            start="20261013", end="20261015",
            adapters=[FakeBoardHistoryAdapter(history=_hist())], now=NOW)
        assert out["status"] == "ok"
        assert out["window"] == {"start": "20261013", "end": "20261015"}
        assert len(out["boards"]) == 2
        assert all(b["status"] == "ok" and b["rows"] == 3 for b in out["boards"])
        for d in ("20261014", "20261015"):
            rows = _board_rows(pg_ready, d)
            assert len(rows) == 2
            assert all(r[3] == "push2his" for r in rows)  # 回补行带 source 标注
        # 实时采集行 source 为 NULL，与回补行可区分（END 日未在回补窗口内）
        realtime = _board_rows(pg_ready, END)
        assert [r[3] for r in realtime] == [None, None]

    def test_default_window_three_months(self, pg_ready):
        _seed_realtime_boards(pg_ready)
        fake = FakeBoardHistoryAdapter()
        fetch_boards_backfill(adapters=[fake], now=NOW)
        _, _, start, end = fake.history_calls[0]
        assert end == END  # 默认 end = 前一交易日
        assert start == "20260718"  # 约 3 个月（90 天）前

    def test_rerun_idempotent(self, pg_ready):
        _seed_realtime_boards(pg_ready)
        for _ in range(2):
            fetch_boards_backfill(
                start="20261013", end="20261015",
                adapters=[FakeBoardHistoryAdapter(history=_hist())], now=NOW)
        assert len(_board_rows(pg_ready, "20261014")) == 2

    def test_per_board_failure_isolated(self, pg_ready):
        _seed_realtime_boards(pg_ready)
        fake = FakeBoardHistoryAdapter(
            history=_hist(), fail_history=("白酒概念",))
        out = fetch_boards_backfill(
            start="20261013", end="20261015", adapters=[fake], now=NOW)
        assert out["status"] == "partial_error"
        assert out["failed_boards"] == ["白酒概念"]
        assert _board_rows(pg_ready, "20261014") == [
            ("industry", "酿酒行业", 2000.0, "push2his")
        ]
        ledger = _ledger(pg_ready, "market_boards")
        by_name = {r[5].split()[1].split("/")[1]: r for r in ledger}
        assert by_name["酿酒行业"][1] == "ok"
        assert by_name["白酒概念"][1] == "error"

    def test_empty_board_list_error(self, pg_ready):
        out = fetch_boards_backfill(
            start="20261013", end="20261015",
            adapters=[FakeBoardHistoryAdapter()], now=NOW)
        assert out["status"] == "error"
        assert "板块清单为空" in out["error"]

    def test_intraday_today_end_rejected(self, pg_ready):
        """盘中禁写延伸：显式 end=当日且交易时段内 → error 零落库零台账。"""
        _seed_realtime_boards(pg_ready)
        now = datetime(2026, 10, 19, 10, 0, tzinfo=_TZ8)
        out = fetch_boards_backfill(
            start="20261014", end="20261019",
            adapters=[FakeBoardHistoryAdapter()], now=now)
        assert out["status"] == "error"
        assert _ledger(pg_ready, "market_boards") == []

    def test_ledger_per_board(self, pg_ready):
        _seed_realtime_boards(pg_ready)
        fetch_boards_backfill(
            start="20261013", end="20261015",
            adapters=[FakeBoardHistoryAdapter(history=_hist())], now=NOW)
        ledger = _ledger(pg_ready, "market_boards")
        assert len(ledger) == 2
        assert all(r[0] == "20261015" and r[1] == "ok" and r[3] == 3 for r in ledger)
        assert all(r[2] == "push2his" for r in ledger)  # 落库口径源；抓取源在 detail
        assert "20261013~20261015" in ledger[0][5]

    def test_progress_logged(self, pg_ready, caplog, monkeypatch):
        """长循环定期向 stderr 报进度（PROGRESS_LOG_EVERY 可覆盖）。"""
        import qstock_mcp.tools_backfill as tb

        monkeypatch.setattr(tb, "PROGRESS_LOG_EVERY", 1)
        _seed_realtime_boards(pg_ready)
        with caplog.at_level(logging.INFO, logger="qstock_mcp.tools_backfill"):
            fetch_boards_backfill(
                start="20261013", end="20261015",
                adapters=[FakeBoardHistoryAdapter(history=_hist())], now=NOW)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("backfill 开始：2 个板块" in m for m in msgs)
        assert any("backfill 进度 1/2" in m for m in msgs)
        assert any("backfill 进度 2/2" in m and "累计落库 6 行" in m
                   for m in msgs)

    def test_progress_time_threshold(self, pg_ready, caplog, monkeypatch):
        """时间阈值（issue #89，spec #86）：距上条进度 ≥60s 即打，不等计数阈值。"""
        import qstock_mcp.tools_backfill as tb

        monkeypatch.setattr(tb, "PROGRESS_LOG_EVERY", 1000)  # 计数阈值不触发
        clock = iter([0, 100, 200, 300])  # 开始 t=0；第 1 板处理完已 100s
        monkeypatch.setattr(tb, "monotonic", lambda: next(clock))
        _seed_realtime_boards(pg_ready)
        with caplog.at_level(logging.INFO, logger="qstock_mcp.tools_backfill"):
            fetch_boards_backfill(
                start="20261013", end="20261015",
                adapters=[FakeBoardHistoryAdapter(history=_hist())], now=NOW)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("backfill 进度 1/2" in m for m in msgs)  # 时间阈值触发
        assert any("backfill 进度 2/2" in m for m in msgs)  # 收尾必打


class TestConsAccumulation:
    def test_cons_rows_land(self, pg_ready):
        _seed_realtime_boards(pg_ready)
        fake = FakeBoardHistoryAdapter(cons={"白酒概念": [
            {"stock_code": "600519"}, {"stock_code": "000858"}]})
        out = accumulate_em_concepts(
            trade_date=END, adapters=[fake], now=NOW)
        assert out["status"] == "ok"
        assert out["boards"] == 1  # 仅概念板块
        assert out["rows"] == 2
        rows = _cons_rows(pg_ready)
        assert rows == [
            ("em", "000858", "BK0816", datetime(2026, 10, 16).date()),
            ("em", "600519", "BK0816", datetime(2026, 10, 16).date()),
        ]

    def test_cons_rerun_idempotent(self, pg_ready):
        _seed_realtime_boards(pg_ready)
        fake = FakeBoardHistoryAdapter()
        accumulate_em_concepts(trade_date=END, adapters=[fake], now=NOW)
        out = accumulate_em_concepts(trade_date=END, adapters=[fake], now=NOW)
        assert out["rows"] == 0  # ON CONFLICT DO NOTHING
        assert len(_cons_rows(pg_ready)) == 1

    def test_cons_failure_isolated(self, pg_ready):
        _seed_realtime_boards(pg_ready)
        fake = FakeBoardHistoryAdapter(fail_cons=("白酒概念",))
        out = accumulate_em_concepts(trade_date=END, adapters=[fake], now=NOW)
        assert out["status"] == "error"
        assert _cons_rows(pg_ready) == []
        ledger = _ledger(pg_ready, "sw_industry_class")
        assert ledger[0][1] == "error"

    def test_cons_progress_logged(self, pg_ready, caplog, monkeypatch):
        """cons 循环与 backfill 同套进度语义（issue #89）：计数阈值触发。"""
        import qstock_mcp.tools_backfill as tb

        monkeypatch.setattr(tb, "PROGRESS_LOG_EVERY", 1)
        _seed_realtime_boards(pg_ready)
        with caplog.at_level(logging.INFO, logger="qstock_mcp.tools_backfill"):
            accumulate_em_concepts(
                trade_date=END, adapters=[FakeBoardHistoryAdapter()], now=NOW)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("cons 开始：1 个概念板块" in m for m in msgs)
        assert any("cons 进度 1/1" in m for m in msgs)


class TestSchedulerIntegration:
    def test_backfill_subcommand(self, pg_ready, capsys, monkeypatch):
        monkeypatch.setattr(
            scheduler, "fetch_boards_backfill",
            lambda **kw: {"status": "ok", "window": {"start": "20261014", "end": END},
                          "boards": [], "failed_boards": []})
        rc = scheduler.main(["backfill"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["job"] == "backfill"

    def test_backfill_partial_exit_1(self, pg_ready, capsys, monkeypatch):
        monkeypatch.setattr(
            scheduler, "fetch_boards_backfill",
            lambda **kw: {"status": "partial_error", "failed_boards": ["白酒概念"]})
        rc = scheduler.main(["backfill", "--start", "20261014"])
        assert rc == 1

    def test_close_runs_cons_when_boards_ok(self, pg_ready, capsys):
        rc = scheduler.main(
            ["close"], adapters=[FakeBoardAdapter("fake")],
            cons_adapters=[FakeBoardHistoryAdapter(
                cons={"白酒概念": [{"stock_code": "600519"}]})])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["cons"]["status"] == "ok"
        assert out["cons"]["rows"] == 1
        rows = _cons_rows(pg_ready)
        assert rows == [("em", "600519", "BK0816",
                         datetime.strptime(out["trade_date"], "%Y%m%d").date())]

    def test_close_cons_failure_not_blocking(self, pg_ready, capsys):
        """cons 失败不拖垮主批：主批 section 全 ok 时退出码仍为 0。"""
        rc = scheduler.main(
            ["close"], adapters=[FakeBoardAdapter("fake")],
            cons_adapters=[FakeBoardHistoryAdapter(fail_cons=("白酒概念",))])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["cons"]["status"] == "error"
        assert out["failed_sections"] == []

    def test_close_cons_skipped_when_boards_failed(self, pg_ready, capsys):
        """东财不健康（boards 失败）时 cons 不执行。"""
        cons = FakeBoardHistoryAdapter()
        rc = scheduler.main(
            ["close"],
            adapters=[FakeBoardAdapter("fake", fail_sections=("boards",))],
            cons_adapters=[cons])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["cons"]["status"] == "skipped"
        assert cons.cons_calls == []
