"""query_snapshot 聚合模式与排序取前 N 接缝测试（issue #48）。

播种两个交易日快照行 → 调工具 → 断言：聚合值（涨跌家数全市场/分市场、
两市成交额合计，万元口径 ADR 0006）、topN 排序与边界（N 大于行数、空表）、
不传新参数时行为完全不变、两个交易日各调一次口径一致（环比由调用方组合，
不在工具内做）。
"""

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_snapshot import fetch_market_snapshot, query_snapshot

from fakes import FakeSnapshotAdapter

DAY1 = "20240104"
DAY2 = "20240105"


def _row(stock_code, name, latest, pre_close, amount):
    return {
        "stock_code": stock_code,
        "stock_name": name,
        "latest_price": latest,
        "pre_close": pre_close,
        "amount": amount,
        "volume": 100,
    }


DAY2_ROWS = [
    _row("600519", "贵州茅台", 10.0, 9.0, 100.0),  # 沪 涨
    _row("601398", "工商银行", 9.0, 10.0, 200.0),  # 沪 跌
    _row("000001", "平安银行", 11.0, 10.0, 300.0),  # 深 涨
    _row("300750", "宁德时代", 10.0, 10.0, 400.0),  # 深 平
    _row("830799", "北证甲", 5.0, 4.0, 50.0),  # 北 涨
    _row("920001", "北证乙", None, 5.0, 10.0),  # 北 无最新价：不计涨跌家数，计成交额
]

DAY1_ROWS = [
    _row("600519", "贵州茅台", 9.0, 10.0, 500.0),  # 沪 跌
    _row("000001", "平安银行", 11.0, 10.0, 500.0),  # 深 涨
]


def _seed(pg_test, trade_date, rows):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()
    fetch_market_snapshot(
        adapters=[FakeSnapshotAdapter("efinance", rows=rows, trade_date=trade_date)]
    )


def _seed_two_days(pg_test):
    _seed(pg_test, DAY1, DAY1_ROWS)
    _seed(pg_test, DAY2, DAY2_ROWS)


# ---------------------------------------------------------------- 聚合模式


def test_aggregate_breadth_and_amount_totals_and_by_market(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2, aggregate="breadth,amount")
    assert result["status"] == "ok"
    assert result["tool"] == "query_snapshot"
    assert result["params"] == {
        "trade_date": DAY2,
        "stock_code": None,
        "aggregate": "breadth,amount",
    }
    assert result["trade_date"] == DAY2
    assert "rows" not in result
    agg = result["aggregates"]
    assert agg["breadth"]["total"] == {"up": 3, "down": 1, "flat": 1}
    assert agg["breadth"]["by_market"] == {
        "sh": {"up": 1, "down": 1, "flat": 0},
        "sz": {"up": 1, "down": 0, "flat": 1},
        "bj": {"up": 1, "down": 0, "flat": 0},
    }
    assert agg["amount"]["total_amount"] == 1060.0
    assert agg["amount"]["by_market"] == {"sh": 300.0, "sz": 700.0, "bj": 60.0}


def test_aggregate_single_kind(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    breadth = query_snapshot(trade_date=DAY2, aggregate="breadth")
    assert set(breadth["aggregates"]) == {"breadth"}
    amount = query_snapshot(trade_date=DAY2, aggregate="amount")
    assert set(amount["aggregates"]) == {"amount"}
    assert amount["aggregates"]["amount"]["total_amount"] == 1060.0


def test_aggregate_empty_date_returns_zeros(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date="20240103", aggregate="breadth,amount")
    assert result["status"] == "ok"
    agg = result["aggregates"]
    assert agg["breadth"]["total"] == {"up": 0, "down": 0, "flat": 0}
    assert agg["breadth"]["by_market"] == {
        "sh": {"up": 0, "down": 0, "flat": 0},
        "sz": {"up": 0, "down": 0, "flat": 0},
        "bj": {"up": 0, "down": 0, "flat": 0},
    }
    assert agg["amount"]["total_amount"] == 0.0
    assert agg["amount"]["by_market"] == {"sh": 0.0, "sz": 0.0, "bj": 0.0}


def test_aggregate_combines_with_stock_code_filter(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2, stock_code="600519", aggregate="breadth")
    assert result["status"] == "ok"
    assert result["aggregates"]["breadth"]["total"] == {"up": 1, "down": 0, "flat": 0}


def test_aggregate_two_days_same_shape_supports_dod(pg_test):
    _seed_two_days(pg_test)
    day1 = query_snapshot(trade_date=DAY1, aggregate="breadth,amount")
    day2 = query_snapshot(trade_date=DAY2, aggregate="breadth,amount")
    assert day1["status"] == day2["status"] == "ok"
    # 两次调用结构一致，环比（当日/前一日各调一次）由调用方组合
    assert day1["aggregates"].keys() == day2["aggregates"].keys()
    for kind in ("breadth", "amount"):
        assert day1["aggregates"][kind].keys() == day2["aggregates"][kind].keys()
    assert day1["aggregates"]["amount"]["total_amount"] == 1000.0
    assert day2["aggregates"]["amount"]["total_amount"] == 1060.0
    assert day1["aggregates"]["breadth"]["total"] == {"up": 1, "down": 1, "flat": 0}


# ---------------------------------------------------------------- 排序取前 N


def test_topn_by_amount_desc_returns_full_rows(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2, order_by="amount", limit=2)
    assert result["status"] == "ok"
    assert result["params"] == {
        "trade_date": DAY2,
        "stock_code": None,
        "order_by": "amount",
        "order": "desc",
        "limit": 2,
    }
    assert result["count"] == 2
    assert [r["stock_code"] for r in result["rows"]] == ["300750", "000001"]
    top = result["rows"][0]  # 行字段与整表查询一致，不裁剪列
    assert top["trade_date"] == DAY2
    assert top["stock_name"] == "宁德时代"
    assert top["source"] == "efinance"
    assert top["amount"] == 400.0
    assert "latest_price" in top and "market_cap" in top


def test_limit_larger_than_row_count_returns_all(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2, order_by="amount", limit=100)
    assert result["status"] == "ok"
    assert result["count"] == len(DAY2_ROWS)


def test_order_asc(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2, order_by="amount", order="asc", limit=2)
    assert result["status"] == "ok"
    assert [r["stock_code"] for r in result["rows"]] == ["920001", "830799"]


def test_order_by_without_limit_orders_all_rows(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2, order_by="amount")
    assert result["status"] == "ok"
    assert result["count"] == len(DAY2_ROWS)
    amounts = [r["amount"] for r in result["rows"]]
    assert amounts == sorted(amounts, reverse=True)


def test_topn_empty_date_returns_zero_count(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date="20240103", order_by="amount", limit=75)
    assert result["status"] == "ok"
    assert result["count"] == 0
    assert result["rows"] == []


def test_default_call_behavior_unchanged(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2)
    assert result["status"] == "ok"
    # 参数回显不含新参数；行按 stock_code 升序；无 aggregates 键
    assert result["params"] == {"trade_date": DAY2, "stock_code": None}
    assert "aggregates" not in result
    assert result["count"] == len(DAY2_ROWS)
    codes = [r["stock_code"] for r in result["rows"]]
    assert codes == sorted(codes)


# ---------------------------------------------------------------- 参数校验


def test_rejects_unknown_aggregate_kind(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2, aggregate="breadth,turnover")
    assert result["status"] == "error"
    assert "turnover" in result["error"]


def test_rejects_empty_aggregate(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2, aggregate="")
    assert result["status"] == "error"


def test_rejects_unknown_order_by_column(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2, order_by="no_such_column")
    assert result["status"] == "error"
    assert "no_such_column" in result["error"]


def test_rejects_bad_limit(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    for bad in (0, -1, 1.5, "75"):
        result = query_snapshot(trade_date=DAY2, order_by="amount", limit=bad)
        assert result["status"] == "error", bad


def test_rejects_bad_order(pg_test):
    _seed(pg_test, DAY2, DAY2_ROWS)
    result = query_snapshot(trade_date=DAY2, order_by="amount", order="down")
    assert result["status"] == "error"
