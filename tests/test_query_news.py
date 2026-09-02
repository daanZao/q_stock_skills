"""query_news 接缝测试（issue #25/T3）：真实 PG 测试库 + 工具层契约。

契约：按 subject_type/subject_code + 发布时间范围（yyyymmdd 闭区间，东八区
整日）过滤 news_items，按发布时间倒序（news_code 决胜）、limit 限量；纯读
不调上游、不计配额；空结果 rows:0 非错误；参数错误与 PG 不可达走统一
error 契约，任何路径不抛异常。PG 不可达自动 skip（见 conftest.pg_test）。
"""

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import select_news_items, upsert_news_items
from qstock_mcp.tools_mx import query_news

from fakes import MX_SEARCH_ITEMS

# 三条去重后条目的发布时间：AN=2026-08-31 09:18:09，NW=2026-08-28 19:22:00，
# WCY=2026-08-28 17:04:00（均东八区）


def _seed(pg_test):
    """market/_market 下落 3 条（MX_SEARCH_ITEMS 去重后），stock/000338 下落 1 条。"""
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    upsert_news_items(conn, "market", "_market", MX_SEARCH_ITEMS)
    upsert_news_items(conn, "stock", "000338", MX_SEARCH_ITEMS[:1])
    conn.close()


# ------------------------------------------------------- select_news_items（真实 PG 测试库）


def test_select_orders_by_publish_time_desc(pg_test):
    _seed(pg_test)
    conn = psycopg2.connect(pg_test)
    rows = select_news_items(conn, "market", "_market")
    assert [r["news_code"] for r in rows] == [
        "AN202608311828758731",  # 08-31 最新在前
        "NW202608283858550632_1",
        "WCYQIN2026082817072776999521_2",
    ]
    conn.close()


def test_select_filters_by_subject(pg_test):
    _seed(pg_test)
    conn = psycopg2.connect(pg_test)
    rows = select_news_items(conn, "stock", "000338")
    assert [r["news_code"] for r in rows] == ["NW202608283858550632_1"]
    assert rows[0]["subject_type"] == "stock"
    conn.close()


def test_select_filters_by_time_range_inclusive_days(pg_test):
    _seed(pg_test)
    conn = psycopg2.connect(pg_test)
    # start 起含当日 00:00
    rows = select_news_items(conn, "market", "_market", start="20260829")
    assert [r["news_code"] for r in rows] == ["AN202608311828758731"]
    # end 止含当日 23:59（东八区整日）
    rows = select_news_items(conn, "market", "_market", start="20260828", end="20260828")
    assert [r["news_code"] for r in rows] == [
        "NW202608283858550632_1",
        "WCYQIN2026082817072776999521_2",
    ]
    conn.close()


def test_select_limit(pg_test):
    _seed(pg_test)
    conn = psycopg2.connect(pg_test)
    rows = select_news_items(conn, limit=2)
    assert len(rows) == 2
    assert rows[0]["news_code"] == "AN202608311828758731"  # 倒序后取前 2
    conn.close()


def test_select_serializes_publish_time_and_excludes_raw(pg_test):
    _seed(pg_test)
    conn = psycopg2.connect(pg_test)
    row = select_news_items(conn, "stock", "000338")[0]
    assert isinstance(row["publish_time"], str)  # JSON 安全
    assert row["publish_time"].startswith("2026-08-28T19:22:00")
    assert "raw" not in row and "id" not in row and "fetched_at" not in row
    conn.close()


def test_select_empty_returns_empty_list(pg_test):
    _seed(pg_test)
    conn = psycopg2.connect(pg_test)
    assert select_news_items(conn, "stock", "600519") == []
    conn.close()


# ------------------------------------------------------- query_news 工具层


def test_query_roundtrip_ok(pg_test):
    _seed(pg_test)
    result = query_news(subject_type="market", subject_code="_market")
    assert result["status"] == "ok"
    assert result["tool"] == "query_news"
    assert result["params"] == {
        "subject_type": "market",
        "subject_code": "_market",
        "start": None,
        "end": None,
        "limit": 20,
    }
    assert result["rows"] == 3
    assert [i["news_code"] for i in result["items"]] == [
        "AN202608311828758731",
        "NW202608283858550632_1",
        "WCYQIN2026082817072776999521_2",
    ]
    assert "quota" not in result  # 纯读不计配额


def test_query_with_time_range_and_limit(pg_test):
    _seed(pg_test)
    result = query_news(start="2026-08-28", end="2026-08-28", limit=1)
    assert result["status"] == "ok"
    assert result["rows"] == 1
    assert result["items"][0]["news_code"] == "NW202608283858550632_1"


def test_query_empty_result_is_zero_rows_not_error(pg_test):
    _seed(pg_test)
    result = query_news(subject_type="stock", subject_code="600519")
    assert result["status"] == "ok"
    assert result["rows"] == 0
    assert result["items"] == []


def test_query_rejects_bad_date(pg_test):
    assert query_news(start="not-a-date")["status"] == "error"
    assert query_news(end="2026-13-99")["status"] == "error"


def test_query_rejects_start_after_end(pg_test):
    result = query_news(start="20260831", end="20260828")
    assert result["status"] == "error"
    assert "晚于" in result["error"]


def test_query_rejects_invalid_limit(pg_test):
    assert query_news(limit=0)["status"] == "error"
    assert query_news(limit=-3)["status"] == "error"
    assert query_news(limit=True)["status"] == "error"
