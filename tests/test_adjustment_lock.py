"""复权事实写入与切换的并发序列化（issue #166）。"""

import threading

import psycopg2
import pytest

from qstock_mcp.adjustment import rebuild_stock
from qstock_mcp.adjustment_lock import ADJUSTMENT_FACT_LOCK_KEY
from qstock_mcp.adjustment_switch import run_adjustment_switch
from qstock_mcp.db import ensure_schema


def _conn(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    return conn


def _raw():
    return {
        "trade_date": "20240102",
        "open": 10.0,
        "high": 10.0,
        "low": 10.0,
        "close": 10.0,
        "preclose": 10.0,
        "volume": 1000,
        "amount": 100.0,
        "tradestatus": 1,
        "source": "baostock",
    }


@pytest.mark.parametrize("operation", ["rebuild", "cutover"])
def test_fact_write_and_switch_wait_for_the_same_lock(pg_test, operation, tmp_path):
    blocker = _conn(pg_test)
    with blocker.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADJUSTMENT_FACT_LOCK_KEY,))
    result = []
    errors = []

    def run():
        try:
            if operation == "rebuild":
                conn = _conn(pg_test)
                result.append(rebuild_stock(conn, "600519", [_raw()], []))
                conn.close()
            else:
                result.append(
                    run_adjustment_switch(
                        "cutover", report_dir=str(tmp_path / "cutover")
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 线程错误向主测试线程传递
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(0.2)
    assert worker.is_alive()

    blocker.rollback()
    worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert result

    if operation == "cutover":
        assert run_adjustment_switch(
            "rollback", report_dir=str(tmp_path / "rollback")
        )["status"] == "ok"
    blocker.close()
