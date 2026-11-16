"""query_daily 新复权读取契约：raw + B 派生、覆盖状态与 legacy 边界（#162）。"""

import psycopg2
import pytest

from qstock_mcp.adjustment import rebuild_stock
from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_daily import query_daily


@pytest.fixture(autouse=True)
def _block_unplanned_upstream(monkeypatch):
    monkeypatch.setattr(
        "qstock_mcp.tools_daily.default_adjustment_adapters", lambda: []
    )


def _conn(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    return conn


def _raw(trade_date, close, *, pct=0.0, source="baostock"):
    return {
        "trade_date": trade_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "preclose": close,
        "volume": 1000,
        "amount": 10000.0,
        "amplitude": None,
        "change_percent": pct,
        "change_amount": None,
        "turnover_rate": 1.2,
        "tradestatus": 1,
        "source": source,
    }


def _factor_evidence(conn, *dates, event_dates=()):
    from qstock_mcp.ledger import record_collection

    for day in dates:
        has_event = day in event_dates
        record_collection(
            conn,
            trade_date=day,
            section="stock_adjust_factor",
            status="ok" if has_event else "empty",
            source="baostock",
            rows=int(has_event),
            detail="morning_batch:test",
        )


def test_new_qfq_is_derived_from_hfq_and_latest_anchor(pg_test, monkeypatch):
    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "600519",
        [_raw("20240102", 10.0), _raw("20240103", 11.0, pct=120.0)],
        [{"ex_date": "20240103", "factor_cum": 2.0}],
        baseline_factor=1.0,
    )
    _factor_evidence(conn, "20240102", "20240103", event_dates={"20240103"})
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    result = query_daily("600519", start="20240102", end="20240103")

    assert result["status"] == "ok"
    assert result["params"]["adj"] == "qfq"
    assert result["adjust"] == {
        "anchor": "20240103",
        "events": [{
            "ex_date": "20240103",
            "factor_cum": 2.0,
            "event_factor": 0.5,
            "kind": None,
            "detail": None,
        }],
        "coverage": "complete",
    }
    assert [row["close"] for row in result["rows"]] == pytest.approx([5.0, 11.0])
    assert [row["price_basis"] for row in result["rows"]] == ["qfq", "qfq"]
    assert [row["source"] for row in result["rows"]] == ["baostock", "baostock"]
    assert result["healed"] == []


@pytest.mark.parametrize(
    "adj,basis,expected_close",
    [("hfq", "hfq", [10.0, 22.0]), ("none", "raw", [10.0, 11.0])],
)
def test_new_hfq_and_raw_keep_formal_pctchg_fallback_complete(
    pg_test, monkeypatch, adj, basis, expected_close
):
    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "600519",
        [_raw("20240102", 10.0), _raw("20240103", 11.0, pct=120.0)],
        [{
            "ex_date": "20240103",
            "factor_cum": 2.0,
            "source": "baostock-pctchg-derived",
        }],
        baseline_factor=1.0,
    )
    _factor_evidence(conn, "20240102", "20240103", event_dates={"20240103"})
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    result = query_daily("600519", start="20240102", end="20240103", adj=adj)

    assert result["status"] == "ok"
    assert result["adjust"]["coverage"] == "complete"
    assert "note" not in result
    assert [row["close"] for row in result["rows"]] == pytest.approx(expected_close)
    assert [row["price_basis"] for row in result["rows"]] == [basis, basis]


def test_unknown_baseline_returns_raw_but_rejects_derived_prices(pg_test, monkeypatch):
    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "688300",
        [_raw("20240103", 11.0)],
        [{"ex_date": "20240103", "factor_cum": 2.0}],
    )
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    raw = query_daily("688300", start="20240103", end="20240103", adj="none")
    qfq = query_daily("688300", start="20240103", end="20240103")

    assert raw["status"] == "ok"
    assert raw["rows"][0]["close"] == 11.0
    assert raw["rows"][0]["price_basis"] == "raw"
    assert raw["adjust"]["coverage"] == "partial"
    assert raw["note"]
    assert qfq["status"] == "error"
    assert qfq["error_kind"] == "data_unavailable"
    assert "rows" not in qfq


def test_pending_state_with_valid_anchor_returns_partial_qfq(pg_test, monkeypatch):
    conn = _conn(pg_test)
    rebuild_stock(conn, "600519", [_raw("20240102", 10.0)], [], baseline_factor=1.0)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE stock_factor_state SET rebuild_pending=true WHERE stock_code='600519'"
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    hfq = query_daily("600519", start="20240102", end="20240102", adj="hfq")
    qfq = query_daily("600519", start="20240102", end="20240102")

    assert hfq["status"] == "ok"
    assert hfq["adjust"]["coverage"] == "partial"
    assert hfq["note"]
    assert qfq["status"] == "ok"
    assert qfq["rows"][0]["close"] == 10.0
    assert qfq["adjust"]["coverage"] == "partial"
    assert qfq["note"]


def test_raw_gap_is_partial_with_note(pg_test, monkeypatch):
    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "600519",
        [_raw("20240102", 10.0), _raw("20240104", 12.0)],
        [],
        baseline_factor=1.0,
    )
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    result = query_daily("600519", start="20240102", end="20240104")

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["adjust"]["coverage"] == "partial"
    assert result["note"]


def test_new_reader_uses_legacy_only_for_pre_window_qfq(pg_test, monkeypatch):
    from qstock_mcp.repository import upsert_daily

    conn = _conn(pg_test)
    upsert_daily(
        conn, "600519", "qfq", [_raw("20221230", 10.0)], "legacy-source"
    )
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    result = query_daily("600519", start="20221230", end="20221230")

    assert result["status"] == "ok"
    assert result["rows"][0]["close"] == 10.0
    assert result["rows"][0]["price_basis"] == "legacy_qfq"
    assert result["adjust"] == {
        "anchor": "20221230", "events": [], "coverage": "partial"
    }
    assert result["note"]


def test_new_reader_rejects_mixed_qfq_and_legacy_backfill(pg_test, monkeypatch):
    conn = _conn(pg_test)
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    mixed = query_daily("600519", start="20221230", end="20230103")
    old_hfq = query_daily("600519", start="20221230", end="20221230", adj="hfq")
    old_raw = query_daily("600519", start="20221230", end="20230103", adj="none")

    assert mixed["status"] == "error"
    assert mixed["error_kind"] == "invalid_range"
    assert "分别查询" in mixed["error"]
    for result in (old_hfq, old_raw):
        assert result["status"] == "error"
        assert result["error_kind"] == "data_unavailable"
        assert "不从 legacy qfq 反推" in result["error"]
        assert "rows" not in result


def test_legacy_default_contract_is_additive_and_explicitly_partial(pg_test, monkeypatch):
    from qstock_mcp.repository import upsert_daily

    conn = _conn(pg_test)
    upsert_daily(conn, "600519", "qfq", [_raw("20240102", 10.0)], "tencent")
    conn.close()
    monkeypatch.delenv("QSTOCK_PRICE_SOURCE", raising=False)

    result = query_daily("600519", start="20240102", end="20240102")

    assert result["status"] == "ok"
    assert result["params"]["adj"] == "qfq"
    assert result["rows"][0]["price_basis"] == "legacy_qfq"
    assert result["adjust"] == {
        "anchor": "20240102", "events": [], "coverage": "partial"
    }
    assert result["note"]


def test_event_factor_uses_previous_event_outside_response_window(pg_test, monkeypatch):
    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "600519",
        [_raw("20240102", 10.0), _raw("20240103", 11.0), _raw("20240104", 12.0)],
        [
            {"ex_date": "20240103", "factor_cum": 2.0},
            {"ex_date": "20240104", "factor_cum": 4.0},
        ],
        baseline_factor=1.0,
    )
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    result = query_daily("600519", start="20240104", end="20240104")

    assert result["adjust"]["events"][0]["factor_cum"] == 4.0
    assert result["adjust"]["events"][0]["event_factor"] == pytest.approx(0.5)


def test_invalid_raw_close_blocks_complete_for_raw_query(pg_test, monkeypatch):
    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "600519",
        [_raw("20240102", 10.0), _raw("20240103", 0.0)],
        [],
    )
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    result = query_daily("600519", start="20240102", end="20240103", adj="none")

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["adjust"]["coverage"] == "partial"
    assert result["note"]


def test_latest_unresolved_factor_anomaly_blocks_complete(pg_test, monkeypatch):
    from qstock_mcp.ledger import record_collection

    conn = _conn(pg_test)
    rebuild_stock(conn, "600519", [_raw("20240102", 10.0)], [], baseline_factor=1.0)
    record_collection(
        conn,
        trade_date="20240102",
        section="stock_adjust_factor",
        status="error",
        error_kind="factor_mismatch",
    )
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")

    unresolved = query_daily("600519", start="20240102", end="20240102")
    assert unresolved["adjust"]["coverage"] == "partial"
    assert unresolved["note"]

    conn = _conn(pg_test)
    record_collection(
        conn,
        trade_date="20240102",
        section="stock_adjust_factor",
        status="ok",
        source="baostock",
        detail="morning_batch:resolved",
    )
    rebuild_stock(conn, "600519", [], [], baseline_factor=1.0)
    conn.close()
    resolved = query_daily("600519", start="20240102", end="20240102")
    assert resolved["adjust"]["coverage"] == "complete"


def test_new_query_self_heals_facts_without_baseline(pg_test, monkeypatch):
    from fakes import FakeAdjustmentAdapter

    conn = _conn(pg_test)
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")
    adapter = FakeAdjustmentAdapter()

    result = query_daily(
        "600519",
        start="20240102",
        end="20240105",
        adj="none",
        adapters=[adapter],
    )
    qfq = query_daily(
        "600519", start="20240102", end="20240105", adapters=[adapter]
    )

    assert result["status"] == "ok"
    assert result["count"] == 4
    assert result["healed"] == [
        {"start": "20240102", "end": "20240105", "source": "baostock", "rows": 4}
    ]
    assert result["adjust"]["coverage"] == "partial"
    assert [row["close"] for row in result["rows"]] == [10.0, 10.0, 10.0, 10.0]
    assert qfq["status"] == "error"
    assert qfq["error_kind"] == "data_unavailable"
    assert [call["adj"] for call in adapter.calls] == ["none"]
    assert len(adapter.factor_calls) == 4
    conn = _conn(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM stock_daily_hfq")
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT baseline_factor, factor_latest, rebuild_pending"
            " FROM stock_factor_state WHERE stock_code='600519'"
        )
        assert cur.fetchone() == (None, None, True)
    conn.close()


def test_new_query_empty_baostock_raw_can_use_single_stock_alternative(pg_test, monkeypatch):
    from fakes import FakeAdjustmentAdapter, FakeAdapter

    conn = _conn(pg_test)
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")
    baostock = FakeAdjustmentAdapter(rows=[])
    tencent = FakeAdapter("tencent", rows_fn=lambda start, end: [
        _raw("20240105", 12.0, source="tencent")
    ])

    result = query_daily(
        "600519",
        start="20240105",
        end="20240105",
        adj="none",
        adapters=[baostock, tencent],
    )

    assert result["status"] == "ok"
    assert result["rows"][0]["close"] == 12.0
    assert result["rows"][0]["source"] == "tencent"
    assert result["adjust"]["coverage"] == "partial"
    assert result["healed"][0]["source"] == "tencent"
    assert len(tencent.calls) == 1


def test_new_query_intraday_defers_current_raw_and_factor_write(pg_test, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from fakes import FakeAdjustmentAdapter

    conn = _conn(pg_test)
    rebuild_stock(conn, "600519", [_raw("20240104", 10.0)], [], baseline_factor=1.0)
    _factor_evidence(conn, "20240104")
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")
    adapter = FakeAdjustmentAdapter()
    now = datetime(2024, 1, 5, 10, 0, tzinfo=timezone(timedelta(hours=8)))

    result = query_daily(
        "600519",
        start="20240104",
        end="20240105",
        adapters=[adapter],
        now=now,
    )

    assert result["status"] == "ok"
    assert result["deferred_rows"] == 1
    assert result["adjust"]["coverage"] == "partial"
    assert [row["trade_date"] for row in result["rows"]] == ["20240104"]
    assert adapter.factor_calls == []
    conn = _conn(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM stock_daily_raw")
        assert cur.fetchone()[0] == 1
    conn.close()


def test_new_query_factor_unknown_returns_raw_partial_but_not_qfq(pg_test, monkeypatch):
    from fakes import FakeAdjustmentAdapter

    class FactorUnavailable(FakeAdjustmentAdapter):
        def fetch_daily_adjust_factor(self, trade_date):
            self.factor_calls.append(trade_date)
            from qstock_mcp.adapters import FetchError

            raise FetchError("factor unavailable", kind="connection")

    conn = _conn(pg_test)
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")
    adapter = FactorUnavailable()

    raw = query_daily(
        "600519",
        start="20240102",
        end="20240105",
        adj="none",
        adapters=[adapter],
    )
    qfq = query_daily(
        "600519",
        start="20240102",
        end="20240105",
        adapters=[adapter],
    )

    assert raw["status"] == "ok"
    assert raw["count"] == 4
    assert raw["adjust"]["coverage"] == "partial"
    assert raw["note"]
    assert qfq["status"] == "error"
    assert qfq["error_kind"] == "data_unavailable"


def test_full_backfill_establishes_baseline_independent_of_query_order(
    pg_test, monkeypatch, tmp_path
):
    from fakes import FakeAdjustmentAdapter
    from qstock_mcp.adjustment_backfill import run_adjustment_backfill

    conn = _conn(pg_test)
    conn.close()
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")
    monkeypatch.setenv("QSTOCK_ADJ_WINDOW_START", "20240102")
    backfill = run_adjustment_backfill(
        start="20240102",
        end="20240105",
        adapter=FakeAdjustmentAdapter(),
        report_dir=str(tmp_path),
        throttle=0,
    )
    assert backfill["status"] == "ok"

    recent = query_daily("600519", start="20240104", end="20240105", adapters=[])
    older = query_daily("600519", start="20240102", end="20240103", adapters=[])

    assert recent["adjust"]["coverage"] == older["adjust"]["coverage"] == "complete"
    assert [row["close"] for row in recent["rows"]] == [10.0, 10.0]
    assert [row["close"] for row in older["rows"]] == [10.0, 10.0]
    conn = _conn(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT baseline_factor, rebuild_pending"
            " FROM stock_factor_state WHERE stock_code='600519'"
        )
        baseline, pending = cur.fetchone()
        assert float(baseline) == 1.0
        assert pending is False
    conn.close()
