"""qstock_test* 隔离库的 runner 互斥与 schema 重建。"""

from contextlib import contextmanager
import threading

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

TEST_RUNNER_LOCK_KEY = 0x71557


class TestRunnerConflict(RuntimeError):
    pass


_state = threading.local()


@contextmanager
def test_runner_lock(maintenance_dsn: str):
    """同一进程可重入；不同 pytest/live 进程互斥持有测试库。"""
    if getattr(_state, "held", False):
        yield
        return
    conn = psycopg2.connect(maintenance_dsn, connect_timeout=2)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (TEST_RUNNER_LOCK_KEY,))
        got = bool(cur.fetchone()[0])
    if not got:
        conn.close()
        raise TestRunnerConflict("另一个 pytest/live 进程正持有 qstock_test runner 锁")
    _state.held = True
    try:
        yield
    finally:
        _state.held = False
        conn.close()


def reset_schema(dsn: str) -> None:
    """终止隔离库残留会话并重建空 public schema。"""
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = %s AND pid <> pg_backend_pid()",
                (conn.info.dbname,),
            )
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    finally:
        conn.close()
