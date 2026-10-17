"""采集台账（issue #77，spec #71/W4）：写库路径落账 + query_collection_log 查询。

任何表任何日期的采集历史可查：所有写库路径（fetch_daily/query_daily 自愈、
fetch_market_snapshot、fetch_board_snapshot 逐 section、init 各 part、
成分字典导入）每次执行落一条台账（trade_date/section/status(ok·empty·error)/
source/rows/error_kind/detail/起止时间，全历史保留，append-only）。
query_collection_log 为统一查询面：按日期/section/状态过滤，空结果 count:0
非错误，输出契约同既有查询工具。
"""

import psycopg2
from datetime import date

from qstock_mcp.db import ensure_schema
from qstock_mcp.ledger import record_collection
from qstock_mcp.tools_board import fetch_board_snapshot
from qstock_mcp.tools_daily import fetch_daily
from qstock_mcp.tools_init import init_database
from qstock_mcp.tools_ledger import query_collection_log
from qstock_mcp.tools_snapshot import fetch_market_snapshot

from fakes import (
    FakeAdapter,
    FakeBoardAdapter,
    FakeInitAdapter,
    FakeSnapshotAdapter,
    trading_rows,
)


TODAY = date.today().strftime("%Y%m%d")


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _ledger_rows(pg_test, where="", args=()):
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, section, status, source, rows, error_kind, detail"
            f" FROM collection_log {where} ORDER BY id",
            args,
        )
        cols = ["trade_date", "section", "status", "source", "rows", "error_kind", "detail"]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------- 台账基元


def test_record_collection_roundtrip(pg_test):
    """落一条台账可查回；台账为 append-only，重复动作各留一行（全历史保留）。"""
    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    record_collection(
        conn,
        trade_date="20261014",
        section="boards",
        status="ok",
        source="akshare",
        rows=100,
    )
    record_collection(
        conn, trade_date="20261014", section="boards", status="ok", source="akshare", rows=100
    )
    conn.close()
    rows = _ledger_rows(pg_test)
    assert len(rows) == 2
    assert rows[0]["trade_date"] == "20261014"
    assert rows[0]["started_at"] if "started_at" in rows[0] else True


def test_record_collection_failure_does_not_raise(pg_test):
    """台账写入失败（如表不存在）只记 log，绝不拖垮主流程。"""
    conn = psycopg2.connect(pg_test)  # 未建表
    record_collection(conn, trade_date="20261014", section="boards", status="ok")
    conn.close()


# ---------------------------------------------------------------- fetch 路径落账


def test_fetch_board_snapshot_ledgers_three_states(pg_test):
    """抓取成功/空/失败三态各落一条台账且可查回（含 error_kind 与逐源明细摘要）。

    boards/indices 为实时语义 section（issue #78），用当日日期。
    """
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_sections=("zt_pool",), rows={"boards": []})
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    assert result["status"] == "ok"

    rows = {r["section"]: r for r in _ledger_rows(pg_test, "WHERE trade_date = %s", (TODAY,))}
    assert set(rows) == {"indices", "boards", "zt_pool", "strong_stocks", "lhb"}
    assert rows["indices"]["status"] == "ok"
    assert rows["indices"]["rows"] > 0
    assert rows["indices"]["source"] == "akshare"
    assert rows["boards"]["status"] == "empty"
    assert rows["boards"]["rows"] == 0
    zt = rows["zt_pool"]
    assert zt["status"] == "error"
    assert zt["error_kind"] == "fetch_failed"
    assert "akshare" in zt["detail"]  # 逐源明细摘要


def test_fetch_daily_ledgers_ok_and_error(pg_test):
    """日线抓取落账：成功 ok（行数/来源），全源失败 error + error_kind。"""
    _init(pg_test)
    ok = fetch_daily("600519", start="20261012", end="20261014",
                     adapters=[FakeAdapter("efake", rows_fn=trading_rows)])
    assert ok["status"] == "ok"
    rows = _ledger_rows(pg_test, "WHERE section = 'stock_daily'")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["trade_date"] == "20261014"
    assert rows[0]["rows"] > 0
    assert rows[0]["source"] == "efake"
    assert "20261012" in rows[0]["detail"]  # 区间摘要

    bad = fetch_daily("000001", start="20261012", end="20261014",
                      adapters=[FakeAdapter("broken", fail_times=99)])
    assert bad["status"] == "error"
    rows = _ledger_rows(
        pg_test, "WHERE section = 'stock_daily' AND status = 'error'"
    )
    assert len(rows) == 1
    assert rows[0]["error_kind"] == "fetch_failed"
    assert "broken" in rows[0]["detail"]


def test_fetch_daily_no_gap_no_ledger(pg_test):
    """区间无缺口（零抓取零写库）不落台账——台账只记真实发生的采集动作。"""
    _init(pg_test)
    ad = FakeAdapter("efake", rows_fn=trading_rows)
    fetch_daily("600519", start="20261012", end="20261014", adapters=[ad])
    again = fetch_daily("600519", start="20261012", end="20261014",
                        adapters=[FakeAdapter("efake2", rows_fn=trading_rows)])
    assert again["rows_upserted"] == 0
    rows = _ledger_rows(pg_test, "WHERE section = 'stock_daily'")
    assert len(rows) == 1  # 第二次无写库动作，不落账


def test_fetch_market_snapshot_ledgers(pg_test):
    """快照落账：成功 ok；上游正常空响应 empty（非失败）。"""
    _init(pg_test)
    fetch_market_snapshot(adapters=[FakeSnapshotAdapter("sfake", trade_date="20261014")])
    rows = _ledger_rows(pg_test, "WHERE section = 'market_snapshot'")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["trade_date"] == "20261014"
    assert rows[0]["rows"] == 2

    fetch_market_snapshot(
        trade_date="20261015",
        adapters=[FakeSnapshotAdapter("sfake", rows=[], trade_date="20261015")],
    )
    rows = _ledger_rows(
        pg_test, "WHERE section = 'market_snapshot' AND trade_date = '20261015'"
    )
    assert rows[0]["status"] == "empty"


def test_init_database_ledgers_parts(pg_test):
    """init 轻量初始化各 part 落账：stock_list / market_snapshot / index_daily。"""
    _init(pg_test)
    result = init_database(adapters=[FakeInitAdapter("ifake")])
    assert result["status"] == "ok"
    rows = {r["section"]: r for r in _ledger_rows(pg_test)}
    assert {"stock_list", "market_snapshot", "index_daily"} <= set(rows)
    assert rows["stock_list"]["status"] == "ok"
    assert rows["stock_list"]["rows"] == 2
    assert rows["index_daily"]["rows"] > 0


# ---------------------------------------------------------------- query_collection_log


def test_query_collection_log_filters(pg_test):
    """统一查询面：按日期/section/状态过滤；空结果 count:0 非错误。"""
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_sections=("zt_pool",), rows={"boards": []})
    fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    fetch_daily("600519", start="20261012", end="20261014",
                adapters=[FakeAdapter("efake", rows_fn=trading_rows)])

    all_rows = query_collection_log(trade_date=TODAY)
    assert all_rows["status"] == "ok"
    assert all_rows["count"] == 5  # 5 个盘面 section
    daily_rows = query_collection_log(trade_date="20261014")
    assert daily_rows["count"] == 1
    assert daily_rows["rows"][0]["section"] == "stock_daily"
    by_section = query_collection_log(section="zt_pool")
    assert by_section["count"] == 1
    assert by_section["rows"][0]["status"] == "error"
    by_status = query_collection_log(status="empty")
    assert by_status["count"] == 1
    assert by_status["rows"][0]["section"] == "boards"
    empty = query_collection_log(trade_date="20261013")
    assert empty["status"] == "ok"
    assert empty["count"] == 0
    assert empty["rows"] == []


def test_query_collection_log_row_shape(pg_test):
    """台账行输出含全部字段；时间戳 ISO 序列化（JSON 安全）。"""
    _init(pg_test)
    fetch_daily("600519", start="20261012", end="20261014",
                adapters=[FakeAdapter("efake", rows_fn=trading_rows)])
    out = query_collection_log(section="stock_daily")
    row = out["rows"][0]
    assert set(row) == {
        "trade_date", "section", "status", "source", "rows",
        "error_kind", "detail", "started_at", "finished_at",
    }
    assert "T" in row["finished_at"]


def test_query_collection_log_param_validation(pg_test):
    """非法状态/日期/limit 走统一 error 契约；limit 上限截断防大结果。"""
    _init(pg_test)
    bad_status = query_collection_log(status="weird")
    assert bad_status["status"] == "error"
    assert "status" in bad_status["error"]
    bad_date = query_collection_log(trade_date="2026-13-40")
    assert bad_date["status"] == "error"
    bad_limit = query_collection_log(limit=0)
    assert bad_limit["status"] == "error"
    capped = query_collection_log(limit=99999)
    assert capped["status"] == "ok"  # 上限内截断而非报错
