"""首次使用主路径：覆盖预检、90 交易日刷新、统一覆盖验收。"""

import json

import psycopg2

from qstock_mcp import scheduler
from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import upsert_stock_list
from fakes import (
    FakeAdapter, FakeSwBoardsAdapter, SW_BOARD_LIST, _sw_history_row, trading_rows,
)


class IndexAdapter:
    name = "tencent"

    def fetch_index_daily(self, index_code, start, end):
        return trading_rows(start, end)


class SwAdapter(FakeSwBoardsAdapter):
    def fetch_sw_board_history(self, board, start, end):
        self.calls.append("history")
        return [
            dict(
                _sw_history_row(day),
                board_type={1: "industry_l1", 2: "industry_l2"}[board["level"]],
                board_code=board["board_code"],
                board_name=board["board_name"],
            )
            for day in (r["trade_date"] for r in trading_rows(start, end))
        ]


class LhbAdapter:
    name = "exchange"

    def fetch_lhb(self, trade_date):
        return {
            "lhb_basic": [{
                "trade_date": trade_date,
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "lhb_reason": "日涨幅偏离值达到7%",
                "buy_amount": 200.0,
                "sell_amount": 100.0,
                "net_buy_amount": 100.0,
                "total_amount": 300.0,
            }],
            "lhb_yyb_capital": [{"rank": 1, "seat_name": "历史禁写"}],
            "lhb_yyb_most": [{"rank": 1, "seat_name": "历史禁写"}],
        }


def _run(capsys, argv, **adapters):
    rc = scheduler.main(argv, **adapters)
    return rc, json.loads(capsys.readouterr().out)


def test_first_run_90_trading_day_path(pg_test, capsys, monkeypatch):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    upsert_stock_list(conn, [{"stock_code": "600519", "stock_name": "贵州茅台"}], "fake")
    conn.close()
    window = ["--trading-days", "90", "--end", "20261023"]
    monkeypatch.setattr(
        "qstock_mcp.tools_coverage.load_sw_industry_meta_seed",
        lambda: [
            {"industry_code": b["board_code"], "name": b["board_name"], "level": b["level"]}
            for b in SW_BOARD_LIST
        ],
    )

    rc, before = _run(capsys, ["coverage", *window])
    assert rc == 1
    assert before["summary"]["gaps"] > 0

    rc, daily = _run(
        capsys, ["daily", "--all", *window, "--force", "--throttle", "0"],
        daily_adapters=[FakeAdapter("tencent", rows_fn=trading_rows)],
    )
    assert rc == 0 and daily["failed"] == 0

    rc, index = _run(capsys, ["index", *window], index_adapters=[IndexAdapter()])
    assert rc == 0 and len(index["indices"]) == 7

    sw = SwAdapter()
    monkeypatch.setattr(
        "qstock_mcp.adapters.sw_adapter.SwSResearchAdapter", lambda: sw
    )
    rc, boards = _run(capsys, ["backfill", *window, "--force"])
    assert rc == 0 and boards["failed_boards"] == []

    rc, lhb = _run(capsys, ["lhb", *window], lhb_adapters=[LhbAdapter()])
    assert rc == 0 and lhb["failed"] == 0

    rc, after = _run(capsys, ["coverage", *window])
    assert rc == 0
    assert after["status"] == "ok"
    assert after["summary"]["gaps"] == 0
    assert after["calendar_days"] == 90

    conn = psycopg2.connect(pg_test)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_daily")
            assert cur.fetchone()[0] == 90
            cur.execute("SELECT COUNT(*) FROM index_daily")
            assert cur.fetchone()[0] == 7 * 90
            cur.execute("SELECT COUNT(*) FROM market_boards")
            assert cur.fetchone()[0] == len(SW_BOARD_LIST) * 90
            cur.execute("SELECT COUNT(*) FROM lhb_basic")
            assert cur.fetchone()[0] == 90
            cur.execute("SELECT COUNT(*) FROM lhb_yyb_capital")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM lhb_yyb_most")
            assert cur.fetchone()[0] == 0
            cur.execute("DELETE FROM lhb_stock_statistic WHERE trade_date = '20261023'")
        conn.commit()
    finally:
        conn.close()

    rc, incomplete = _run(capsys, ["coverage", *window])
    assert rc == 1
    lhb_day = next(
        row for row in incomplete["sections"]["lhb"]
        if row["trade_date"] == "20261023"
    )
    assert lhb_day["basic_rows"] > 0
    assert lhb_day["statistic_rows"] == 0
    assert lhb_day["status"] == "gap"
