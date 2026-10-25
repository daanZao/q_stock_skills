"""backfill 子命令测试（issue #84，spec #71/W5）。

backfill：push2his 板块 K 线同口径回补行业+概念板块历史入 market_boards
（CLI 专用路径，不经 fetch 工具的历史日期防线）；回补行带 source='push2his'
标注，与实时采集行（source 为 NULL）可区分；业务键幂等可重跑。
概念成分积累（accumulate_em_concepts）已随概念维度退役摘除（issue #119）。
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from qstock_mcp import scheduler
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.ledger import record_collection
from qstock_mcp.repository import BOARD_SECTION_TABLES, upsert_board_rows
from qstock_mcp.tools_backfill import fetch_boards_backfill
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


def _seed_many_boards(dsn, n, trade_date=END):
    """种子 n 个概念板块（板块000..板块{n-1}），驱动熔断阈值测试（issue #90）。

    select_distinct_boards 按 (board_type, board_name) 排序：同 type 同前缀下
    数字后缀保证处理顺序 = 编号顺序。
    """
    table, date_col, key_cols, fields = BOARD_SECTION_TABLES["boards"][0]
    conn = psycopg2.connect(dsn)
    try:
        upsert_board_rows(
            conn, table, date_col, key_cols, fields, trade_date,
            [{"board_type": "concept", "board_code": f"BK{i:04d}",
              "board_name": f"板块{i:03d}", "latest_price": 1.0,
              "change_percent": 1.0} for i in range(n)],
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


def _record_backfill_ledger(dsn, btype, bname, start, end, status="ok"):
    """预置一条 backfill 台账行（detail 格式与 tools_backfill 写入一致）。"""
    conn = psycopg2.connect(dsn)
    try:
        record_collection(
            conn, trade_date=end, section="market_boards", status=status,
            source="push2his", rows=3,
            detail=f"backfill {btype}/{bname} {start}~{end} fetch_source=fake",
        )
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

    def test_circuit_breaker_aborts_after_consecutive_failures(self, pg_ready):
        """连续 10 板全源失败即熔断（issue #90，spec #86）：中止，不磨完全部。"""
        _seed_many_boards(pg_ready, 12)
        fake = FakeBoardHistoryAdapter(
            fail_history=tuple(f"板块{i:03d}" for i in range(12)))
        out = fetch_boards_backfill(
            start="20261013", end="20261015", adapters=[fake], now=NOW)
        assert out["status"] == "error"
        assert out["aborted"] is True
        assert "熔断" in out["abort_reason"]
        assert len(out["boards"]) == 10  # 第 10 板熔断，剩余 2 板未处理
        ledger = _ledger(pg_ready, "market_boards")
        assert len(ledger) == 11  # 10 条逐板 error + 1 条整体 aborted
        assert "aborted" in ledger[-1][5]

    def test_circuit_breaker_reset_by_ok_and_empty(self, pg_ready):
        """成功与 empty（上游健康无数据）都重置连续计数，间歇失败不熔断。"""
        _seed_many_boards(pg_ready, 12)
        fake = FakeBoardHistoryAdapter(
            history={("concept", "板块005"): []},  # empty 区间
            fail_history=tuple(
                f"板块{i:03d}" for i in (0, 1, 2, 3, 4, 6, 7, 8, 9, 10)))
        out = fetch_boards_backfill(
            start="20261013", end="20261015", adapters=[fake], now=NOW)
        assert "aborted" not in out
        assert out["status"] == "partial_error"
        assert len(out["boards"]) == 12
        by_name = {b["board_name"]: b["status"] for b in out["boards"]}
        assert by_name["板块005"] == "empty"
        assert by_name["板块011"] == "ok"

    def test_skip_board_with_ok_ledger_same_window(self, pg_ready):
        """断点续跑（issue #91，spec #86）：同板块同窗口台账 ok → 跳过不抓。"""
        _seed_realtime_boards(pg_ready)
        _record_backfill_ledger(pg_ready, "industry", "酿酒行业",
                                "20261013", "20261015")
        fake = FakeBoardHistoryAdapter(history=_hist())
        out = fetch_boards_backfill(
            start="20261013", end="20261015", adapters=[fake], now=NOW)
        assert out["skipped_boards"] == ["酿酒行业"]
        assert [c[1] for c in fake.history_calls] == ["白酒概念"]

    def test_empty_ledger_not_skipped(self, pg_ready):
        """empty（上游当时无数据，覆盖未确认）不构成跳过依据。"""
        _seed_realtime_boards(pg_ready)
        _record_backfill_ledger(pg_ready, "industry", "酿酒行业",
                                "20261013", "20261015", status="empty")
        fake = FakeBoardHistoryAdapter(history=_hist())
        out = fetch_boards_backfill(
            start="20261013", end="20261015", adapters=[fake], now=NOW)
        assert out["skipped_boards"] == []
        assert len(fake.history_calls) == 2

    def test_different_window_not_skipped(self, pg_ready):
        """窗口不同（即便同板块已 ok）不跳过。"""
        _seed_realtime_boards(pg_ready)
        _record_backfill_ledger(pg_ready, "industry", "酿酒行业",
                                "20260901", "20261012")
        fake = FakeBoardHistoryAdapter(history=_hist())
        out = fetch_boards_backfill(
            start="20261013", end="20261015", adapters=[fake], now=NOW)
        assert out["skipped_boards"] == []
        assert len(fake.history_calls) == 2

    def test_rerun_after_success_skips_all(self, pg_ready):
        """成功跑完后同窗口重跑：全部跳过、零抓取，幂等。"""
        _seed_realtime_boards(pg_ready)
        fake = FakeBoardHistoryAdapter(history=_hist())
        out1 = fetch_boards_backfill(
            start="20261013", end="20261015", adapters=[fake], now=NOW)
        assert out1["status"] == "ok"
        fake2 = FakeBoardHistoryAdapter(history=_hist())
        out2 = fetch_boards_backfill(
            start="20261013", end="20261015", adapters=[fake2], now=NOW)
        assert fake2.history_calls == []
        assert out2["status"] == "ok"
        assert sorted(out2["skipped_boards"]) == sorted(["酿酒行业", "白酒概念"])


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

    def test_backfill_aborted_exit_1(self, pg_ready, capsys, monkeypatch):
        """熔断中止（issue #90）：exit 1 且 aborted/abort_reason 透传到 stdout JSON。"""
        monkeypatch.setattr(
            scheduler, "fetch_boards_backfill",
            lambda **kw: {"status": "error", "aborted": True,
                          "abort_reason": "连续 10 个板块全源失败",
                          "boards": [], "failed_boards": []})
        rc = scheduler.main(["backfill"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["aborted"] is True
