"""query_index_daily 接缝测试（issue #33/T1）：真实 PG 测试库，纯读不补抓。

契约：按 index_code + 日期范围（yyyymmdd 闭区间）查询 index_daily 已沉淀的
主要指数日线，按 trade_date 升序返回最近 limit 行（缺省 120）；未知指数代码、
坏日期、非法 limit 走统一 error 契约（参数错误不连库）；空结果 count:0 非错误。
PG 不可达自动 skip（见 conftest.pg_test）。
"""

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import select_index_daily, upsert_index_daily
from qstock_mcp.tools_init import query_index_daily

from fakes import weekday_rows

# 000001 落 2024-01-02~01-12 的 9 个工作日行；399006 落 01-02~01-05 的 4 行
_SEED_START, _SEED_END = "20240102", "20240112"


def _seed(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    upsert_index_daily(conn, "000001", weekday_rows(_SEED_START, _SEED_END), "efinance")
    upsert_index_daily(conn, "399006", weekday_rows(_SEED_START, "20240105"), "akshare")
    conn.close()


# ------------------------------------------------------- select_index_daily（真实 PG 测试库）


def test_select_filters_by_range_and_orders_ascending(pg_test):
    _seed(pg_test)
    conn = psycopg2.connect(pg_test)
    rows = select_index_daily(conn, "000001", start="20240105", end="20240109")
    assert [r["trade_date"] for r in rows] == ["20240105", "20240108", "20240109"]
    assert rows[0]["index_code"] == "000001"
    assert rows[0]["source"] == "efinance"
    assert "id" not in rows[0] and "created_at" not in rows[0]  # 内部列不外泄
    conn.close()


def test_select_limit_takes_most_recent_rows(pg_test):
    _seed(pg_test)
    conn = psycopg2.connect(pg_test)
    rows = select_index_daily(conn, "000001", limit=3)
    assert [r["trade_date"] for r in rows] == ["20240110", "20240111", "20240112"]
    conn.close()


# ------------------------------------------------------- query_index_daily 工具层


def test_query_range_roundtrip_ok(pg_test):
    _seed(pg_test)
    result = query_index_daily("000001", start="20240105", end="20240109")
    assert result["status"] == "ok"
    assert result["tool"] == "query_index_daily"
    assert result["params"] == {
        "index_code": "000001",
        "start": "20240105",
        "end": "20240109",
        "limit": 120,
    }
    assert result["index_name"] == "上证指数"
    assert result["range"] == {"start": "20240105", "end": "20240109"}
    assert result["count"] == 3
    assert [r["trade_date"] for r in result["rows"]] == [
        "20240105",
        "20240108",
        "20240109",
    ]
    assert result["rows"][0]["close"] == 10.0


def test_query_defaults_return_recent_rows_ascending(pg_test):
    _seed(pg_test)
    result = query_index_daily("000001")
    assert result["status"] == "ok"
    assert result["range"] == {"start": None, "end": None}
    assert result["count"] == 9  # 库里共 9 行，缺省 limit 120 全返回
    dates = [r["trade_date"] for r in result["rows"]]
    assert dates == sorted(dates)
    assert dates[0] == _SEED_START and dates[-1] == _SEED_END


def test_query_default_limit_caps_at_120(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    # 约 174 个工作日行，验证缺省 120 限量取最近
    upsert_index_daily(conn, "399001", weekday_rows("20240101", "20240831"), "efinance")
    conn.close()
    result = query_index_daily("399001")
    assert result["status"] == "ok"
    assert result["count"] == 120
    assert result["rows"][-1]["trade_date"] == "20240830"  # 08-31 为周六


def test_query_limit_takes_most_recent_n(pg_test):
    _seed(pg_test)
    result = query_index_daily("000001", limit=2)
    assert result["status"] == "ok"
    assert [r["trade_date"] for r in result["rows"]] == ["20240111", "20240112"]


def test_query_accepts_dash_date_format(pg_test):
    _seed(pg_test)
    result = query_index_daily("000001", start="2024-01-08", end="2024-01-08")
    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["rows"][0]["trade_date"] == "20240108"


def test_query_isolates_index_codes(pg_test):
    _seed(pg_test)
    result = query_index_daily("399006")
    assert result["status"] == "ok"
    assert result["index_name"] == "创业板指"
    assert result["count"] == 4
    assert all(r["index_code"] == "399006" for r in result["rows"])


def test_query_empty_result_is_zero_count_not_error(pg_test):
    _seed(pg_test)
    result = query_index_daily("000001", start="20250101", end="20250110")
    assert result["status"] == "ok"
    assert result["count"] == 0
    assert result["rows"] == []


def test_query_unknown_index_code_is_param_error():
    result = query_index_daily("600519")  # 参数校验在连库之前，无需 PG
    assert result["status"] == "error"
    assert "600519" in result["error"]
    assert "399006" in result["error"]  # 错误信息列出可选指数


def test_query_rejects_bad_date():
    assert query_index_daily("000001", start="2024-13-45")["status"] == "error"
    assert query_index_daily("000001", end="not-a-date")["status"] == "error"


def test_query_rejects_start_after_end():
    result = query_index_daily("000001", start="20240110", end="20240105")
    assert result["status"] == "error"
    assert "晚于" in result["error"]


def test_query_rejects_invalid_limit():
    assert query_index_daily("000001", limit=0)["status"] == "error"
    assert query_index_daily("000001", limit=-3)["status"] == "error"
    assert query_index_daily("000001", limit=True)["status"] == "error"
