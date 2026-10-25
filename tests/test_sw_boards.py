"""申万 boards 行业段迁移测试（issue #118，spec #111）：fake 适配器 + 真实 PG。

覆盖：boards section 申万双级口径落库（industry_l1/l2）、三字段第四态声明
（planned_absent_fields 进响应与台账 detail）、历史日期防线不变、核心
section fail-fast 语义不变（申万连接级故障整体 error）、sw-boards 子命令
（fetch / --check 对拍）、sw-dict 成分字典在线刷新。
"""

import json
from datetime import date

import psycopg2
import pytest

from qstock_mcp import scheduler
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.sw_board_parity import compare_sw_board_sources
from qstock_mcp.tools_board import (
    PLANNED_ABSENT_BOARD_FIELDS,
    fetch_board_snapshot,
)
from fakes import FakeSwBoardsAdapter, SW_BOARD_ROWS

TODAY = date.today().strftime("%Y%m%d")


@pytest.fixture
def pg_ready(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()
    return pg_test


def _boards_rows(dsn, trade_date):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT board_type, board_code, board_name, latest_price,"
                " change_percent, volume, amount, volume_ratio, leading_stock"
                " FROM market_boards WHERE trade_date = %s ORDER BY board_code",
                (trade_date,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _boards_ledger(dsn, trade_date):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, source, rows, detail FROM collection_log"
                " WHERE section = 'boards' AND trade_date = %s ORDER BY id",
                (trade_date,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- boards section 申万落库

def test_boards_section_lands_sw_rows(pg_ready):
    out = fetch_board_snapshot(
        trade_date=TODAY, sections="boards",
        adapters=[FakeSwBoardsAdapter()],
    )
    assert out["status"] == "ok"
    boards = out["sections"]["boards"]
    assert boards["status"] == "ok"
    assert boards["rows"] == 2
    assert boards["source"] == "swsresearch"
    # 三字段第四态机器可读声明（issue #118，不误报故障）
    assert boards["planned_absent_fields"] == PLANNED_ABSENT_BOARD_FIELDS
    assert set(boards["planned_absent_fields"]) == {
        "fund_flow", "volume_ratio", "leading_stock", "leading_change",
    }
    rows = _boards_rows(pg_ready, TODAY)
    assert len(rows) == 2
    types = {r[0] for r in rows}
    assert types == {"industry_l1", "industry_l2"}  # 申万双级口径
    for r in rows:
        assert r[7] is None and r[8] is None  # volume_ratio/leading_stock 恒 NULL
    ledger = _boards_ledger(pg_ready, TODAY)
    assert ledger[-1][0] == "ok"
    assert ledger[-1][1] == "swsresearch"
    assert "planned_absent_fields=fund_flow,volume_ratio,leading_stock,leading_change" \
        in (ledger[-1][3] or "")


def test_boards_section_sw_chain_when_not_injected(pg_ready, monkeypatch):
    """未注入适配器时 boards 段解析为申万单源链（default_boards_adapters）。"""
    from qstock_mcp import tools_board

    monkeypatch.setattr(
        tools_board, "default_boards_adapters",
        lambda: [FakeSwBoardsAdapter()],
    )
    out = fetch_board_snapshot(trade_date=TODAY, sections="boards")
    assert out["sections"]["boards"]["source"] == "swsresearch"
    assert len(_boards_rows(pg_ready, TODAY)) == 2


def test_boards_section_historical_date_rejected(pg_ready):
    """实时语义防线不变：申万承接后 boards 仍为实时 section，历史日期零落库。"""
    out = fetch_board_snapshot(
        trade_date="20200102", sections="boards",
        adapters=[FakeSwBoardsAdapter()],
    )
    assert out["status"] == "error"
    boards = out["sections"]["boards"]
    assert boards["status"] == "error"
    assert boards["error_kind"] == "unsupported"
    assert boards["detail"] == "realtime_section_no_backfill"
    assert _boards_rows(pg_ready, "20200102") == []


def test_boards_section_connection_failure_triggers_failfast(pg_ready):
    """申万连接级故障：boards 为核心 section → 整体 error + retry_advice。"""
    exc = FetchError("swsresearch current boom")
    exc.__cause__ = ConnectionResetError("reset")
    out = fetch_board_snapshot(
        trade_date=TODAY, sections="boards",
        adapters=[FakeSwBoardsAdapter(fail=("boards",), exc=exc)],
    )
    assert out["status"] == "error"
    assert out["error_kind"] == "upstream_unreachable"
    assert "boards" in out["error"]
    assert "retry_advice" in out
    assert _boards_rows(pg_ready, TODAY) == []


# ---------------------------------------------------------------- sw-boards 子命令

def _run(capsys, argv, adapters):
    rc = scheduler.main(argv, adapters=adapters)
    return rc, json.loads(capsys.readouterr().out)


def test_cli_sw_boards_fetch(pg_ready, capsys):
    rc, out = _run(capsys, ["sw-boards"], [FakeSwBoardsAdapter()])
    assert rc == 0
    assert out["job"] == "sw-boards" and out["mode"] == "fetch"
    assert out["boards"]["status"] == "ok"
    assert out["boards"]["rows"] == 2
    assert "planned_absent_fields" in out["boards"]
    assert len(_boards_rows(pg_ready, TODAY)) == 2


def test_cli_sw_boards_failure_exit_1(pg_ready, capsys):
    rc, out = _run(
        capsys, ["sw-boards"],
        [FakeSwBoardsAdapter(fail=("boards",))],
    )
    assert rc == 1
    assert out["status"] == "error"


def test_cli_sw_boards_check_parity(pg_ready, capsys):
    """--check 对拍：库内行与官网一致 → 零差异；删库内行 → in_db=False 差异。"""
    adapters = [FakeSwBoardsAdapter()]
    fetch_board_snapshot(trade_date=TODAY, sections="boards", adapters=adapters)
    rc, out = _run(capsys, ["sw-boards", "--check"], adapters)
    assert rc == 0
    assert out["mode"] == "check"
    assert out["count"] == 2
    assert out["mismatches"] == 0
    for row in out["rows"]:
        assert row["in_db"] is True
        assert row["diffs"] == []

    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM market_boards WHERE trade_date = %s"
                " AND board_code = '801880'", (TODAY,))
        conn.commit()
    finally:
        conn.close()
    rc, out = _run(capsys, ["sw-boards", "--check"], adapters)
    assert rc == 0  # 差异是对拍产出，非失败
    missing = next(r for r in out["rows"] if r["board_code"] == "801880")
    assert missing["in_db"] is False
    assert missing["diffs"] == ["库内无行"]
    assert out["mismatches"] == 1


def test_cli_sw_boards_check_upstream_unreachable(pg_ready, capsys):
    """申万连接级故障：对拍 error + upstream_unreachable + retry_advice。"""
    fake = FakeSwBoardsAdapter()

    def _boom(trade_date):
        exc = FetchError("swsresearch current boom")
        exc.__cause__ = ConnectionResetError("reset")
        raise exc

    fake.fetch_boards = _boom
    rc, out = _run(capsys, ["sw-boards", "--check"], [fake])
    assert rc == 1
    assert out["status"] == "error"
    assert out["error_kind"] == "upstream_unreachable"
    assert "retry_advice" in out


def test_parity_limit_scope(pg_ready, capsys):
    adapters = [FakeSwBoardsAdapter()]
    fetch_board_snapshot(trade_date=TODAY, sections="boards", adapters=adapters)
    rc, out = _run(
        capsys, ["sw-boards", "--check", "--limit", "1"], adapters)
    assert rc == 0
    assert out["count"] == 1


# ---------------------------------------------------------------- sw-dict 成分字典在线刷新

def test_cli_sw_dict_refresh(pg_ready, capsys, monkeypatch):
    """申万分类 xls（fake 返回内置 fixture 字节）→ 清空重载 'sw' 体系。"""
    monkeypatch.setattr(
        "qstock_mcp.adapters.sw_adapter.SwSResearchAdapter",
        lambda *a, **k: FakeSwBoardsAdapter(),
    )
    rc, out = _run(capsys, ["sw-dict"], None)
    assert rc == 0
    assert out["job"] == "sw-dict"
    assert out["status"] == "ok"
    assert out["source"] == "swsresearch"
    assert out["inserted"] > 0
    assert out["stocks"] > 0
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(DISTINCT stock_code) FROM sw_industry_class"
                " WHERE source_system = 'sw'")
            assert cur.fetchone()[0] == out["stocks"]
            # 北交所归属在册（申万分类覆盖北交所，调研结论 3）
            cur.execute(
                "SELECT count(DISTINCT stock_code) FROM sw_industry_class"
                " WHERE source_system = 'sw' AND stock_code LIKE '92%'")
            assert cur.fetchone()[0] > 0
    finally:
        conn.close()
    # 重复执行幂等：行数稳定
    rc, again = _run(capsys, ["sw-dict"], None)
    assert rc == 0
    assert again["inserted"] == out["inserted"]


def test_refresh_sw_dict_download_failure(pg_ready, monkeypatch):
    from qstock_mcp import tools_init

    monkeypatch.setattr(
        "qstock_mcp.adapters.sw_adapter.SwSResearchAdapter",
        lambda *a, **k: FakeSwBoardsAdapter(fail=("class_file",)),
    )
    result = tools_init.refresh_sw_dict()
    assert result["status"] == "error"
    conn = psycopg2.connect(pg_ready)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sw_industry_class")
            assert cur.fetchone()[0] == 0  # 下载失败零落库
    finally:
        conn.close()
