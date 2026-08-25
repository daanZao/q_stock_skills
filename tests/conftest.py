"""DB 测试约定：连真实 PostgreSQL 测试库；不可达时自动 skip。

测试库默认 `qstock_test`（通过维护库 `postgres` 按需创建），可用环境变量覆盖：
- QSTOCK_TEST_PG_DSN：测试库连接串
- QSTOCK_TEST_MAINT_DSN：维护库连接串（用于建测试库）
"""

import os

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

MAINT_DSN = os.environ.get("QSTOCK_TEST_MAINT_DSN", "postgresql://localhost/postgres")
TEST_DB = "qstock_test"
TEST_DSN = os.environ.get("QSTOCK_TEST_PG_DSN", f"postgresql://localhost/{TEST_DB}")


def _reachable(dsn: str) -> bool:
    try:
        psycopg2.connect(dsn, connect_timeout=2).close()
        return True
    except Exception:
        return False


@pytest.fixture
def pg_test(monkeypatch):
    """提供一个干净的测试库 DSN 并注入 PG_DSN；库内所有表在测试前清空。"""
    if not _reachable(MAINT_DSN):
        pytest.skip("PostgreSQL 不可达，跳过 DB 测试")
    conn = psycopg2.connect(MAINT_DSN)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{TEST_DB}"')
    conn.close()

    conn = psycopg2.connect(TEST_DSN)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    conn.close()

    monkeypatch.setenv("PG_DSN", TEST_DSN)
    return TEST_DSN
