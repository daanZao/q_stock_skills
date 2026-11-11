"""复权迁移八项硬验收（issue #165，spec #158 §7）。"""

import csv
import json

import psycopg2

from qstock_mcp.adjustment_acceptance import run_adjustment_acceptance
from test_adjustment_backfill import (
    END, WINDOW_DAYS, FakeBackfillAdapter, _bar, _raw_series, _run,
)


def test_acceptance_exposes_eight_checks_and_is_local_by_default(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    migration = _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    assert migration["status"] == "ok"

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    assert [check["id"] for check in out["checks"]] == [
        "factor_baseline",
        "event_invariants",
        "structure_coverage",
        "indicator_regression",
        "upstream_smoke",
        "event_reconciliation",
        "golden_set",
        "hash_idempotency",
    ]
    assert out["summary"]["upstream_requests"]["total"] == 0
    assert out["summary"]["duration_s"] >= 0
    assert out["summary"]["performance_budget_s"] == 600
    smoke = next(check for check in out["checks"] if check["id"] == "upstream_smoke")
    assert smoke["status"] == "not_run"
    assert out["status"] == "partial_error"


def test_golden_stocks_do_not_consume_ordinary_sample_quota(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    raw = _raw_series()
    for number in range(600100, 600199):
        raw[str(number)] = [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]
    raw["600519"] = [_bar(day, 8.0, 8.0, 0.0) for day in WINDOW_DAYS]
    migration = _run(
        pg_test, FakeBackfillAdapter(raw=raw), report_dir=str(migration_dir)
    )

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    assert migration["summary"]["ordinary_raw_sample"]["no_event_stocks"] == 99
    with (migration_dir / "raw_sample.csv").open(encoding="utf-8", newline="") as handle:
        sample = list(csv.DictReader(handle))
    assert "600519" not in {row["stock_code"] for row in sample}
    indicator = next(
        check for check in out["checks"] if check["id"] == "indicator_regression"
    )
    assert indicator["golden_sample_rows"] == 0

    contaminated = {**sample[0], "stock_code": "600519", "classification": "no_event"}
    with (migration_dir / "raw_sample.csv").open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=list(sample[0])).writerow(contaminated)
    rerun = run_adjustment_acceptance(migration_dir=str(migration_dir))
    indicator = next(
        check for check in rerun["checks"] if check["id"] == "indicator_regression"
    )
    assert indicator["golden_sample_rows"] == 1
    assert indicator["status"] == "error"
    assert any(row["field"] == "golden_sample_quota" for row in indicator["failures"])


def test_golden_set_has_20_boundaries_and_recorded_vectors_are_fixed(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "golden_set")
    assert check["cases"] == 20
    assert check["recorded_cases_checked"] == 3
    assert check["recorded_failures"] == []
    assert {"688300", "600381", "600094", "600519", "301569", "603786", "603336"} <= set(
        check["required_coverage"]
    )
    assert "suspended_reverse_event" in check["boundaries"]
    assert "delisted" in check["boundaries"]
    assert "bse_default_excluded" in check["boundaries"]
    assert check["status"] == "error"
    assert any(row["stock_code"] == "688300" for row in check["failures"])


def test_upstream_smoke_runs_three_serial_days_without_db_writes(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    before = _business_hash_snapshot(pg_test)
    adapter = FakeBackfillAdapter()

    out = run_adjustment_acceptance(
        migration_dir=str(migration_dir), adapter=adapter,
        smoke_days=3, smoke_end=END, throttle=0,
    )

    check = next(check for check in out["checks"] if check["id"] == "upstream_smoke")
    assert check["status"] == "ok", check
    assert check["days"] == ["20261014", "20261015", "20261016"]
    assert check["upstream_requests"] == {"factor": 3, "raw": 3, "total": 6}
    assert check["empty_factor_days"] == ["20261016"]
    assert check["unit_checks"] == ["amount_wan", "volume_shares"]
    assert check["timeout_policy"] == "adapter_60s"
    assert check["resume_evidence"] == "hash_idempotency"
    assert adapter.factor_calls == check["days"]
    assert adapter.raw_day_calls == check["days"]
    assert _business_hash_snapshot(pg_test) == before


def test_upstream_smoke_rejects_non_finite_amount(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    raw = _raw_series()
    raw["600001"][2]["amount"] = float("nan")
    _run(pg_test, FakeBackfillAdapter(raw=raw), report_dir=str(migration_dir))

    out = run_adjustment_acceptance(
        migration_dir=str(migration_dir), adapter=FakeBackfillAdapter(raw=raw),
        smoke_days=3, smoke_end=END, throttle=0,
    )

    check = next(check for check in out["checks"] if check["id"] == "upstream_smoke")
    assert check["status"] == "error"
    assert any(
        row["trade_date"] == "20261014" and row["field"] == "raw/date/adj/amount/volume"
        for row in check["failures"]
    )


def test_smoke_preserves_baostock_locked_error(pg_test, tmp_path):
    from qstock_mcp.baostock_errors import BaostockLocked

    class LockedAdapter(FakeBackfillAdapter):
        def fetch_daily_adjust_factor(self, trade_date):
            raise BaostockLocked("未持有有效的 baostock 租约")

    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))

    out = run_adjustment_acceptance(
        migration_dir=str(migration_dir), adapter=LockedAdapter(),
        smoke_days=3, smoke_end=END, throttle=0,
    )

    assert out["status"] == "error"
    assert out["error_kind"] == "baostock_locked"


def test_smoke_reports_upstream_failure_and_timeout(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    failed = run_adjustment_acceptance(
        migration_dir=str(migration_dir),
        adapter=FakeBackfillAdapter(raw_failures={"20261014": 1}),
        smoke_days=3, smoke_end=END, throttle=0,
    )
    failed_check = next(
        check for check in failed["checks"] if check["id"] == "upstream_smoke"
    )
    assert failed_check["status"] == "error"
    assert any(row["field"] == "raw" for row in failed_check["failures"])

    class TimeoutAdapter(FakeBackfillAdapter):
        def fetch_astock_daily(self, trade_date):
            raise TimeoutError("simulated upstream timeout")

    timed_out = run_adjustment_acceptance(
        migration_dir=str(migration_dir), adapter=TimeoutAdapter(),
        smoke_days=3, smoke_end=END, throttle=0,
    )
    timeout_check = next(
        check for check in timed_out["checks"] if check["id"] == "upstream_smoke"
    )
    assert timeout_check["status"] == "error"
    assert "timeout" in timeout_check["failures"][0]["error"]


def _business_hash_snapshot(dsn):
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 'raw', count(*) FROM stock_daily_raw UNION ALL"
            " SELECT 'event', count(*) FROM stock_adjust_factor UNION ALL"
            " SELECT 'hfq', count(*) FROM stock_daily_hfq UNION ALL"
            " SELECT 'state', count(*) FROM stock_factor_state ORDER BY 1"
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def test_indicator_regression_uses_independent_rolling_expectations(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "indicator_regression")
    assert check["status"] == "ok", check
    assert check["sample_stocks"] == 3
    assert check["sample_rows"] == 15
    assert check["window_comparisons"] == 60
    assert check["expanded_to"] == []
    assert check["legacy_parity_hard"] is False
    assert check["failures"] == []


def test_legacy_diagnostics_only_read_sampled_dates(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    raw = _raw_series()
    raw["600004"] = [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]
    _run(pg_test, FakeBackfillAdapter(raw=raw), report_dir=str(migration_dir))
    sample_path = migration_dir / "raw_sample.csv"
    with sample_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        sample = [
            row for row in reader
            if row["stock_code"] != "600004" or row["trade_date"] == "20261012"
        ]
    with sample_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sample)
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO stock_daily (stock_code, trade_date, close, amount, adj, source)"
            " VALUES ('600004', %s, 9999, 100, 'qfq', 'legacy')",
            [("20261012",), ("20261013",)],
        )
    conn.commit()
    conn.close()

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "indicator_regression")
    assert check["status"] == "error"
    assert [row["trade_date"] for row in check["legacy_diagnostics"]] == ["20261012"]
    assert {"qfq", "high_52w", "low_52w"} <= set(check["legacy_diagnostics"][0]["fields"])
    assert check["legacy_diagnostics"][0]["explained_by_event"] is False
    assert check["unexplained_significant_differences"] == 1
    assert check["expanded_to"] == [{
        "classification": "no_event", "stocks": 1, "rows": 5,
        "failure_count": 0, "legacy_unexplained": 2,
    }]


def test_full_universe_requires_100_plus_100_five_rows_and_fixed_seed(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    raw = {}
    events = {day: [] for day in WINDOW_DAYS}
    for number in range(600100, 600200):
        code = str(number)
        raw[code] = [
            _bar(day, 8.0 if day >= "20261013" else 10.0, 5.0 if day == "20261013" else 8.0, 60.0 if day == "20261013" else 0.0)
            for day in WINDOW_DAYS
        ]
        events["20261013"].append({"stock_code": code, "factor_cum": 2.0})
    for number in range(100, 200):
        raw[f"{number:06d}"] = [_bar(day, 7.0, 7.0, 0.0) for day in WINDOW_DAYS]
    migration = _run(
        pg_test, FakeBackfillAdapter(events=events, raw=raw), report_dir=str(migration_dir)
    )
    assert migration["summary"]["ordinary_raw_sample"]["event_stocks"] == 100
    assert migration["summary"]["ordinary_raw_sample"]["no_event_stocks"] == 100

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))
    check = next(check for check in out["checks"] if check["id"] == "indicator_regression")
    assert check["status"] == "ok", check
    assert check["sample_contract"]["rows_per_stock"] == 5

    sample_path = migration_dir / "raw_sample.csv"
    with sample_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        sample = list(reader)[1:]
    with sample_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sample)
    rerun = run_adjustment_acceptance(migration_dir=str(migration_dir))
    check = next(check for check in rerun["checks"] if check["id"] == "indicator_regression")
    assert any(row["field"] == "sample_rows_per_stock" for row in check["failures"])
    assert any(row["field"] == "sample_membership" for row in check["failures"])

    summary_path = migration_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["ordinary_raw_sample"]["seed"] = "999"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    rerun = run_adjustment_acceptance(migration_dir=str(migration_dir))
    check = next(check for check in rerun["checks"] if check["id"] == "indicator_regression")
    assert any(row["field"] == "sample_seed" for row in check["failures"])


def test_indicator_regression_allows_null_amount_on_suspended_rows(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE stock_daily_raw SET amount=NULL"
            " WHERE stock_code='600001' AND trade_date='20261014'"
        )
        cur.execute(
            "UPDATE stock_daily_hfq SET amount=NULL"
            " WHERE stock_code='600001' AND trade_date='20261014'"
        )
    conn.commit()
    conn.close()

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "indicator_regression")
    assert check["status"] == "ok", check


def test_sample_failure_expands_to_same_class_without_resampling(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE stock_daily_hfq SET close=close+1"
            " WHERE stock_code='600001' AND trade_date='20261014'"
        )
    conn.commit()
    conn.close()

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "indicator_regression")
    assert check["status"] == "error"
    assert check["expanded_to"] == [{
        "classification": "event", "stocks": 3, "rows": 15, "failure_count": 1,
        "legacy_unexplained": 0,
    }]
    assert check["expanded_rows_checked"] == 15
    assert check["failures"][0] == {
        "stock_code": "600001", "trade_date": "20261014", "field": "hfq=raw*F",
    }


def test_event_snapshot_reconciles_count_stock_set_and_values(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "event_reconciliation")
    assert check["status"] == "ok", check
    assert check["snapshot_rows"] == 4
    assert check["local_rows"] == 4
    assert check["snapshot_stocks"] == 3
    assert check["snapshot_hash"] == check["local_hash"]
    assert check["relative_tolerance"] == 0.001
    assert check["failures"] == []


def test_hash_idempotency_compares_every_stock_and_table_after_resume(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    resumed = _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    assert resumed["summary"]["upstream_requests"]["total"] == 0

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "hash_idempotency")
    assert check["status"] == "ok", check
    assert check["stocks_compared"] == 3
    assert check["tables"] == ["raw", "event", "hfq", "state"]
    assert check["differences"] == []
    assert check["resume_zero_upstream"] is True
    assert check["duration_s"] >= 0
    assert check["performance_budget_s"] == 600
    assert check["revision_scenarios"] == {
        "raw_revision": "ok", "factor_revision": "ok",
        "event_retraction": "ok", "repeat_hash_stable": "ok",
    }


def test_structure_coverage_checks_full_set_and_state_matrix(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "structure_coverage")
    assert check["status"] == "ok", check
    assert check["raw_rows"] == 15
    assert check["hfq_rows"] == 15
    assert check["state_rows"] == 3
    assert check["primary_key_duplicates"] == 0
    assert check["state_shape_errors"] == 0
    assert check["event_alignment_errors"] == 0
    assert check["factor_days_covered"] == 5
    assert check["raw_days_covered"] == 5
    assert check["pending_states"] == 0
    assert check["unknown_baseline_coverage"] == "partial"


def test_factor_coverage_uses_latest_ledger_status_per_day(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_log (trade_date, section, status, error_kind, detail)"
            " VALUES ('20261014', 'stock_adjust_factor', 'error', 'fetch_failed', %s)",
            ("adj-backfill event 20261012~20261016 latest failure",),
        )
    conn.commit()
    conn.close()

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "structure_coverage")
    assert check["status"] == "error"
    assert check["factor_days_covered"] == 4
    assert check["blocking"]["factor_date_coverage"] == 4


def test_exact_d1_and_raw_day_row_counts_reject_cross_gap_fallback(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM stock_daily_raw"
            " WHERE stock_code='600002' AND trade_date='20261013'"
        )
    conn.commit()
    conn.close()

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    factor = next(check for check in out["checks"] if check["id"] == "factor_baseline")
    assert any(
        row["stock_code"] == "600002" and "pctChg/D-1" in row["fields"]
        for row in factor["failures"]
    )
    events = next(check for check in out["checks"] if check["id"] == "event_invariants")
    assert any(
        row["stock_code"] == "600002" and "D-1" in row["fields"]
        for row in events["failures"]
    )
    structure = next(check for check in out["checks"] if check["id"] == "structure_coverage")
    assert structure["raw_day_row_mismatches"] == 1
    assert structure["blocking"]["raw_day_row_mismatches"] == 1


def test_unknown_baseline_and_pending_valid_state_block_structure_pass(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE stock_factor_state SET baseline_factor=NULL, factor_latest=NULL,"
            " rebuild_pending=true WHERE stock_code='600001'"
        )
        cur.execute(
            "UPDATE stock_factor_state SET rebuild_pending=true"
            " WHERE stock_code='600002'"
        )
    conn.commit()
    conn.close()

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "structure_coverage")
    assert check["status"] == "error"
    assert check["pending_states"] == 2
    assert check["state_shape_errors"] == 0
    assert check["unknown_baseline_coverage"] == "partial"
    assert check["unknown_baseline_cases_checked"] == 1
    assert check["coverage_failures"] == []
    assert check["blocking"]["pending_states"] == 2
    event_check = next(check for check in out["checks"] if check["id"] == "event_invariants")
    failure = next(
        row for row in event_check["failures"]
        if row["stock_code"] == "600001" and row["event_date"] == "20261013"
    )
    assert "D:qfq*F_latest=hfq" in failure["fields"]


def test_factor_and_first_event_baselines_reconcile_with_pctchg(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "factor_baseline")
    assert check["status"] == "ok", check
    assert check["events_checked"] == 4
    assert check["first_events_checked"] == 3
    assert check["relative_tolerance"] == 0.001
    assert check["failures"] == []


def test_all_event_neighborhoods_pass_hard_invariants(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "event_invariants")
    assert check["status"] == "ok", check
    assert check["events_checked"] == 4
    assert check["neighborhood_rows_checked"] == 12
    assert check["failures"] == []


def test_event_failure_locates_stock_date_field_and_event(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE stock_daily_hfq SET close=close+1"
            " WHERE stock_code='600001' AND trade_date='20261015'"
        )
    conn.commit()
    conn.close()

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "event_invariants")
    assert check["status"] == "error"
    assert check["failure_count"] == 1
    failure = next(row for row in check["failures"] if row["event_date"] == "20261015")
    assert failure["stock_code"] == "600001"
    assert failure["trade_date"] == "20261015"
    assert "D:hfq=raw*F" in failure["fields"]


def test_event_invariants_read_actual_qfq_from_price_reader(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE stock_factor_state SET factor_latest=8 WHERE stock_code='600001'"
        )
    conn.commit()
    conn.close()

    out = run_adjustment_acceptance(migration_dir=str(migration_dir))

    check = next(check for check in out["checks"] if check["id"] == "event_invariants")
    assert check["status"] == "ok", check
    assert check["reader_stocks_checked"] == 3
    structure = next(check for check in out["checks"] if check["id"] == "structure_coverage")
    assert structure["status"] == "error"
    assert structure["state_factor_latest_mismatches"] == 1


def test_acceptance_writes_only_summary_failures_and_diagnostics(pg_test, tmp_path):
    migration_dir = tmp_path / "migration"
    report_dir = tmp_path / "acceptance"
    _run(pg_test, FakeBackfillAdapter(), report_dir=str(migration_dir))

    out = run_adjustment_acceptance(
        migration_dir=str(migration_dir), report_dir=str(report_dir)
    )

    assert out["artifacts"] == {
        "summary": str(report_dir / "acceptance_summary.json"),
        "failures": str(report_dir / "acceptance_failures.csv"),
        "indicator_diagnostics": str(report_dir / "indicator_diagnostics.csv"),
    }
    with (report_dir / "acceptance_summary.json").open(encoding="utf-8") as handle:
        assert json.load(handle)["status"] == out["status"]
    with (report_dir / "acceptance_failures.csv").open(encoding="utf-8", newline="") as handle:
        failures = list(csv.DictReader(handle))
    assert failures
    assert {"check_id", "stock_code", "trade_date", "field", "event_date"} <= set(failures[0])
    assert not (report_dir / "all_rows.csv").exists()


def test_adj_acceptance_cli_contract(pg_test, capsys, monkeypatch, tmp_path):
    from qstock_mcp import scheduler

    calls = []
    monkeypatch.setattr(
        scheduler, "run_adjustment_acceptance",
        lambda **kwargs: calls.append(kwargs) or {
            "status": "ok", "stage": "verified", "messages": ["数据可以使用"],
        },
    )
    rc = scheduler.main([
        "adj-acceptance", "--migration-dir", str(tmp_path / "migration"),
        "--smoke-days", "3", "--smoke-end", END, "--throttle", "0",
        "--report-dir", str(tmp_path / "acceptance"),
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["job"] == "adj-acceptance"
    assert calls[0] == {
        "migration_dir": str(tmp_path / "migration"), "smoke_days": 3,
        "smoke_end": END, "throttle": 0.0,
        "report_dir": str(tmp_path / "acceptance"),
    }

    monkeypatch.setattr(
        scheduler, "run_adjustment_acceptance",
        lambda **kwargs: {"status": "partial_error", "stage": "rebuilt"},
    )
    assert scheduler.main([
        "adj-acceptance", "--migration-dir", str(tmp_path / "migration"),
    ]) == 1
    capsys.readouterr()
