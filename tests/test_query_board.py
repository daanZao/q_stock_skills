"""query_board_data 接缝测试：真实 PG 测试库，数据经 fetch_board_snapshot 落入（issue #5）。

覆盖：按表/日期/代码过滤、日期缺省取库内最新、lhb_yyb_* 的 fetch_date 语义、
未知表与坏日期参数错误。

日期口径（issue #78 服务端防线）：boards/indices 为实时语义 section，历史日期
拒收——经 fetch 的落库种子一律用当日日期；需要历史日期的种子（最新日期缺省
测试）绕过 fetch、直写 repository。
"""

from datetime import date

import psycopg2

from qstock_mcp.adapters.base import INDEX_FIELDS
from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import upsert_board_rows
from qstock_mcp.tools_board import fetch_board_snapshot, query_board_data

from fakes import BOARD_ROWS, INDEX_ROWS, FakeBoardAdapter

TODAY = date.today().strftime("%Y%m%d")


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _seed(pg_test):
    result = fetch_board_snapshot(
        trade_date=TODAY, adapters=[FakeBoardAdapter("akshare")]
    )
    assert result["status"] == "ok"


def _seed_indices_direct(pg_test, trade_date: str):
    """历史日期种子：实时 section 防线（issue #78）下不经 fetch，直写 repository。"""
    conn = psycopg2.connect(pg_test)
    upsert_board_rows(
        conn, "market_indices", "trade_date", ("index_code",), INDEX_FIELDS,
        trade_date, INDEX_ROWS,
    )
    conn.close()


def test_query_filters_by_table_date_and_code(pg_test):
    _init(pg_test)
    _seed(pg_test)
    result = query_board_data(
        table="market_boards", trade_date=TODAY, code="酿酒行业"
    )
    assert result["status"] == "ok"
    assert result["tool"] == "query_board_data"
    assert result["table"] == "market_boards"
    assert result["trade_date"] == TODAY
    assert result["count"] == 1
    row = result["rows"][0]
    assert row["board_name"] == "酿酒行业"
    assert row["board_type"] == "industry"
    assert row["leading_stock"] == "贵州茅台"
    assert "id" not in row and "created_at" not in row  # 内部列不外泄


def test_query_defaults_to_latest_date_in_table(pg_test):
    _init(pg_test)
    _seed_indices_direct(pg_test, "20240104")  # 历史日期种子直写（防线不经 fetch）
    _seed(pg_test)
    result = query_board_data(table="market_indices")
    assert result["status"] == "ok"
    assert result["trade_date"] == TODAY  # 缺省取库内最新交易日
    assert result["count"] == len(INDEX_ROWS)
    assert all(r["trade_date"] == TODAY for r in result["rows"])


def test_query_lhb_yyb_uses_fetch_date(pg_test):
    _init(pg_test)
    _seed(pg_test)
    result = query_board_data(table="lhb_yyb_capital", trade_date=TODAY)
    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["rows"][0]["seat_name"] == "机构专用"
    assert result["rows"][0]["fetch_date"] == TODAY


def test_query_unknown_table_is_param_error(pg_test):
    _init(pg_test)
    result = query_board_data(table="stock_daily")
    assert result["status"] == "error"
    assert "stock_daily" in result["error"]
    assert "zt_pool" in result["error"]  # 错误信息列出可选表


def test_query_rejects_bad_date_param(pg_test):
    _init(pg_test)
    result = query_board_data(table="zt_pool", trade_date="2024-13-45")
    assert result["status"] == "error"
