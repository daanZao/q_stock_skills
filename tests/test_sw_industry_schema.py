"""成分字典与行业名称表 DDL 测试（issue #72，spec #70）。

打 db.ensure_schema 接缝 + 真实 PG 测试库（pg_test fixture，不可达自动 skip）：
- 幂等建表：sw_industry_class（多体系同构形态）与 sw_industry_meta，重复执行无副作用
- 现库迁移：旧形态手工建表（issue #67 入库的表）行不丢、source_system 回填 'sw'
"""

import psycopg2

from qstock_mcp.db import ensure_schema

from conftest import reset_test_schema

CLASS_COLUMNS = {
    "source_system",
    "stock_code",
    "board_code",
    "effective_date",
    "updated_at",
}
CLASS_PK = ["source_system", "stock_code", "board_code", "effective_date"]


def _columns(cur, table: str) -> set:
    cur.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {row[0] for row in cur.fetchall()}


def _pk_columns(cur, table: str) -> list:
    cur.execute(
        """
        SELECT a.attname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE c.contype = 'p' AND t.relname = %s AND n.nspname = 'public'
        ORDER BY k.ord
        """,
        (table,),
    )
    return [row[0] for row in cur.fetchall()]


def test_ensure_schema_creates_sw_tables(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    with conn.cursor() as cur:
        assert _columns(cur, "sw_industry_meta") == {"industry_code", "name", "level"}
        assert _columns(cur, "sw_industry_class") == CLASS_COLUMNS
        assert _pk_columns(cur, "sw_industry_meta") == ["industry_code"]
        assert _pk_columns(cur, "sw_industry_class") == CLASS_PK
    conn.close()


def test_ensure_schema_is_idempotent(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    ensure_schema(conn)
    with conn.cursor() as cur:
        assert _columns(cur, "sw_industry_meta") == {"industry_code", "name", "level"}
        assert _columns(cur, "sw_industry_class") == CLASS_COLUMNS
        assert _pk_columns(cur, "sw_industry_class") == CLASS_PK
    conn.close()


def test_legacy_sw_industry_class_migrated(pg_test):
    """旧形态（stock_code/effective_date/industry_code/updated_at，PK 二元组）：
    迁移后行完整保留、industry_code 改名 board_code、source_system 回填 'sw'、
    主键扩为四元组；再执行一次 ensure_schema 不重复迁移。"""
    reset_test_schema(pg_test)
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE sw_industry_class (
                stock_code     text,
                effective_date date,
                industry_code  char(6),
                updated_at     timestamp,
                PRIMARY KEY (stock_code, effective_date)
            )
            """
        )
        cur.execute(
            "INSERT INTO sw_industry_class"
            " (stock_code, effective_date, industry_code, updated_at) VALUES"
            " ('600000', '2026-01-01', '801780', now()),"
            " ('000001', '2026-01-01', '801010', now())"
        )
    conn.commit()

    ensure_schema(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sw_industry_class")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT DISTINCT source_system FROM sw_industry_class")
        assert [row[0] for row in cur.fetchall()] == ["sw"]
        cur.execute(
            "SELECT board_code FROM sw_industry_class WHERE stock_code = '600000'"
        )
        assert cur.fetchone()[0] == "801780"
        assert _columns(cur, "sw_industry_class") == CLASS_COLUMNS
        assert _pk_columns(cur, "sw_industry_class") == CLASS_PK

    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sw_industry_class")
        assert cur.fetchone()[0] == 2
        assert _pk_columns(cur, "sw_industry_class") == CLASS_PK
    conn.close()
