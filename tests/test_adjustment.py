"""复权单票竖切：raw + 累计 B → hfq/state → qfq（issue #160）。"""

import psycopg2
import pytest

from qstock_mcp.adjustment import rebuild_stock
from qstock_mcp.db import ensure_schema


def _conn(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    return conn


def _raw(trade_date, close, *, preclose=None, pct=0.0, tradestatus=1):
    return {
        "trade_date": trade_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "preclose": preclose,
        "volume": 1000 if tradestatus else 0,
        "amount": 10000.0,
        "amplitude": None,
        "change_percent": pct,
        "change_amount": None,
        "turnover_rate": None,
        "tradestatus": tradestatus,
        "source": "baostock",
    }


def test_688300_normalized_golden(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "688300",
        [
            _raw("20261015", 136.01),
            _raw("20261016", 102.70, preclose=104.62, pct=-1.8352),
        ],
        [{"ex_date": "20261016", "factor_cum": 1.300038204889484}],
        baseline_factor=1.0,
    )

    assert snapshot["state"]["baseline_factor"] == pytest.approx(1.0)
    assert snapshot["state"]["factor_latest"] == pytest.approx(1.300038204889484)
    assert snapshot["state"]["rebuild_pending"] is False
    assert [row["factor_cum"] for row in snapshot["hfq_rows"]] == pytest.approx(
        [1.0, 1.300038204889484]
    )
    assert [row["close"] for row in snapshot["hfq_rows"]] == pytest.approx(
        [136.01, 133.51392364215]
    )
    assert [row["close"] for row in snapshot["qfq_rows"]] == pytest.approx(
        [104.6200023110568, 102.70]
    )
    assert snapshot["factor_events"][0]["event_factor"] == pytest.approx(0.769208)
    conn.close()


def test_688300_real_cumulative_factors_derive_event_factor(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "688300",
        [
            _raw("20261015", 136.01),
            _raw("20261016", 102.70, preclose=104.62, pct=-1.8352),
        ],
        [
            {"ex_date": "20260610", "factor_cum": 2.965589},
            {"ex_date": "20261016", "factor_cum": 3.855379},
        ],
        baseline_factor=2.965589,
    )
    assert snapshot["factor_events"][1]["event_factor"] == pytest.approx(
        0.7692081634516347
    )
    assert snapshot["hfq_rows"][1]["factor_cum"] == pytest.approx(1.300038204889484)
    conn.close()


def test_688300_derives_first_window_baseline(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "688300",
        [
            _raw("20261015", 136.01),
            _raw("20261016", 102.70, preclose=104.62, pct=-1.8352),
        ],
        [{"ex_date": "20261016", "factor_cum": 3.855379}],
    )

    assert snapshot["state"]["baseline_factor"] == pytest.approx(2.965588537153224)
    assert snapshot["state"]["factor_latest"] == pytest.approx(1.300038204889484)
    assert snapshot["state"]["rebuild_pending"] is False
    assert snapshot["factor_events"][0]["event_factor"] == pytest.approx(
        0.7692080433994231
    )
    conn.close()


def test_600381_reverse_event_on_suspended_day(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "600381",
        [
            _raw("20140626", 2.08, tradestatus=0),
            _raw("20140627", 16.75, preclose=16.75, tradestatus=0),
        ],
        [
            {"ex_date": "20120515", "factor_cum": 7.192060},
            {"ex_date": "20140627", "factor_cum": 0.893104},
        ],
        baseline_factor=7.192060,
    )

    assert snapshot["factor_events"][1]["event_factor"] == pytest.approx(8.052880739533134)
    assert [row["factor_cum"] for row in snapshot["hfq_rows"]] == pytest.approx(
        [1.0, 0.124179104477612]
    )
    assert snapshot["hfq_rows"][1]["factor_cum"] < snapshot["hfq_rows"][0]["factor_cum"]
    assert snapshot["hfq_rows"][1]["close"] == pytest.approx(2.08)
    assert snapshot["hfq_rows"][1]["tradestatus"] == 0
    conn.close()


def test_600094_resume_after_long_suspension(pg_test):
    """2026-11-04 受控补证：D-1/D 与全历史 B 均来自 baostock 原始响应。"""
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "600094",
        [
            _raw("20111010", 4.37, tradestatus=0),
            _raw("20111011", 7.67, preclose=5.83, pct=31.5609, tradestatus=1),
        ],
        [
            {"ex_date": "20070319", "factor_cum": 2.646807},
            {"ex_date": "20111011", "factor_cum": 1.983970},
        ],
        baseline_factor=2.646807,
    )

    assert snapshot["factor_events"][1]["event_factor"] == pytest.approx(1.334096)
    assert snapshot["hfq_rows"][1]["factor_cum"] < snapshot["hfq_rows"][0]["factor_cum"]
    assert snapshot["hfq_rows"][0]["tradestatus"] == 0
    assert snapshot["hfq_rows"][1]["tradestatus"] == 1
    conn.close()


def test_missing_first_baseline_is_pending_not_complete(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "688300",
        [_raw("20261016", 102.70, preclose=104.62, pct=-1.8352)],
        [{"ex_date": "20261016", "factor_cum": 3.855379}],
    )

    assert snapshot["state"]["baseline_factor"] is None
    assert snapshot["state"]["factor_latest"] is None
    assert snapshot["state"]["rebuild_pending"] is True
    assert snapshot["coverage"] == "partial"
    assert snapshot["hfq_rows"] == []
    assert snapshot["qfq_rows"] == []
    assert len(snapshot["raw_rows"]) == 1
    assert len(snapshot["factor_events"]) == 1
    conn.close()


def test_first_baseline_does_not_cross_raw_gap(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "688300",
        [
            _raw("20261014", 136.01),
            _raw("20261016", 102.70, preclose=104.62, pct=-1.8352),
        ],
        [{"ex_date": "20261016", "factor_cum": 3.855379}],
    )
    assert snapshot["coverage"] == "partial"
    assert snapshot["state"]["rebuild_pending"] is True
    assert snapshot["hfq_rows"] == []
    conn.close()


def test_invalid_close_is_quarantined_and_not_derived(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "600519",
        [
            _raw("20240102", 10.0),
            _raw("20240103", 0.0),
            _raw("20240104", None),
            _raw("20240105", 11.0),
        ],
        [],
    )

    assert len(snapshot["raw_rows"]) == 4
    assert snapshot["quarantined"] == [
        {"stock_code": "600519", "trade_date": "20240103", "reason": "invalid_close"},
        {"stock_code": "600519", "trade_date": "20240104", "reason": "invalid_close"},
    ]
    assert [row["trade_date"] for row in snapshot["hfq_rows"]] == ["20240102", "20240105"]
    assert snapshot["state"]["latest_trade_date"] == "20240105"
    assert snapshot["coverage"] == "partial"

    from qstock_mcp.tools_ledger import query_collection_log

    ledger = query_collection_log(section="stock_daily_raw", status="error")
    assert ledger["count"] == 1
    assert ledger["rows"][0]["error_kind"] == "invalid_close"
    assert "600519:20240103" in ledger["rows"][0]["detail"]
    assert "600519:20240104" in ledger["rows"][0]["detail"]
    conn.close()


def _business_tables(conn, stock_code):
    queries = {
        "raw": "SELECT stock_code, trade_date, open, high, low, close, preclose,"
               " volume, amount, amplitude, change_percent, change_amount,"
               " turnover_rate, tradestatus, source FROM stock_daily_raw"
               " WHERE stock_code=%s ORDER BY trade_date",
        "events": "SELECT stock_code, ex_date, factor_cum, kind, detail, reg_date, source"
                  " FROM stock_adjust_factor WHERE stock_code=%s ORDER BY ex_date",
        "hfq": "SELECT stock_code, trade_date, open, high, low, close, volume, amount,"
               " turnover_rate, tradestatus, factor_cum FROM stock_daily_hfq"
               " WHERE stock_code=%s ORDER BY trade_date",
        "state": "SELECT stock_code, baseline_factor, factor_latest, latest_trade_date,"
                 " rebuild_pending FROM stock_factor_state WHERE stock_code=%s",
    }
    with conn.cursor() as cur:
        result = {}
        for name, query in queries.items():
            cur.execute(query, (stock_code,))
            result[name] = cur.fetchall()
        return result


def test_same_input_rebuild_has_identical_business_snapshot(pg_test):
    conn = _conn(pg_test)
    args = (
        conn,
        "688300",
        [
            _raw("20261015", 136.01),
            _raw("20261016", 102.70, preclose=104.62, pct=-1.8352),
        ],
        [{"ex_date": "20261016", "factor_cum": 1.300038204889484}],
    )
    first = rebuild_stock(*args, baseline_factor=1.0)
    first_tables = _business_tables(conn, "688300")
    second = rebuild_stock(*args, baseline_factor=1.0)
    assert second == first
    assert _business_tables(conn, "688300") == first_tables
    conn.close()


def test_pending_preserves_invalid_close_quarantine_and_ledger(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "688300",
        [
            _raw("20261015", 0.0),
            _raw("20261016", 102.70, preclose=104.62, pct=-1.8352),
        ],
        [{"ex_date": "20261016", "factor_cum": 3.855379}],
    )
    assert len(snapshot["raw_rows"]) == 2
    assert snapshot["quarantined"] == [
        {"stock_code": "688300", "trade_date": "20261015", "reason": "invalid_close"}
    ]
    assert snapshot["coverage"] == "partial"

    from qstock_mcp.tools_ledger import query_collection_log

    ledger = query_collection_log(section="stock_daily_raw", status="error")
    assert ledger["count"] == 1
    assert "688300:20261015" in ledger["rows"][0]["detail"]
    conn.close()


def test_all_invalid_raw_is_partial_without_derived_or_state(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn, "600519", [_raw("20240102", 0.0), _raw("20240103", None)], []
    )
    assert snapshot["coverage"] == "partial"
    assert len(snapshot["raw_rows"]) == 2
    assert len(snapshot["quarantined"]) == 2
    assert snapshot["state"] is None
    assert snapshot["hfq_rows"] == []
    assert snapshot["qfq_rows"] == []
    conn.close()


def test_same_b_response_does_not_create_price_event(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "600519",
        [
            _raw("20230629", 1713.71),
            _raw("20230630", 1687.80, preclose=1687.80),
            _raw("20230703", 1691.00, preclose=1691.00),
        ],
        [
            {"ex_date": "20230630", "factor_cum": 6.889798},
            {"ex_date": "20230703", "factor_cum": 6.889798},
        ],
        baseline_factor=1.0,
    )

    assert [event["ex_date"] for event in snapshot["factor_events"]] == ["20230630"]
    assert [row["factor_cum"] for row in snapshot["hfq_rows"]] == pytest.approx(
        [1.0, 6.889798, 6.889798]
    )
    conn.close()


def test_authoritative_revision_and_retraction_rebuild_derived_rows(pg_test):
    conn = _conn(pg_test)
    raw = [_raw("20240102", 10.0), _raw("20240103", 11.0), _raw("20240104", 12.0)]
    rebuild_stock(
        conn, "600519", raw,
        [{"ex_date": "20240103", "factor_cum": 2.0}],
        baseline_factor=1.0,
    )

    revised = rebuild_stock(
        conn, "600519", [],
        [{"ex_date": "20240103", "factor_cum": 4.0}],
        authoritative_event_dates=("20240103",), baseline_factor=1.0,
    )
    assert [row["factor_cum"] for row in revised["hfq_rows"]] == pytest.approx([1.0, 4.0, 4.0])
    assert [row["close"] for row in revised["qfq_rows"]] == pytest.approx([2.5, 11.0, 12.0])

    retracted = rebuild_stock(
        conn, "600519", [], [], authoritative_event_dates=("20240103",)
    )
    assert retracted["factor_events"] == []
    assert [row["factor_cum"] for row in retracted["hfq_rows"]] == pytest.approx([1.0, 1.0, 1.0])
    assert [row["close"] for row in retracted["qfq_rows"]] == pytest.approx([10.0, 11.0, 12.0])
    conn.close()


def test_factor_state_check_accepts_pending_previous_values(pg_test):
    conn = _conn(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stock_factor_state (stock_code, baseline_factor, factor_latest,"
            " latest_trade_date, rebuild_pending) VALUES ('600519', 6.785630, 1.0,"
            " '20230630', true)"
        )
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "baseline,latest,pending",
    [
        (None, None, False),
        (1.0, None, True),
        (None, 1.0, True),
        (0.0, 1.0, False),
        (1.0, float("inf"), False),
    ],
)
def test_factor_state_check_rejects_invalid_shapes(
    pg_test, baseline, latest, pending
):
    conn = _conn(pg_test)
    with conn.cursor() as cur:
        with pytest.raises(
            (psycopg2.errors.CheckViolation, psycopg2.errors.NumericValueOutOfRange)
        ):
            cur.execute(
                "INSERT INTO stock_factor_state (stock_code, baseline_factor, factor_latest,"
                " latest_trade_date, rebuild_pending) VALUES ('600519', %s, %s,"
                " '20230630', %s)",
                (baseline, latest, pending),
            )
    conn.rollback()
    conn.close()


def test_600519_dirty_fifth_column_does_not_reset_derived_factor(pg_test):
    from qstock_mcp.adapters.baostock_adapter import map_adjust_factor_rows

    fields = "code,dividOperateDate,foreAdjustFactor,backAdjustFactor,adjustFacto".split(",")
    mapped = map_adjust_factor_rows(fields, [
        ["sh.600519", "2023-06-30", "0.898366", "6.889798", "0.988591"],
        ["sh.600519", "2023-07-03", "0.898366", "6.889798", "6.889798"],
    ])
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "600519",
        [
            _raw("20230629", 1713.71),
            _raw("20230630", 1687.80, preclose=1687.80),
            _raw("20230703", 1691.00, preclose=1691.00),
        ],
        [{"ex_date": "20221227", "factor_cum": 6.785630}] + mapped,
        baseline_factor=6.785630,
    )
    assert [event["ex_date"] for event in snapshot["factor_events"]] == [
        "20221227", "20230630",
    ]
    assert snapshot["factor_events"][1]["factor_cum"] == 6.889798
    assert snapshot["factor_events"][1]["event_factor"] == pytest.approx(
        0.9848808339518807
    )
    assert snapshot["state"]["factor_latest"] == pytest.approx(1.015351264363073)
    conn.close()


def test_window_backfill_derives_first_event_baseline_and_hides_pre_window_rows(pg_test):
    """#163：窗口外 D-1 只作 B0 计算输入；窗口内首个事件反推 B0。"""
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "688300",
        [
            _raw("20221230", 120.0),
            _raw("20230103", 130.0, preclose=120.0, pct=8.333333),
            _raw("20230104", 100.0, preclose=104.0, pct=-3.846154),
        ],
        [{"ex_date": "20230104", "factor_cum": 3.0}],
        window_start="20230103",
    )

    assert snapshot["state"]["baseline_factor"] == pytest.approx(2.4)
    assert snapshot["state"]["factor_latest"] == pytest.approx(1.25)
    assert snapshot["state"]["rebuild_pending"] is False
    assert [row["trade_date"] for row in snapshot["hfq_rows"]] == [
        "20230103", "20230104",
    ]
    assert [row["close"] for row in snapshot["hfq_rows"]] == pytest.approx([130.0, 125.0])
    assert [row["close"] for row in snapshot["qfq_rows"]] == pytest.approx([104.0, 100.0])
    assert [row["trade_date"] for row in snapshot["raw_rows"]] == [
        "20221230", "20230103", "20230104",
    ]
    assert snapshot["factor_audit"][0]["status"] == "ok"
    assert snapshot["factor_audit"][0]["price_factor"] == pytest.approx(0.8)
    conn.close()


def test_window_backfill_audit_flags_authoritative_price_path_mismatch(pg_test):
    """#163：同一事件双路径相对误差 >0.1% 时 pending，但保留已建立基线。"""
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "600519",
        [
            _raw("20230103", 100.0, preclose=100.0, pct=0.0),
            _raw("20230104", 80.0, preclose=80.0, pct=0.0),
            _raw("20230105", 90.0, preclose=72.0, pct=25.0),
        ],
        [
            {"ex_date": "20230104", "factor_cum": 3.0},
            {"ex_date": "20230105", "factor_cum": 4.0},
        ],
        window_start="20230103",
    )

    assert [item["status"] for item in snapshot["factor_audit"]] == ["ok", "mismatch"]
    assert snapshot["factor_audit"][1]["relative_error"] == pytest.approx(0.2)
    assert snapshot["state"]["rebuild_pending"] is True
    assert snapshot["state"]["baseline_factor"] == pytest.approx(2.4)
    assert snapshot["state"]["factor_latest"] == pytest.approx(4.0 / 2.4)
    assert len(snapshot["hfq_rows"]) == 3
    conn.close()


def test_window_backfill_audit_flags_missing_pctchg_as_unverified(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "600519",
        [
            _raw("20230103", 100.0, preclose=100.0, pct=0.0),
            _raw("20230104", 80.0, preclose=80.0, pct=0.0),
            _raw("20230105", 90.0, preclose=72.0, pct=None),
        ],
        [
            {"ex_date": "20230104", "factor_cum": 3.0},
            {"ex_date": "20230105", "factor_cum": 4.0},
        ],
        window_start="20230103",
    )

    assert [item["status"] for item in snapshot["factor_audit"]] == ["ok", "unverified"]
    assert snapshot["state"]["rebuild_pending"] is True
    conn.close()


def test_window_backfill_missing_d1_keeps_state_null_and_pending(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "688300",
        [_raw("20230103", 130.0, preclose=120.0, pct=8.333333)],
        [{"ex_date": "20230103", "factor_cum": 3.0}],
        window_start="20230103",
    )

    assert snapshot["state"]["baseline_factor"] is None
    assert snapshot["state"]["factor_latest"] is None
    assert snapshot["state"]["rebuild_pending"] is True
    assert snapshot["coverage"] == "partial"
    assert snapshot["hfq_rows"] == []
    assert snapshot["factor_audit"][0]["status"] == "unverified"
    conn.close()


def test_window_end_excludes_later_events_and_rows(pg_test):
    conn = _conn(pg_test)
    snapshot = rebuild_stock(
        conn,
        "600519",
        [
            _raw("20230103", 100.0, preclose=100.0, pct=0.0),
            _raw("20230104", 80.0, preclose=80.0, pct=0.0),
            _raw("20230105", 90.0, preclose=72.0, pct=25.0),
        ],
        [
            {"ex_date": "20230104", "factor_cum": 3.0},
            {"ex_date": "20230105", "factor_cum": 4.0},
        ],
        window_start="20230103",
        window_end="20230104",
    )

    assert [item["ex_date"] for item in snapshot["factor_audit"]] == ["20230104"]
    assert [row["trade_date"] for row in snapshot["hfq_rows"]] == [
        "20230103", "20230104",
    ]
    assert snapshot["state"]["factor_latest"] == pytest.approx(1.25)
    assert snapshot["state"]["latest_trade_date"] == "20230104"
    conn.close()


def test_window_end_rejects_post_window_facts_without_truncating(pg_test):
    """#163 评审：历史窗口不得删除/回退窗口外 hfq；检测到窗口外事实即拒绝。"""
    conn = _conn(pg_test)
    rebuild_stock(
        conn,
        "600519",
        [
            _raw("20230103", 100.0, preclose=100.0, pct=0.0),
            _raw("20230104", 80.0, preclose=80.0, pct=0.0),
            _raw("20230105", 90.0, preclose=72.0, pct=25.0),
            _raw("20230106", 90.0, preclose=90.0, pct=0.0),
        ],
        [{"ex_date": "20230104", "factor_cum": 2.0}],
        baseline_factor=1.0,
    )
    before = _business_tables(conn, "600519")

    with pytest.raises(ValueError, match="window_end"):
        rebuild_stock(
            conn, "600519", [], [],
            window_start="20230103", window_end="20230104",
        )

    assert _business_tables(conn, "600519") == before
    conn.close()
