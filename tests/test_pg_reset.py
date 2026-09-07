"""pg_test 的 schema 重置兜底（issue #30）：用例断言失败泄漏的连接持锁时，
reset_test_db 先 terminate 本库残留会话，DROP SCHEMA 不再永久阻塞。
"""

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from conftest import reset_test_db


def test_reset_terminates_leaked_connection(pg_test):
    """模拟上个用例断言失败：连接未提交（idle in transaction）持锁、未 close。

    reset_test_db 须终止该残留会话并完成清库；泄漏连接此后不可再用。
    """
    leaked = psycopg2.connect(pg_test)
    try:
        leaked.cursor().execute("CREATE TABLE leak_t (id int)")  # 未提交，持锁
        reset_test_db(pg_test)  # 不阻塞即通过；fix 前这里会被 DROP SCHEMA 卡死
        with pytest.raises(psycopg2.OperationalError):
            leaked.cursor().execute("SELECT 1")  # 连接已被 terminate
    finally:
        leaked.close()  # 本用例自己收尾，不依赖后继 fixture 兜底


def test_reset_recreates_empty_schema(pg_test):
    """清库后 public schema 为空：上个用例的表不存在。"""
    conn = psycopg2.connect(pg_test)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE leftover (id int)")
    conn.close()
    reset_test_db(pg_test)
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
        assert cur.fetchone()[0] == 0
    conn.close()
