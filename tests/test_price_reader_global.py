"""全局个股价格读取的 new 实现契约（issue #166）。"""

import psycopg2
import pytest

from qstock_mcp.adjustment import rebuild_stock
from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_strong import compute_strong_rows


def _conn(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    return conn


def _raw(trade_date, close, *, pct=0.0):
    return {
        "trade_date": trade_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "preclose": close,
        "volume": 1000,
        "amount": 100.0,
        "amplitude": None,
        "change_percent": pct,
        "change_amount": None,
        "turnover_rate": 1.0,
        "tradestatus": 1,
        "source": "baostock",
    }


def test_new_reader_supports_strong_stock_window_and_cross_section(pg_test, monkeypatch):
    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "600519",
        [_raw("20240102", 10.0), _raw("20240103", 11.0, pct=-45.4545)],
        [{"ex_date": "20240103", "factor_cum": 2.0}],
        baseline_factor=1.0,
    )
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")
    monkeypatch.setattr("qstock_mcp.tools_strong.HIGH_WINDOW_DAYS", 2)
    monkeypatch.setattr("qstock_mcp.tools_strong.MULTI_ZT_WINDOW_DAYS", 2)

    conn = _conn(pg_test)
    rows = compute_strong_rows(conn, "20240103")
    conn.close()

    assert rows is not None
    assert [(row["stock_code"], row["reason"]) for row in rows] == [
        ("600519", "60日新高")
    ]
    assert rows[0]["latest_price"] == pytest.approx(11.0)
    assert rows[0]["change_percent"] == pytest.approx(-45.4545)
    assert rows[0]["amount"] == pytest.approx(100.0)


def test_new_reader_supports_sw_board_latest_date_and_aggregation(pg_test, monkeypatch):
    from qstock_mcp.tools_sw_board import query_sw_board_daily

    conn = _conn(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sw_industry_meta (industry_code, name, level)"
            " VALUES ('240000', '有色金属', 1)"
        )
        cur.execute(
            "INSERT INTO sw_industry_class"
            " (stock_code, board_code, effective_date, source_system)"
            " VALUES ('600519', '240101', '20200101', 'sw'),"
            " ('000001', '240101', '20200101', 'sw')"
        )
    conn.commit()
    for code, amount, pct in (("600519", 100.0, 1.0), ("000001", 300.0, 3.0)):
        first = _raw("20240102", 10.0)
        latest = {**_raw("20240103", 11.0, pct=pct), "amount": amount}
        rebuild_stock(conn, code, [first, latest], [], baseline_factor=1.0)
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    result = query_sw_board_daily()

    assert result["status"] == "ok"
    assert result["trade_date"] == "20240103"
    assert result["adj"] == "qfq"
    assert result["count"] == 1
    row = result["rows"][0]
    assert row["industry_code"] == "240000"
    assert row["change_pct"] == pytest.approx(2.5)
    assert row["amount"] == pytest.approx(400.0)
    assert (row["up_count"], row["stock_count"], row["counted_count"]) == (2, 2, 2)


def test_new_coverage_rejects_pending_even_when_raw_dates_are_complete(pg_test, monkeypatch):
    from qstock_mcp.repository import upsert_stock_list
    from qstock_mcp.tools_coverage import verify_history_coverage

    conn = _conn(pg_test)
    upsert_stock_list(conn, [{"stock_code": "600519", "stock_name": "贵州茅台"}], "fake")
    rebuild_stock(
        conn,
        "600519",
        [_raw("20240102", 10.0), _raw("20240103", 11.0)],
        [],
        baseline_factor=1.0,
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE stock_factor_state SET rebuild_pending=true WHERE stock_code='600519'"
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    result = verify_history_coverage("20240102", "20240103")

    entry = result["sections"]["stock_daily"][0]
    assert entry["missing_days"] == []
    assert entry["coverage"] == "partial"
    assert entry["status"] == "gap"
    assert entry["note"]
    assert result["status"] == "error"
    assert result["summary"]["gaps"] >= 1


def test_new_strong_stock_never_falls_back_to_raw_when_hfq_is_unavailable(
    pg_test, monkeypatch
):
    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "600519",
        [
            _raw("20240102", 10.0),
            {**_raw("20240103", 11.0), "change_percent": None},
        ],
        [{"ex_date": "20240103", "factor_cum": 2.0}],
    )
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")
    monkeypatch.setattr("qstock_mcp.tools_strong.HIGH_WINDOW_DAYS", 2)
    monkeypatch.setattr("qstock_mcp.tools_strong.MULTI_ZT_WINDOW_DAYS", 2)

    conn = _conn(pg_test)
    rows = compute_strong_rows(conn, "20240103")
    conn.close()

    assert rows == []
