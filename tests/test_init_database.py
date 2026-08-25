"""init_database 接缝测试：tool 函数层 + 真实 PG 测试库。

只断言外部行为：返回的自描述 JSON 与落库结果，不碰内部实现。
"""

import psycopg2

from qstock_mcp.tools_init import init_database

EXPECTED_TABLES = {
    "stock_daily",
    "market_snapshot",
    "market_indices",
    "market_boards",
    "zt_pool",
    "strong_stocks",
    "lhb_basic",
    "lhb_stock_detail",
    "lhb_stock_statistic",
    "lhb_yyb_capital",
    "lhb_yyb_most",
    "conclusions",
}


def test_missing_pg_dsn_returns_clear_error(monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    result = init_database()
    assert result["status"] == "error"
    assert "PG_DSN" in result["error"]


def test_init_creates_all_tables(pg_test):
    result = init_database()
    assert result["status"] == "ok"

    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        actual = {row[0] for row in cur.fetchall()}
    conn.close()
    assert EXPECTED_TABLES <= actual


def test_init_is_idempotent(pg_test):
    first = init_database()
    second = init_database()
    assert first["status"] == "ok"
    assert second["status"] == "ok"


def test_init_output_is_self_describing(pg_test):
    result = init_database()
    assert result["status"] == "ok"
    assert result["tool"] == "init_database"
    assert EXPECTED_TABLES <= set(result["tables"])
