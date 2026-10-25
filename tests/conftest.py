"""DB 测试约定：连真实 PostgreSQL 测试库；不可达时自动 skip。

测试库默认 `qstock_test`（通过维护库 `postgres` 按需创建），可用环境变量覆盖：
- QSTOCK_TEST_PG_DSN：测试库连接串
- QSTOCK_TEST_MAINT_DSN：维护库连接串（用于建测试库）
"""

import os
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

MAINT_DSN = os.environ.get("QSTOCK_TEST_MAINT_DSN", "postgresql://localhost/postgres")
TEST_DSN = os.environ.get(
    "QSTOCK_TEST_PG_DSN", "postgresql://localhost/qstock_test"
)
# 待建/待清库名随 DSN 覆盖（并行开发各自的隔离测试库，如 qstock_test_115）；
# 缺省仍为 qstock_test
TEST_DB = TEST_DSN.rsplit("/", 1)[-1]


def _reachable(dsn: str) -> bool:
    try:
        psycopg2.connect(dsn, connect_timeout=2).close()
        return True
    except Exception:
        return False


# 测试库为全 runner 共享单例：每个用例都 terminate 他端连接 + DROP SCHEMA，
# 两个 pytest 进程同跑会互相拆表（现象 = 大面积 psycopg2 OperationalError /
# UndefinedTable，干扰诊断）。会话级咨询锁保证同时只有一个 runner 持有测试库；
# 持锁进程被 kill 时锁随连接释放，不会死锁后续运行。
_TEST_RUNNER_LOCK_KEY = 0x71557  # 'qstock' 七进制梗：任意不冲突常量即可


def _try_lock() -> tuple[Any | None, bool | None]:
    """经维护库取会话级咨询锁。

    返回 (conn, True) = 已持锁（conn 须保活到会话结束）；(None, None) = PG
    不可达，交由各用例 pg_test fixture 自行 skip；(None, False) = 锁被其他
    runner 持有，调用方应立即退出。
    """
    try:
        conn = psycopg2.connect(MAINT_DSN, connect_timeout=2)
    except Exception:
        return None, None
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_TEST_RUNNER_LOCK_KEY,))
        got = bool(cur.fetchone()[0])
    if not got:
        conn.close()
        return None, False
    return conn, True


@pytest.fixture(scope="session", autouse=True)
def _single_test_runner():
    """拒绝并发 runner：拿不到咨询锁直接退出（返回码 3），不产出误导性红结果。"""
    conn, got = _try_lock()
    if got is None:
        yield  # PG 不可达：交由各用例 pg_test fixture 自行 skip
        return
    if not got:
        pytest.exit(
            "另一个 pytest 进程正持有 qstock_test 测试库 runner 锁"
            "（共享库不可并发，并发会互相 DROP SCHEMA 制造假失败）；"
            "请先结束它再跑",
            returncode=3,
        )
    try:
        yield
    finally:
        conn.close()  # 会话连接关闭即释放咨询锁


def reset_test_db(dsn: str) -> None:
    """清空测试库：先 terminate 本库残留会话（上个用例断言失败泄漏的连接
    持锁时 DROP SCHEMA 会被永久阻塞，issue #30），再重建 public schema。"""
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()",
            (conn.info.dbname,),
        )
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    conn.close()


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """重试退避 sleep 全局置空（issue #87）：测试不真的等待；退避专项测试
    在本夹具生效后自行覆盖为记录器。"""
    monkeypatch.setattr("qstock_mcp.fetch_chain.sleep", lambda _s: None)


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

    reset_test_db(TEST_DSN)

    monkeypatch.setenv("PG_DSN", TEST_DSN)
    return TEST_DSN
