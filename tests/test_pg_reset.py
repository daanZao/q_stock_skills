"""pg_test 的数据清理、显式 schema 重建与 runner 隔离回归。"""

import os
from pathlib import Path
import subprocess
import sys

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from qstock_mcp.db import ensure_schema

from conftest import reset_test_db, reset_test_schema

ROOT = Path(__file__).resolve().parents[1]


def test_reset_preserves_schema_and_clears_committed_rows(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stock_list (stock_code, stock_name) VALUES ('600519', '贵州茅台')"
        )
    conn.commit()
    conn.close()

    reset_test_db(pg_test)
    reset_test_db(pg_test)

    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.stock_list') IS NOT NULL")
        assert cur.fetchone()[0] is True
        cur.execute("SELECT count(*) FROM stock_list")
        assert cur.fetchone()[0] == 0
    conn.close()


def test_reset_terminates_leaked_connection(pg_test):
    """模拟上个用例断言失败：连接未提交（idle in transaction）持锁、未 close。

    reset_test_db 须终止该残留会话并完成清库；泄漏连接此后不可再用。
    """
    leaked = psycopg2.connect(pg_test)
    try:
        leaked.cursor().execute("CREATE TABLE leak_t (id int)")  # 未提交，持锁
        reset_test_db(pg_test)  # 不阻塞即通过；残留会话已被终止
        with pytest.raises(psycopg2.OperationalError):
            leaked.cursor().execute("SELECT 1")  # 连接已被 terminate
    finally:
        leaked.close()  # 本用例自己收尾，不依赖后继 fixture 兜底


def test_reset_recreates_empty_schema(pg_test):
    """显式 schema 迁移用例仍可取得空 public schema。"""
    conn = psycopg2.connect(pg_test)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE leftover (id int)")
    conn.close()
    reset_test_schema(pg_test)
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
        assert cur.fetchone()[0] == 0
    conn.close()


def test_pg_unreachable_skips_db_test():
    env = os.environ.copy()
    env["QSTOCK_TEST_MAINT_DSN"] = "postgresql://localhost:1/postgres"
    env["QSTOCK_TEST_PG_DSN"] = "postgresql://localhost:1/qstock_test"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_init_database.py::test_init_creates_all_tables",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 skipped" in output


def test_shared_runner_lock_rejects_second_pytest(pg_test):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_init_database.py::test_missing_pg_dsn_returns_clear_error",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 3, output
    assert "另一个 pytest/live 进程" in output
