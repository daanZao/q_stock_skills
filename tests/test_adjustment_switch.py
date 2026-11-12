"""复权新模型切换、回滚与 roll-forward 演练（issue #166）。"""

import psycopg2

from qstock_mcp.adjustment import rebuild_stock
from qstock_mcp.adjustment_switch import run_adjustment_switch
from qstock_mcp.db import ensure_schema, list_tables
from qstock_mcp.repository import upsert_daily
from qstock_mcp.tools_daily import query_daily


def _conn(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    return conn


def _raw(trade_date, close):
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
        "change_percent": 0.0,
        "change_amount": None,
        "turnover_rate": 1.0,
        "tradestatus": 1,
        "source": "baostock",
    }


def test_cutover_schema_idempotency_legacy_boundary_and_immediate_rollback(
    pg_test, monkeypatch, tmp_path
):
    conn = _conn(pg_test)
    upsert_daily(conn, "600519", "qfq", [_raw("20221230", 9.0)], "legacy-source")
    rebuild_stock(conn, "600519", [_raw("20240102", 10.0)], [], baseline_factor=1.0)
    conn.close()

    cutover = run_adjustment_switch("cutover", report_dir=str(tmp_path / "cutover"))

    assert cutover["status"] == "ok"
    assert cutover["mode"] == "new"
    assert cutover["required_env"] == {"QSTOCK_PRICE_SOURCE": "new"}
    conn = _conn(pg_test)
    assert "stock_daily_legacy" in list_tables(conn)
    assert "stock_daily" not in list_tables(conn)
    ensure_schema(conn)
    assert "stock_daily" not in list_tables(conn)
    conn.close()

    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")
    legacy = query_daily("600519", start="20221230", end="20221230")
    assert legacy["status"] == "ok"
    assert legacy["rows"][0]["close"] == 9.0
    assert legacy["rows"][0]["price_basis"] == "legacy_qfq"

    rollback = run_adjustment_switch("rollback", report_dir=str(tmp_path / "rollback"))

    assert rollback["status"] == "ok"
    assert rollback["mode"] == "legacy"
    assert rollback["repair_required"] is False
    assert rollback["repair"] == {"none": 0, "hfq": 0, "qfq": 0}
    conn = _conn(pg_test)
    assert "stock_daily" in list_tables(conn)
    assert "stock_daily_legacy" not in list_tables(conn)
    assert "stock_daily_raw" in list_tables(conn)
    conn.close()


def test_rollback_repairs_reanchored_legacy_and_roll_forward_keeps_facts(
    pg_test, monkeypatch, tmp_path
):
    conn = _conn(pg_test)
    upsert_daily(conn, "600519", "qfq", [_raw("20240102", 10.0)], "legacy-source")
    rebuild_stock(conn, "600519", [_raw("20240102", 10.0)], [], baseline_factor=1.0)
    conn.close()
    assert run_adjustment_switch(
        "cutover", report_dir=str(tmp_path / "cutover")
    )["status"] == "ok"

    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "600519",
        [_raw("20240102", 10.0), _raw("20240103", 11.0)],
        [{"ex_date": "20240103", "factor_cum": 2.0}],
        baseline_factor=1.0,
    )
    conn.close()

    rollback = run_adjustment_switch("rollback", report_dir=str(tmp_path / "rollback"))

    assert rollback["status"] == "ok"
    assert rollback["repair_required"] is True
    assert rollback["repair"] == {"none": 2, "hfq": 2, "qfq": 2}
    assert rollback["upstream_requests"] == 0
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "legacy")
    repaired = query_daily("600519", start="20240102", end="20240103")
    assert repaired["status"] == "ok"
    assert [row["close"] for row in repaired["rows"]] == [5.0, 11.0]

    forward = run_adjustment_switch(
        "roll-forward", report_dir=str(tmp_path / "roll-forward")
    )
    assert forward["status"] == "ok"
    assert forward["mode"] == "new"
    assert forward["upstream_requests"] == 0

    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "600519",
        [_raw("20240102", 10.0), _raw("20240103", 11.0), _raw("20240104", 12.0)],
        [{"ex_date": "20240103", "factor_cum": 2.0}],
        baseline_factor=1.0,
    )
    conn.close()
    second_rollback = run_adjustment_switch(
        "rollback", report_dir=str(tmp_path / "second-rollback")
    )
    assert second_rollback["status"] == "ok"
    assert second_rollback["repair"]["qfq"] == 3
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "legacy")
    continued = query_daily("600519", start="20240102", end="20240104")
    assert [row["close"] for row in continued["rows"]] == [5.0, 11.0, 12.0]


def test_adj_switch_cli_status(pg_test, capsys):
    import json

    from qstock_mcp import scheduler

    rc = scheduler.main(["adj-switch", "status"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["job"] == "adj-switch"
    assert out["mode"] == "legacy"


def test_archived_legacy_table_supports_global_legacy_reads(pg_test, monkeypatch, tmp_path):
    from qstock_mcp.tools_strong import compute_strong_rows
    from qstock_mcp.tools_sw_board import query_sw_board_daily

    conn = _conn(pg_test)
    upsert_daily(
        conn,
        "600519",
        "qfq",
        [_raw("20240102", 10.0), _raw("20240103", 11.0)],
        "legacy-source",
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sw_industry_meta (industry_code, name, level)"
            " VALUES ('240000', '有色金属', 1)"
        )
        cur.execute(
            "INSERT INTO sw_industry_class"
            " (stock_code, board_code, effective_date, source_system)"
            " VALUES ('600519', '240101', '20200101', 'sw')"
        )
    conn.commit()
    conn.close()
    assert run_adjustment_switch(
        "cutover", report_dir=str(tmp_path / "cutover")
    )["status"] == "ok"
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "legacy")
    monkeypatch.setattr("qstock_mcp.tools_strong.HIGH_WINDOW_DAYS", 2)
    monkeypatch.setattr("qstock_mcp.tools_strong.MULTI_ZT_WINDOW_DAYS", 2)
    try:
        conn = _conn(pg_test)
        strong = compute_strong_rows(conn, "20240103")
        conn.close()
        board = query_sw_board_daily()

        assert strong is not None
        assert [(row["stock_code"], row["latest_price"]) for row in strong] == [
            ("600519", 11.0)
        ]
        assert board["status"] == "ok"
        assert board["trade_date"] == "20240103"
        assert board["rows"][0]["change_pct"] == 0.0
    finally:
        run_adjustment_switch("rollback", report_dir=str(tmp_path / "rollback"))


def test_report_failure_does_not_misreport_committed_cutover(pg_test, monkeypatch, tmp_path):
    from qstock_mcp import adjustment_switch

    monkeypatch.setattr(
        adjustment_switch,
        "_write_report",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = run_adjustment_switch("cutover", report_dir=str(tmp_path / "unwritable"))

    assert result["status"] == "partial_error"
    assert result["committed"] is True
    assert result["mode"] == "new"
    assert "disk full" in result["error"]

    monkeypatch.setattr(
        adjustment_switch,
        "_write_report",
        lambda report_dir, action, payload: None,
    )
    restored = run_adjustment_switch("rollback", report_dir=str(tmp_path / "rollback"))
    assert restored["status"] == "ok"


def test_non_test_database_requires_explicit_production_gate(monkeypatch):
    from types import SimpleNamespace

    from qstock_mcp import adjustment_switch

    conn = SimpleNamespace(
        info=SimpleNamespace(dbname="qstock"),
        close=lambda: None,
    )
    monkeypatch.setattr(adjustment_switch, "connect", lambda: (conn, None))

    result = adjustment_switch.run_adjustment_switch("cutover")

    assert result["status"] == "error"
    assert result["error_kind"] == "production_not_enabled"
    assert "QSTOCK_ADJ_SWITCH_ENABLE_PRODUCTION" in result["error"]


def test_rollback_removes_stale_qfq_when_anchor_becomes_unavailable(
    pg_test, monkeypatch, tmp_path
):
    conn = _conn(pg_test)
    upsert_daily(conn, "600519", "qfq", [_raw("20240102", 10.0)], "legacy-source")
    rebuild_stock(conn, "600519", [_raw("20240102", 10.0)], [], baseline_factor=1.0)
    conn.close()
    assert run_adjustment_switch(
        "cutover", report_dir=str(tmp_path / "cutover")
    )["status"] == "ok"

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

    rollback = run_adjustment_switch("rollback", report_dir=str(tmp_path / "rollback"))

    assert rollback["status"] == "ok"
    assert rollback["repair"] == {"none": 2, "hfq": 0, "qfq": 0}
    assert rollback["verification"] == {
        "missing_none": 0,
        "missing_hfq": 0,
        "missing_qfq": 0,
        "stale_qfq": 0,
    }
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "legacy")
    from qstock_mcp.price_reader import select_daily

    conn = _conn(pg_test)
    rows = select_daily(conn, "600519", "qfq", "20240102", "20240103")
    conn.close()
    assert rows == []
