"""复权回填（issue #163）：逐日事件集 + 全市场 raw/派生/续跑。

测试打 `adjustment_backfill.run_adjustment_backfill` 核心层（CLI 专用、不经
MCP），注入 fake baostock 适配器 + 真实 PG 测试库；覆盖成功空日、失败重试、
窗口基线、D-1、双路径对账、待修复台账、断点续跑、权威修订/撤销与导出产物。
"""

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

from qstock_mcp.adjustment_backfill import run_adjustment_backfill
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema

_TZ8 = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 20, 8, 30, tzinfo=_TZ8)  # 窗口已收盘后的次晨
START, END = "20261012", "20261016"
WINDOW_DAYS = ["20261012", "20261013", "20261014", "20261015", "20261016"]
D1 = "20261009"


def _bar(trade_date, close, preclose, pct, *, tradestatus=1, amount=100.0):
    return {
        "trade_date": trade_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "preclose": preclose,
        "volume": 1000 if tradestatus else 0,
        "amount": amount,
        "amplitude": None,
        "change_percent": pct,
        "change_amount": None,
        "turnover_rate": 1.0,
        "tradestatus": tradestatus,
    }


def _stock_a_rows():
    """600001：10-13 首次事件（B=2）、10-15 第二次事件（B=4），B0=1。"""
    return [
        _bar("20261012", 10.0, 10.0, 0.0),
        _bar("20261013", 8.0, 5.0, 60.0),
        _bar("20261014", 9.0, 8.0, 12.5),
        _bar("20261015", 9.0, 4.5, 100.0),
        _bar("20261016", 9.0, 9.0, 0.0),
    ]


def _stock_b_rows():
    """600002：10-14 首次事件（B=3），B0=1.5。"""
    return [
        _bar("20261012", 10.0, 10.0, 0.0),
        _bar("20261013", 10.0, 10.0, 0.0),
        _bar("20261014", 8.0, 5.0, 60.0),
        _bar("20261015", 8.0, 8.0, 0.0),
        _bar("20261016", 8.0, 8.0, 0.0),
    ]


def _stock_c_rows():
    """600003：窗口首日 10-12 即事件（B=2）；D-1=10-09 仅作基线输入。"""
    return [
        _bar(D1, 10.0, 10.0, 0.0),
        _bar("20261012", 8.0, 5.0, 60.0),
        _bar("20261013", 8.0, 8.0, 0.0),
        _bar("20261014", 8.0, 8.0, 0.0),
        _bar("20261015", 8.0, 8.0, 0.0),
        _bar("20261016", 8.0, 8.0, 0.0),
    ]


def _events():
    return {
        "20261012": [{"stock_code": "600003", "factor_cum": 2.0}],
        "20261013": [{"stock_code": "600001", "factor_cum": 2.0}],
        "20261014": [{"stock_code": "600002", "factor_cum": 3.0}],
        "20261015": [{"stock_code": "600001", "factor_cum": 4.0}],
        "20261016": [],
    }


def _raw_series():
    return {
        "600001": _stock_a_rows(),
        "600002": _stock_b_rows(),
        "600003": _stock_c_rows(),
    }


class FakeBackfillAdapter:
    """baostock 两接口 fake：按日事件、按日全市场 raw，可注入失败次数。"""

    name = "baostock"

    def __init__(self, *, events=None, raw=None, factor_failures=None,
                 raw_failures=None):
        self.events = events if events is not None else _events()
        self.raw = raw if raw is not None else _raw_series()
        self.factor_failures = dict(factor_failures or {})
        self.raw_failures = dict(raw_failures or {})
        self.factor_calls: list[str] = []
        self.raw_calls: list[dict] = []
        self.raw_day_calls: list[str] = []

    def fetch_daily_adjust_factor(self, trade_date):
        self.factor_calls.append(trade_date)
        remaining = self.factor_failures.get(trade_date, 0)
        if remaining > 0:
            self.factor_failures[trade_date] = remaining - 1
            raise FetchError(f"factor {trade_date} unavailable")
        return [dict(event) for event in self.events.get(trade_date, [])]

    def fetch_astock_daily(self, trade_date):
        self.raw_day_calls.append(trade_date)
        remaining = self.raw_failures.get(trade_date, 0)
        if remaining > 0:
            self.raw_failures[trade_date] = remaining - 1
            raise FetchError(f"raw {trade_date} unavailable")
        return [
            {**row, "stock_code": stock_code, "adj": "none"}
            for stock_code, rows in self.raw.items() for row in rows
            if row["trade_date"] == trade_date
        ]

    def fetch_daily(self, stock_code, start, end, adj="qfq"):
        self.raw_calls.append(
            {"stock_code": stock_code, "start": start, "end": end, "adj": adj}
        )
        remaining = self.raw_failures.get(stock_code, 0)
        if remaining > 0:
            self.raw_failures[stock_code] = remaining - 1
            raise FetchError(f"raw {stock_code} unavailable")
        return [
            dict(row) for row in self.raw.get(stock_code, [])
            if start <= row["trade_date"] <= end
        ]


def _conn(dsn):
    conn = psycopg2.connect(dsn)
    ensure_schema(conn)
    return conn


def _run(dsn, adapter, *, start=START, end=END, **kwargs):
    kwargs.setdefault("throttle", 0)
    kwargs.setdefault("now", NOW)
    return run_adjustment_backfill(
        start=start, end=end, adapter=adapter, **kwargs
    )


def _fetch(dsn, sql, args=()):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def _ledger(dsn, *, section=None, error_kind=None):
    sql = (
        "SELECT trade_date, status, source, rows, error_kind, detail"
        " FROM collection_log WHERE 1=1"
    )
    args = []
    if section:
        sql += " AND section=%s"
        args.append(section)
    if error_kind:
        sql += " AND error_kind=%s"
        args.append(error_kind)
    return _fetch(dsn, sql + " ORDER BY id", tuple(args))


def test_happy_path_ingests_events_fetches_market_raw_by_day_and_exports(pg_test, tmp_path):
    adapter = FakeBackfillAdapter()
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert _fetch(pg_test, "SELECT count(*) FROM stock_daily_raw")[0][0] == 16
    assert out["status"] == "ok", out["summary"]
    assert out["stage"] == "verified"
    assert out["messages"] == ["数据处理完成", "数据可以使用"]
    assert out["summary"]["days"] == {"ok": 4, "empty": 1, "error": 0, "skipped": 0}
    assert out["summary"]["event_set_complete"] is True
    assert out["summary"]["events"] == 4
    assert out["summary"]["event_stocks"] == 3
    assert out["summary"]["upstream_requests"] == {
        "factor_attempts": 5, "raw_attempts": 6, "total": 11,
    }
    assert out["summary"]["raw"]["staging_d1_stocks"] == 1

    # 因子与 raw 都按日获取；额外 D-1 只用于窗口首日事件的基线输入
    assert adapter.factor_calls == WINDOW_DAYS
    assert adapter.raw_day_calls == [D1] + WINDOW_DAYS
    assert adapter.raw_calls == []

    # 事件表只写实际事件；非事件票无占位因子
    events = _fetch(
        pg_test,
        "SELECT stock_code, ex_date, factor_cum, source FROM stock_adjust_factor"
        " ORDER BY ex_date, stock_code",
    )
    assert [(row[0], row[1], float(row[2])) for row in events] == [
        ("600003", "20261012", 2.0),
        ("600001", "20261013", 2.0),
        ("600002", "20261014", 3.0),
        ("600001", "20261015", 4.0),
    ]
    assert all(row[3] == "baostock-adjust-factor" for row in events)

    # 600003：D-1 raw 留库作审计，但不进 hfq
    raw_c = _fetch(
        pg_test,
        "SELECT trade_date, close, amount FROM stock_daily_raw"
        " WHERE stock_code='600003' ORDER BY trade_date",
    )
    assert raw_c[0][0] == D1
    assert len(raw_c) == 6
    hfq_c = _fetch(
        pg_test,
        "SELECT trade_date, close, factor_cum FROM stock_daily_hfq"
        " WHERE stock_code='600003' ORDER BY trade_date",
    )
    assert [row[0] for row in hfq_c] == WINDOW_DAYS
    assert float(hfq_c[0][2]) == pytest.approx(2.0)
    assert float(hfq_c[0][1]) == pytest.approx(16.0)

    # raw 保留 turn/tradestatus/权威 pctChg，amount 为适配层换算后的万元口径
    raw_a = _fetch(
        pg_test,
        "SELECT trade_date, turnover_rate, tradestatus, change_percent, amount"
        " FROM stock_daily_raw WHERE stock_code='600001' ORDER BY trade_date",
    )
    assert raw_a[1] == ("20261013", 1.0, 1, 60.0, 100.0)

    # 600001：窗口基线 B0=1，F 与 hfq/qfq 按事件重建
    state = _fetch(
        pg_test,
        "SELECT baseline_factor, factor_latest, latest_trade_date, rebuild_pending"
        " FROM stock_factor_state WHERE stock_code='600001'",
    )[0]
    assert float(state[0]) == pytest.approx(1.0)
    assert float(state[1]) == pytest.approx(4.0)
    assert state[2] == END
    assert state[3] is False
    hfq_a = _fetch(
        pg_test,
        "SELECT trade_date, close, factor_cum FROM stock_daily_hfq"
        " WHERE stock_code='600001' ORDER BY trade_date",
    )
    assert [float(row[2]) for row in hfq_a] == pytest.approx([1.0, 2.0, 2.0, 4.0, 4.0])
    assert [float(row[1]) for row in hfq_a] == pytest.approx([10.0, 16.0, 18.0, 36.0, 36.0])

    # 台账：逐日事件 + D-1/窗口内逐日 raw + 无待修复
    event_ledger = _ledger(pg_test, section="stock_adjust_factor")
    assert len(event_ledger) == len(WINDOW_DAYS)
    assert sorted(row[1] for row in event_ledger) == ["empty", "ok", "ok", "ok", "ok"]
    raw_ledger = _ledger(pg_test, section="stock_daily_raw")
    assert len(raw_ledger) == 6
    assert all(row[1] == "ok" for row in raw_ledger)
    assert _ledger(pg_test, section="stock_factor_repair") == []

    # 导出产物：事件集、事件票、逐日状态、汇总
    for name in (
        "factor_days.csv", "raw_days.csv", "events.csv", "event_stocks.csv",
        "raw_results.csv", "repairs.csv", "summary.json",
    ):
        assert (tmp_path / name).exists(), name
    stocks = list(csv.DictReader((tmp_path / "event_stocks.csv").open(encoding="utf-8")))
    assert [row["stock_code"] for row in stocks] == ["600001", "600002", "600003"]
    with (tmp_path / "summary.json").open(encoding="utf-8") as handle:
        summary = json.load(handle)
    assert summary["status"] == "ok"
    assert summary["window"] == {"start": START, "end": END}


def test_no_event_stock_uses_market_raw_and_unit_baseline(pg_test, tmp_path):
    raw = _raw_series()
    raw["600004"] = [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]
    out = _run(pg_test, FakeBackfillAdapter(raw=raw), report_dir=str(tmp_path))

    assert out["status"] == "ok", out["summary"]
    state = _fetch(
        pg_test,
        "SELECT baseline_factor, factor_latest, latest_trade_date, rebuild_pending"
        " FROM stock_factor_state WHERE stock_code='600004'",
    )[0]
    assert float(state[0]) == pytest.approx(1.0)
    assert float(state[1]) == pytest.approx(1.0)
    assert state[2:] == (END, False)
    factors = _fetch(
        pg_test,
        "SELECT factor_cum FROM stock_daily_hfq WHERE stock_code='600004'"
        " ORDER BY trade_date",
    )
    assert [float(row[0]) for row in factors] == pytest.approx([1.0] * 5)
    assert out["summary"]["no_event_stock_processing"]["ok"] == 1
    results = list(csv.DictReader((tmp_path / "raw_results.csv").open(encoding="utf-8")))
    assert "600004" in [row["stock_code"] for row in results]


def test_legacy_none_rows_are_audited_without_merging_into_raw(pg_test, tmp_path):
    raw = {"600004": [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]}
    conn = psycopg2.connect(pg_test)
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO stock_daily (stock_code, trade_date, close, amount, adj, source)"
                " VALUES (%s, %s, %s, 100.0, 'none', %s)",
                [
                    ("600004", START, 7.0, "baostock"),
                    ("600005", START, 8.0, "baostock"),
                    ("600006", START, None, "baostock"),
                    ("600007", "20221230", 9.0, "tencent"),
                ],
            )
        conn.commit()
    finally:
        conn.close()

    out = _run(
        pg_test,
        FakeBackfillAdapter(events={}, raw=raw),
        report_dir=str(tmp_path),
    )

    audit = out["summary"]["legacy_none"]
    assert audit == {
        "rows": 4,
        "stocks": 4,
        "first_trade_date": "20221230",
        "last_trade_date": START,
        "covered_by_raw": 1,
        "invalid_close": 1,
        "raw_recheck_required": 2,
        "isolated_rows": 4,
        "merged_into_raw": 0,
        "by_source": {"baostock": 3, "tencent": 1},
    }
    assert _fetch(pg_test, "SELECT count(*) FROM stock_daily WHERE adj='none'")[0][0] == 4
    assert _fetch(pg_test, "SELECT count(*) FROM stock_daily_raw")[0][0] == 5
    with (tmp_path / "legacy_none_audit.json").open(encoding="utf-8") as handle:
        assert json.load(handle) == audit


def test_failed_raw_day_at_window_edge_marks_no_event_stock_pending(pg_test, tmp_path):
    raw = _raw_series()
    raw["600004"] = [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]
    first = FakeBackfillAdapter(raw=raw, raw_failures={START: 99})
    out = _run(pg_test, first, report_dir=str(tmp_path / "first"))

    assert out["status"] == "partial_error"
    state = _fetch(
        pg_test,
        "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='600004'",
    )[0]
    assert state[0] is True

    resumed = _run(
        pg_test, FakeBackfillAdapter(raw=raw), report_dir=str(tmp_path / "resumed")
    )
    assert resumed["status"] == "ok", resumed["summary"]
    state = _fetch(
        pg_test,
        "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='600004'",
    )[0]
    assert state[0] is False


def _seed_event(dsn, stock_code, ex_date, factor_cum):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO stock_adjust_factor (stock_code, ex_date, factor_cum, source)"
                " VALUES (%s, %s, %s, 'seed')",
                (stock_code, ex_date, factor_cum),
            )
        conn.commit()
    finally:
        conn.close()


def test_factor_day_failure_retries_then_succeeds(pg_test, tmp_path):
    adapter = FakeBackfillAdapter(factor_failures={"20261014": 2})
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "ok"
    assert adapter.factor_calls.count("20261014") == 3  # 前两次失败，第三次成功
    assert out["summary"]["days"]["ok"] == 4
    day_rows = _ledger(pg_test, section="stock_adjust_factor")
    day_14 = [row for row in day_rows if row[0] == "20261014"]
    assert len(day_14) == 1
    assert day_14[0][1] == "ok"
    assert "attempts=3" in day_14[0][5]


def test_factor_day_failure_is_error_not_empty_and_keeps_old_events(pg_test, tmp_path):
    _seed_event(pg_test, "600002", "20261014", 3.0)
    adapter = FakeBackfillAdapter(factor_failures={"20261014": 99})
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "partial_error"
    assert out["summary"]["event_set_complete"] is False
    assert out["summary"]["days"]["error"] == 1
    assert [day["trade_date"] for day in out["summary"]["failed_days"]] == ["20261014"]

    day_14 = [row for row in _ledger(pg_test, section="stock_adjust_factor")
              if row[0] == "20261014"]
    assert len(day_14) == 1
    assert day_14[0][1] == "error"
    assert day_14[0][4] == "fetch_failed"

    # 失败不删旧事件，也不写空事件
    kept = _fetch(
        pg_test,
        "SELECT stock_code, factor_cum FROM stock_adjust_factor WHERE ex_date='20261014'",
    )
    assert [(row[0], float(row[1])) for row in kept] == [("600002", 3.0)]
    assert out["messages"] == ["数据处理完成"]


def test_incomplete_event_set_leaves_no_event_candidates_unclassified(pg_test, tmp_path):
    raw = {"600004": [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]}
    adapter = FakeBackfillAdapter(
        events={}, raw=raw, factor_failures={"20261014": 99},
    )
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "error"
    assert out["summary"]["raw"]["rows"] == 5
    assert out["summary"]["classification"] == {
        "event_set_complete": False,
        "factor_days_expected": 5,
        "factor_days_succeeded": 4,
        "factor_days_failed": 1,
        "event_stocks": 0,
        "no_event_stocks": 0,
        "retracted_stocks": 0,
        "unclassified_stocks": 1,
        "skip_reasons": {"incomplete_event_set": 1},
    }
    assert _fetch(
        pg_test, "SELECT count(*) FROM stock_factor_state WHERE stock_code='600004'"
    )[0][0] == 0
    rows = list(csv.DictReader((tmp_path / "stock_classification.csv").open(encoding="utf-8")))
    assert rows == [{
        "stock_code": "600004",
        "classification": "unclassified",
        "reason": "incomplete_event_set",
    }]


def test_resume_skips_completed_factor_and_raw_days(pg_test, tmp_path):
    failed_day = "20261014"
    first = FakeBackfillAdapter(raw_failures={failed_day: 99})
    out_first = _run(pg_test, first, report_dir=str(tmp_path / "first"))
    assert out_first["status"] == "partial_error"
    assert out_first["summary"]["raw_day_processing"]["error"] == 1
    assert first.raw_day_calls.count(failed_day) == 3
    assert first.raw_calls == []
    repairs = _ledger(pg_test, section="stock_factor_repair")
    assert any(row[4] == "raw_gap" for row in repairs)

    # 已完成因子日/raw 日零请求，只补失败日
    second = FakeBackfillAdapter()
    out_second = _run(pg_test, second, report_dir=str(tmp_path / "second"))
    assert out_second["status"] == "ok", out_second["summary"]
    assert second.factor_calls == []
    assert second.raw_day_calls == [failed_day]
    assert second.raw_calls == []
    assert out_second["summary"]["upstream_requests"]["total"] == 1

    state = _fetch(
        pg_test,
        "SELECT baseline_factor, factor_latest, rebuild_pending"
        " FROM stock_factor_state WHERE stock_code='600002'",
    )[0]
    assert float(state[0]) == pytest.approx(1.5)
    assert float(state[1]) == pytest.approx(2.0)
    assert state[2] is False


def test_set_audit_hashes_and_raw_sample_are_deterministic_on_resume(pg_test, tmp_path):
    raw = _raw_series()
    raw["600004"] = [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]
    first = _run(
        pg_test, FakeBackfillAdapter(raw=raw), report_dir=str(tmp_path / "first")
    )

    expected_validation = {
        "event_set_complete": True,
        "event_rows": 4,
        "event_stocks": 3,
        "no_event_stocks": 1,
        "raw_rows": 20,
        "raw_stocks": 4,
        "quarantined_rows": 0,
        "hfq_rows": 20,
        "state_rows": 4,
        "pending_states": 0,
        "hfq_without_valid_raw": 0,
        "valid_raw_without_hfq": 0,
        "state_without_raw": 0,
        "classification_mismatches": 0,
    }
    assert first["summary"]["validation"] == expected_validation
    hashes = list(csv.DictReader((tmp_path / "first" / "stock_hashes.csv").open()))
    assert len(hashes) == 4
    by_code = {row["stock_code"]: row for row in hashes}
    assert by_code["600004"]["raw_rows"] == "5"
    assert by_code["600004"]["event_rows"] == "0"
    assert by_code["600004"]["hfq_rows"] == "5"
    assert by_code["600004"]["state_rows"] == "1"
    assert all(len(row["raw_hash"]) == 32 for row in hashes)
    sample = list(csv.DictReader((tmp_path / "first" / "raw_sample.csv").open()))
    assert len(sample) == 20
    assert {row["classification"] for row in sample} == {"event", "no_event"}
    assert {row["price_bucket"] for row in sample} == {"lt10", "10_100"}
    assert all(
        sum(row["stock_code"] == code for row in sample) == 5 for code in by_code
    )
    assert first["summary"]["ordinary_raw_sample"] == {
        "seed": "164", "strategy": "100_per_class_5_per_stock_stratified",
        "rows": 20, "stocks": 4, "event_stocks": 3, "no_event_stocks": 1,
    }

    second = _run(
        pg_test, FakeBackfillAdapter(raw=raw), report_dir=str(tmp_path / "second")
    )
    assert second["summary"]["upstream_requests"]["total"] == 0
    assert second["summary"]["validation"] == expected_validation
    assert second["summary"]["skip_reasons"] == {
        "factor_day_checkpoint": 5, "raw_day_checkpoint": 6,
    }
    assert (tmp_path / "second" / "stock_hashes.csv").read_bytes() == (
        tmp_path / "first" / "stock_hashes.csv"
    ).read_bytes()
    assert (tmp_path / "second" / "raw_sample.csv").read_bytes() == (
        tmp_path / "first" / "raw_sample.csv"
    ).read_bytes()


def test_raw_sample_stock_cap_preserves_exchange_strata(pg_test, tmp_path):
    rows = [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]
    raw = {
        f"{code:06d}": [dict(row) for row in rows]
        for code in range(600000, 600101)
    }
    raw["009517"] = rows

    out = _run(
        pg_test, FakeBackfillAdapter(events={}, raw=raw), report_dir=str(tmp_path)
    )

    assert out["status"] == "ok", out["summary"]
    assert out["summary"]["ordinary_raw_sample"]["stocks"] == 100
    sample = list(csv.DictReader((tmp_path / "raw_sample.csv").open()))
    assert {row["exchange"] for row in sample} == {"sh", "sz"}
    assert any(row["stock_code"] == "009517" for row in sample)


def test_state_only_row_is_in_hash_and_blocks_verified(pg_test, tmp_path):
    conn = psycopg2.connect(pg_test)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO stock_factor_state (stock_code, baseline_factor,"
                " factor_latest, latest_trade_date, rebuild_pending)"
                " VALUES ('600099', 1, 1, '20261016', false)"
            )
        conn.commit()
    finally:
        conn.close()

    raw = {"600004": [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]}
    out = _run(
        pg_test, FakeBackfillAdapter(events={}, raw=raw), report_dir=str(tmp_path)
    )

    assert out["status"] == "partial_error"
    assert out["stage"] == "rebuilt"
    assert out["summary"]["validation"]["state_rows"] == 2
    assert out["summary"]["validation"]["state_without_raw"] == 1
    hashes = list(csv.DictReader((tmp_path / "stock_hashes.csv").open()))
    assert len(hashes) == 2
    by_code = {row["stock_code"]: row for row in hashes}
    assert by_code["600099"]["state_rows"] == "1"
    assert by_code["600099"]["state_hash"]


def _events_for(stock_code, ex_date, factor_cum):
    events = {day: [] for day in WINDOW_DAYS}
    events[ex_date] = [{"stock_code": stock_code, "factor_cum": factor_cum}]
    return events


def test_new_listing_without_d1_is_pending_not_provisional(pg_test, tmp_path):
    events = _events_for("600005", "20261014", 3.0)
    raw = {"600005": [
        _bar("20261014", 8.0, 5.0, 60.0),
        _bar("20261015", 8.0, 8.0, 0.0),
        _bar("20261016", 8.0, 8.0, 0.0),
    ]}
    raw["600004"] = [_bar(day, 7.0, 7.0, 0.0) for day in [D1] + WINDOW_DAYS]
    adapter = FakeBackfillAdapter(events=events, raw=raw)
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "partial_error"
    assert out["messages"] == ["数据处理完成"]
    state = _fetch(
        pg_test,
        "SELECT baseline_factor, factor_latest, rebuild_pending"
        " FROM stock_factor_state WHERE stock_code='600005'",
    )[0]
    assert state[0] is None and state[1] is None and state[2] is True
    assert _fetch(
        pg_test, "SELECT count(*) FROM stock_daily_hfq WHERE stock_code='600005'"
    )[0][0] == 0
    repairs = _ledger(pg_test, section="stock_factor_repair")
    assert [row[4] for row in repairs] == ["baseline_unverified"]


def test_dual_path_mismatch_marks_pending_and_keeps_baseline(pg_test, tmp_path):
    raw = {"600001": _stock_a_rows()}
    raw["600001"][3] = _bar("20261015", 9.0, 9.0, 0.0)  # 价格路径 f=1，B 路径 f=0.5
    adapter = FakeBackfillAdapter(events=_events(), raw=raw)
    # 只保留 600001 事件，避免其他票干扰断言
    adapter.events = {
        "20261012": [], "20261013": [{"stock_code": "600001", "factor_cum": 2.0}],
        "20261014": [], "20261015": [{"stock_code": "600001", "factor_cum": 4.0}],
        "20261016": [],
    }
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "partial_error"
    state = _fetch(
        pg_test,
        "SELECT baseline_factor, factor_latest, rebuild_pending"
        " FROM stock_factor_state WHERE stock_code='600001'",
    )[0]
    assert float(state[0]) == pytest.approx(1.0)
    assert float(state[1]) == pytest.approx(4.0)
    assert state[2] is True
    repairs = _ledger(pg_test, section="stock_factor_repair")
    assert [row[4] for row in repairs] == ["factor_mismatch"]
    assert "relative_error" in repairs[0][5]


def test_raw_gap_is_pending_and_recorded(pg_test, tmp_path):
    raw = {"600001": [
        _bar("20261012", 10.0, 10.0, 0.0),
        _bar("20261013", 8.0, 5.0, 60.0),
        _bar("20261015", 9.0, 4.5, 100.0),
        _bar("20261016", 9.0, 9.0, 0.0),
    ]}
    adapter = FakeBackfillAdapter(
        events={
            "20261012": [], "20261013": [{"stock_code": "600001", "factor_cum": 2.0}],
            "20261014": [], "20261015": [{"stock_code": "600001", "factor_cum": 4.0}],
            "20261016": [],
        },
        raw=raw,
    )
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "partial_error"
    repairs = _ledger(pg_test, section="stock_factor_repair")
    reasons = [row[4] for row in repairs]
    assert "raw_gap" in reasons
    assert "price_path_unverified" in reasons
    raw_gap = next(row for row in repairs if row[4] == "raw_gap")
    assert "20261014" in raw_gap[5]
    state = _fetch(
        pg_test,
        "SELECT baseline_factor, rebuild_pending FROM stock_factor_state"
        " WHERE stock_code='600001'",
    )[0]
    assert float(state[0]) == pytest.approx(1.0)
    assert state[1] is True


def test_invalid_close_kept_in_raw_quarantined_and_repaired(pg_test, tmp_path):
    raw = {"600001": _stock_a_rows()}
    raw["600001"][2] = _bar("20261014", 0.0, 8.0, 0.0)
    adapter = FakeBackfillAdapter(
        events={
            "20261012": [], "20261013": [{"stock_code": "600001", "factor_cum": 2.0}],
            "20261014": [], "20261015": [],
            "20261016": [],
        },
        raw=raw,
    )
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "partial_error"
    assert _fetch(
        pg_test,
        "SELECT close FROM stock_daily_raw"
        " WHERE stock_code='600001' AND trade_date='20261014'",
    )[0][0] == 0.0
    assert _fetch(
        pg_test,
        "SELECT count(*) FROM stock_daily_hfq"
        " WHERE stock_code='600001' AND trade_date='20261014'",
    )[0][0] == 0
    repairs = _ledger(pg_test, section="stock_factor_repair")
    assert "invalid_close" in [row[4] for row in repairs]
    quarantine = [row for row in _ledger(pg_test, section="stock_daily_raw")
                  if row[4] == "invalid_close"]
    assert len(quarantine) == 1
    assert "600001:20261014" in quarantine[0][5]
    results = list(csv.DictReader((tmp_path / "raw_results.csv").open(encoding="utf-8")))
    assert results[0]["pending"] == "True"
    assert out["summary"]["event_stock_processing"]["pending"] == 1


def test_no_event_invalid_closes_are_quarantined_and_exported(pg_test, tmp_path):
    rows = [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]
    rows[1] = _bar("20261013", 0.0, 7.0, 0.0)
    rows[2] = _bar("20261014", None, 7.0, 0.0)
    out = _run(
        pg_test,
        FakeBackfillAdapter(events={}, raw={"600004": rows}),
        report_dir=str(tmp_path),
    )

    assert out["status"] == "partial_error"
    assert out["summary"]["isolation"] == {
        "rows": 2, "stocks": 1, "reasons": {"invalid_close": 2},
    }
    assert _fetch(
        pg_test,
        "SELECT trade_date FROM stock_daily_raw WHERE stock_code='600004'"
        " ORDER BY trade_date",
    ) == [(day,) for day in WINDOW_DAYS]
    assert _fetch(
        pg_test,
        "SELECT trade_date FROM stock_daily_hfq WHERE stock_code='600004'"
        " ORDER BY trade_date",
    ) == [("20261012",), ("20261015",), ("20261016",)]
    state = _fetch(
        pg_test,
        "SELECT baseline_factor, factor_latest, latest_trade_date, rebuild_pending"
        " FROM stock_factor_state WHERE stock_code='600004'",
    )[0]
    assert float(state[0]) == 1.0
    assert float(state[1]) == 1.0
    assert state[2:] == (END, True)
    quarantined = list(csv.DictReader((tmp_path / "quarantined.csv").open(encoding="utf-8")))
    assert quarantined == [
        {"stock_code": "600004", "trade_date": "20261013", "reason": "invalid_close"},
        {"stock_code": "600004", "trade_date": "20261014", "reason": "invalid_close"},
    ]


def test_authoritative_revision_rebuilds_affected_window(pg_test, tmp_path):
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(tmp_path / "first"))

    revised_events = _events()
    revised_events["20261015"] = [{"stock_code": "600001", "factor_cum": 5.0}]
    revised_raw = _raw_series()
    revised_raw["600001"][3] = _bar("20261015", 9.0, 3.6, 150.0)
    adapter = FakeBackfillAdapter(events=revised_events, raw=revised_raw)
    out = _run(pg_test, adapter, force=True, report_dir=str(tmp_path / "second"))

    assert out["status"] == "ok"
    assert adapter.factor_calls == WINDOW_DAYS
    factor = _fetch(
        pg_test,
        "SELECT factor_cum FROM stock_adjust_factor"
        " WHERE stock_code='600001' AND ex_date='20261015'",
    )[0][0]
    assert float(factor) == pytest.approx(5.0)
    state = _fetch(
        pg_test,
        "SELECT factor_latest FROM stock_factor_state WHERE stock_code='600001'",
    )[0][0]
    assert float(state) == pytest.approx(5.0)
    hfq = _fetch(
        pg_test,
        "SELECT trade_date, factor_cum FROM stock_daily_hfq"
        " WHERE stock_code='600001' ORDER BY trade_date",
    )
    assert [float(row[1]) for row in hfq] == pytest.approx([1.0, 2.0, 2.0, 5.0, 5.0])


def test_authoritative_event_reclassifies_previously_no_event_stock(pg_test, tmp_path):
    raw = {"600004": [
        _bar("20261012", 10.0, 10.0, 0.0),
        _bar("20261013", 10.0, 10.0, 0.0),
        _bar("20261014", 8.0, 5.0, 60.0),
        _bar("20261015", 8.0, 8.0, 0.0),
        _bar("20261016", 8.0, 8.0, 0.0),
    ]}
    first = _run(
        pg_test, FakeBackfillAdapter(events={}, raw=raw),
        report_dir=str(tmp_path / "first"),
    )
    assert first["summary"]["classification"]["no_event_stocks"] == 1

    events = _events_for("600004", "20261014", 2.0)
    second = _run(
        pg_test, FakeBackfillAdapter(events=events, raw=raw), force=True,
        report_dir=str(tmp_path / "second"),
    )

    assert second["status"] == "ok", second["summary"]
    assert second["summary"]["classification"]["event_stocks"] == 1
    assert second["summary"]["classification"]["no_event_stocks"] == 0
    state = _fetch(
        pg_test,
        "SELECT baseline_factor, factor_latest, rebuild_pending"
        " FROM stock_factor_state WHERE stock_code='600004'",
    )[0]
    assert float(state[0]) == 1.0
    assert float(state[1]) == 2.0
    assert state[2] is False
    factors = _fetch(
        pg_test,
        "SELECT factor_cum FROM stock_daily_hfq WHERE stock_code='600004'"
        " ORDER BY trade_date",
    )
    assert [float(row[0]) for row in factors] == [1.0, 1.0, 2.0, 2.0, 2.0]


def test_authoritative_retraction_deletes_stale_event_and_rebuilds_as_no_event(pg_test, tmp_path):
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(tmp_path / "first"))

    retracted_events = _events()
    retracted_events["20261014"] = []
    adapter = FakeBackfillAdapter(events=retracted_events)
    out = _run(pg_test, adapter, force=True, report_dir=str(tmp_path / "second"))

    assert out["status"] == "ok"
    assert out["summary"]["retracted_rebuilt"] == 1
    assert _fetch(
        pg_test,
        "SELECT count(*) FROM stock_adjust_factor WHERE stock_code='600002'",
    )[0][0] == 0
    state = _fetch(
        pg_test,
        "SELECT baseline_factor, factor_latest, rebuild_pending"
        " FROM stock_factor_state WHERE stock_code='600002'",
    )[0]
    assert float(state[0]) == pytest.approx(1.0)
    assert float(state[1]) == pytest.approx(1.0)
    assert state[2] is False
    factors = _fetch(
        pg_test,
        "SELECT factor_cum FROM stock_daily_hfq"
        " WHERE stock_code='600002' ORDER BY trade_date",
    )
    assert [float(row[0]) for row in factors] == pytest.approx([1.0] * 5)
    # raw 事实保留，撤回的是事件/派生
    assert _fetch(
        pg_test,
        "SELECT count(*) FROM stock_daily_raw WHERE stock_code='600002'",
    )[0][0] == 5
    stocks = list(csv.DictReader(
        (tmp_path / "second" / "event_stocks.csv").open(encoding="utf-8")
    ))
    assert [row["stock_code"] for row in stocks] == ["600001", "600003"]


def test_limit_marks_limited_and_does_not_claim_complete(pg_test, tmp_path):
    adapter = FakeBackfillAdapter()
    out = _run(pg_test, adapter, limit=1, report_dir=str(tmp_path))

    assert out["status"] == "partial_error"
    assert out["messages"] == ["数据处理完成"]
    assert out["summary"]["event_stock_processing"]["limited"] is True
    assert out["summary"]["event_stock_processing"]["selected"] == 1
    assert adapter.raw_calls == []
    assert adapter.raw_day_calls == [D1] + WINDOW_DAYS  # limit 只限本地重建
    stocks = list(csv.DictReader((tmp_path / "event_stocks.csv").open(encoding="utf-8")))
    assert len(stocks) == 3  # 导出仍是完整事件票清单


def test_factor_upstream_unavailable_is_error_without_fallback(pg_test, tmp_path):
    adapter = FakeBackfillAdapter(
        factor_failures={day: 99 for day in WINDOW_DAYS}
    )
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "error"
    assert out["stage"] == "ingested"
    assert out["messages"] == ["数据处理完成"]
    assert adapter.raw_calls == []  # 不自动换腾讯/新浪回填
    assert out["summary"]["events"] == 0
    assert len(out["summary"]["failed_days"]) == 5
    assert all(row[1] == "error" for row in _ledger(pg_test, section="stock_adjust_factor"))


def test_cli_contract(pg_test, tmp_path, capsys, monkeypatch):
    from qstock_mcp import scheduler

    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "stage": "verified", "messages": ["数据可以使用"]}

    monkeypatch.setattr(scheduler, "run_adjustment_backfill", fake)
    rc = scheduler.main([
        "adj-backfill", "--start", "20261012", "--end", "20261016",
        "--limit", "5", "--report-dir", str(tmp_path), "--throttle", "0",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["job"] == "adj-backfill"
    assert out["status"] == "ok"
    assert calls[0]["start"] == "20261012"
    assert calls[0]["end"] == "20261016"
    assert calls[0]["limit"] == 5
    assert calls[0]["throttle"] == 0

    monkeypatch.setattr(
        scheduler, "run_adjustment_backfill",
        lambda **kwargs: {"status": "partial_error"},
    )
    rc = scheduler.main(["adj-backfill", "--start", "20261012", "--end", "20261016"])
    assert rc == 1
    capsys.readouterr()

    monkeypatch.setattr(
        scheduler, "run_adjustment_backfill",
        lambda **kwargs: pytest.fail("互斥参数不应触达回填"),
    )
    rc = scheduler.main([
        "adj-backfill", "--start", "20261012", "--trading-days", "3",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "--trading-days 与 --start 互斥" in out["error"]


def test_cli_trading_days_computes_window(pg_test, capsys, monkeypatch):
    from qstock_mcp import scheduler

    calls = []
    monkeypatch.setattr(
        scheduler, "run_adjustment_backfill",
        lambda **kwargs: calls.append(kwargs) or {"status": "ok"},
    )
    rc = scheduler.main(["adj-backfill", "--trading-days", "3", "--end", "20261016"])
    assert rc == 0
    assert calls[0]["start"] == "20261014"
    assert calls[0]["end"] == "20261016"


def test_resume_skips_do_not_consume_request_throttle(pg_test, tmp_path, monkeypatch):
    """断点命中不应为“跳过”票付节流等待（回归：旧实现每票都 sleep）。"""
    sleeps = []
    monkeypatch.setattr(
        "qstock_mcp.adjustment_backfill.sleep", lambda seconds: sleeps.append(seconds)
    )
    first = _run(
        pg_test, FakeBackfillAdapter(), throttle=1.5,
        report_dir=str(tmp_path / "first"),
    )
    assert first["status"] == "ok"
    assert len(sleeps) > 0  # 首次真实请求之间按 1.5s 节流

    sleeps.clear()
    second = _run(
        pg_test, FakeBackfillAdapter(), throttle=1.5,
        report_dir=str(tmp_path / "second"),
    )
    assert second["summary"]["upstream_requests"]["total"] == 0
    assert sleeps == []


class CrashingRawAdapter(FakeBackfillAdapter):
    def fetch_astock_daily(self, trade_date):
        raise RuntimeError("simulated crash during raw phase")


def test_retraction_marker_survives_interruption_before_raw_rebuild(pg_test, tmp_path):
    """事件整日替换后崩溃：续跑仍能按撤销标记把旧事件票重建为无事件基线。"""
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(tmp_path / "first"))

    retracted_events = _events()
    retracted_events["20261014"] = []
    with pytest.raises(RuntimeError, match="simulated crash"):
        _run(
            pg_test, CrashingRawAdapter(events=retracted_events), force=True,
            report_dir=str(tmp_path / "crashed"),
        )
    markers = _ledger(pg_test, section="stock_factor_retracted")
    assert any("600002" in row[5] for row in markers)
    pending = _fetch(
        pg_test,
        "SELECT rebuild_pending FROM stock_factor_state WHERE stock_code='600002'",
    )[0][0]
    assert pending is True  # 整日替换同事务标记受影响 state

    out = _run(
        pg_test, FakeBackfillAdapter(events=retracted_events),
        report_dir=str(tmp_path / "resumed"),
    )
    assert out["status"] == "ok"
    assert out["summary"]["retracted_rebuilt"] == 1
    state = _fetch(
        pg_test,
        "SELECT baseline_factor, factor_latest, rebuild_pending"
        " FROM stock_factor_state WHERE stock_code='600002'",
    )[0]
    assert float(state[0]) == pytest.approx(1.0)
    assert float(state[1]) == pytest.approx(1.0)
    assert state[2] is False
    factors = _fetch(
        pg_test,
        "SELECT factor_cum FROM stock_daily_hfq"
        " WHERE stock_code='600002' ORDER BY trade_date",
    )
    assert [float(row[0]) for row in factors] == pytest.approx([1.0] * 5)


def test_day_checkpoint_ignores_morning_pctchg_derived_row(pg_test, tmp_path):
    """morning 的 pctChg 降级 ok 行不能冒充回填已调过权威逐日因子接口。"""
    _seed_event(pg_test, "600002", "20261014", 3.0)
    conn = psycopg2.connect(pg_test)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO collection_log (trade_date, section, status, source,"
                " rows, detail) VALUES ('20261014','stock_adjust_factor','ok',"
                " 'baostock', 1, 'morning_batch:baostock-pctchg-derived;stocks=1')"
            )
        conn.commit()
    finally:
        conn.close()

    adapter = FakeBackfillAdapter()
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "ok"
    assert adapter.factor_calls == WINDOW_DAYS
    day_14 = [row for row in _ledger(pg_test, section="stock_adjust_factor")
              if row[0] == "20261014"]
    assert "adj-backfill event" in day_14[-1][5]


def test_same_b_daily_row_is_not_stored_as_placeholder_event(pg_test, tmp_path):
    events = _events()
    events["20261015"] = [{"stock_code": "600001", "factor_cum": 2.0}]
    raw = _raw_series()
    raw["600001"][3] = _bar("20261015", 9.0, 9.0, 0.0)  # 无价格阶跃
    adapter = FakeBackfillAdapter(events=events, raw=raw)
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "ok"
    assert out["summary"]["events"] == 3  # 10-15 同 B 行不是事件
    stored = _fetch(
        pg_test,
        "SELECT ex_date, factor_cum FROM stock_adjust_factor"
        " WHERE stock_code='600001' ORDER BY ex_date",
    )
    assert [(row[0], float(row[1])) for row in stored] == [("20261013", 2.0)]
    day_15 = [row for row in _ledger(pg_test, section="stock_adjust_factor")
              if row[0] == "20261015"]
    assert day_15[-1][1] == "ok"
    assert day_15[-1][3] == 0
    assert "equal_b_skipped=1" in day_15[-1][5]
    state = _fetch(
        pg_test,
        "SELECT factor_latest FROM stock_factor_state WHERE stock_code='600001'",
    )[0][0]
    assert float(state) == pytest.approx(2.0)


def test_historical_end_with_later_facts_is_rejected(pg_test, tmp_path):
    conn = psycopg2.connect(pg_test)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO stock_daily_raw (stock_code, trade_date, close, source)"
                " VALUES ('600001','20261019',10.0,'seed')"
            )
        conn.commit()
    finally:
        conn.close()

    adapter = FakeBackfillAdapter()
    out = _run(pg_test, adapter, end="20261016", report_dir=str(tmp_path))

    assert out["status"] == "error"
    assert out["error_kind"] == "window_behind_data"
    assert adapter.factor_calls == []
    assert adapter.raw_calls == []
    assert _fetch(
        pg_test,
        "SELECT count(*) FROM stock_daily_raw WHERE trade_date='20261019'",
    )[0][0] == 1
    assert _fetch(pg_test, "SELECT count(*) FROM stock_adjust_factor")[0][0] == 0


def test_missing_first_event_raw_is_recorded_as_raw_gap(pg_test, tmp_path):
    raw = {"600001": [
        _bar("20261012", 10.0, 10.0, 0.0),
        _bar("20261014", 9.0, 8.0, 12.5),
        _bar("20261015", 9.0, 4.5, 100.0),
        _bar("20261016", 9.0, 9.0, 0.0),
    ]}
    adapter = FakeBackfillAdapter(
        events={
            "20261012": [], "20261013": [{"stock_code": "600001", "factor_cum": 2.0}],
            "20261014": [], "20261015": [{"stock_code": "600001", "factor_cum": 4.0}],
            "20261016": [],
        },
        raw=raw,
    )
    out = _run(pg_test, adapter, report_dir=str(tmp_path))

    assert out["status"] == "partial_error"
    repairs = _ledger(pg_test, section="stock_factor_repair")
    raw_gap = next(row for row in repairs if row[4] == "raw_gap")
    assert "first_event=20261013" in raw_gap[5]
    state = _fetch(
        pg_test,
        "SELECT baseline_factor, rebuild_pending FROM stock_factor_state"
        " WHERE stock_code='600001'",
    )[0]
    assert state[0] is None
    assert state[1] is True


def test_backfill_baostock_locked_returns_structured_error(pg_test, tmp_path):
    from qstock_mcp.baostock_errors import BaostockLocked

    class LockedAdapter(FakeBackfillAdapter):
        def fetch_daily_adjust_factor(self, trade_date):
            raise BaostockLocked("未持有有效的 baostock 租约")

    out = run_adjustment_backfill(
        start=START,
        end=END,
        adapter=LockedAdapter(),
        throttle=0,
        report_dir=str(tmp_path),
        now=NOW,
    )

    assert out["status"] == "error"
    assert out["error_kind"] == "baostock_locked"


def test_backfill_uses_shared_api_without_persistent_lifecycle(pg_test, tmp_path, monkeypatch):
    from qstock_mcp.adapters import baostock_adapter

    created = []

    class FakeDefault(FakeBackfillAdapter):
        def __init__(self):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(baostock_adapter, "BaostockAdapter", FakeDefault)
    out = run_adjustment_backfill(
        start=START, end=END, adapter=None, throttle=0,
        report_dir=str(tmp_path), now=NOW,
    )

    assert out["status"] == "ok"
    assert len(created) == 1
