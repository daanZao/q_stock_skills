"""live 抽样验收：固定种子、样本限制与隔离 Universe（issue #134）。"""

import json
from pathlib import Path
import subprocess
import sys

import psycopg2

from qstock_mcp import scheduler
from qstock_mcp.adapters import FetchError
from qstock_mcp.dates import trading_day_window
from qstock_mcp.repository import select_stock_codes
from qstock_mcp.tools_live_sample import run_live_sample

from fakes import FakeInitAdapter, FakeSwBoardsAdapter, trading_rows


def _list_rows():
    groups = {
        "sh_main": ["600000", "600001", "600002", "600003"],
        "sz_main": ["000001", "000002", "000003", "000004"],
        "star": ["688001", "688002", "688003", "688004"],
        "chinext": ["300001", "300002", "300003", "300004"],
        "bse": ["920001", "920002", "920003", "920004"],
    }
    return [
        {"stock_code": code, "stock_name": f"样本{code}"}
        for codes in groups.values()
        for code in codes
    ]


class SampleDailyAdapter:
    name = "fake-live"

    def __init__(self, behaviors=None):
        self.behaviors = behaviors or {}
        self.calls = []

    def fetch_daily(self, stock_code, start, end, adj="qfq"):
        self.calls.append((stock_code, start, end, adj))
        behavior = self.behaviors.get(stock_code, "full")
        if behavior == "error":
            raise FetchError(f"{stock_code} unreachable", kind="connection")
        if behavior == "empty":
            return []
        rows = trading_rows(start, end)
        if behavior == "short":
            return rows[-10:]
        if behavior == "long":
            for row in rows[:20]:
                row.update(
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=None,
                    change_percent=None,
                )
        return rows


def _run(pg_test, daily, seed=7):
    return run_live_sample(
        seed=seed,
        end="20261028",
        daily_adapters=[daily],
        list_adapters=[FakeInitAdapter("fake-list", list_rows=_list_rows())],
        sw_adapters=[FakeSwBoardsAdapter()],
        throttle=0,
    )


def test_live_sample_is_reproducible_and_scopes_universe(pg_test):
    daily = SampleDailyAdapter()
    first = _run(pg_test, daily)
    second = _run(pg_test, SampleDailyAdapter())

    assert first["status"] == "ok"
    assert first["scope"] == "live_sample"
    assert first["seed"] == 7
    assert first["representatives"]["range"] == {
        "start": trading_day_window(90, "20261028")[0],
        "end": "20261028",
        "trading_days": 90,
    }
    assert first["boards"]["range"] == {
        "start": trading_day_window(10, "20261028")[0],
        "end": "20261028",
        "trading_days": 10,
    }
    groups = first["representatives"]["groups"]
    assert set(groups) == {"sh_main", "sz_main", "star", "chinext", "bse"}
    for group in groups.values():
        assert len(group["initial_codes"]) == 3
        assert group["supplement"] is None
        assert len(group["attempts"]) == 3
        assert all(attempt["count"] == 90 for attempt in group["attempts"])
        assert all(attempt["sources"] == ["fake-live"] for attempt in group["attempts"])

    boards = first["boards"]["selected"]
    assert 2 <= len(boards) <= 3
    assert 100 <= sum(board["constituent_count"] for board in boards) <= 300
    assert first["boards"]["size_target_met"] is True
    board_universe = set(first["boards"]["stock_codes"])
    assert len(first["boards"]["attempts"]) == len(board_universe)
    assert all(attempt["count"] == 10 for attempt in first["boards"]["attempts"])

    conn = psycopg2.connect(pg_test)
    try:
        assert set(select_stock_codes(conn)) == set(first["universe"]["stock_codes"])
    finally:
        conn.close()
    assert first["universe"]["stock_codes"] == sorted(first["universe"]["stock_codes"])
    assert {
        item["code"] for item in first["coverage"]["stocks"]
    } == set(first["universe"]["stock_codes"])
    assert first["coverage"]["status"] == "ok"
    assert {
        name: group["initial_codes"] for name, group in
        second["representatives"]["groups"].items()
    } == {name: group["initial_codes"] for name, group in groups.items()}
    assert second["boards"]["selected"] == boards


def test_anomalies_get_one_supplement_and_failures_stay_distinct(pg_test):
    behaviors = {}
    for prefix, behavior in (
        ("600", "long"),
        ("000", "short"),
        ("688", "empty"),
        ("920", "error"),
    ):
        for row in _list_rows():
            if row["stock_code"].startswith(prefix):
                behaviors[row["stock_code"]] = behavior
    out = _run(pg_test, SampleDailyAdapter(behaviors))
    groups = out["representatives"]["groups"]

    assert out["status"] == "partial_error"
    for name, reason in (
        ("sh_main", "long_suspension"),
        ("sz_main", "history_insufficient"),
        ("star", "no_data"),
    ):
        group = groups[name]
        assert len(group["initial_codes"]) == 3
        assert len(group["attempts"]) == 4
        assert group["supplement"] is not None
        assert reason in group["supplement"]["replacement_reasons"]
        assert group["sample_limit"] == "replacement_still_limited"
        assert all(reason in item["sample_limits"] for item in group["attempts"])
    assert all(
        "new_stock" in item["sample_limits"]
        for item in groups["sz_main"]["attempts"]
    )
    assert all(
        "long_suspension" not in item["sample_limits"]
        and item["suspension_basis"] is None
        for item in groups["sz_main"]["attempts"] + groups["star"]["attempts"]
    )

    assert len(groups["bse"]["attempts"]) == 3
    assert groups["bse"]["supplement"] is None
    assert all(item["status"] == "error" for item in groups["bse"]["attempts"])
    assert all(item["sample_limits"] == [] for item in groups["bse"]["attempts"])
    assert groups["chinext"]["supplement"] is None
    assert out["summary"]["errors"] == 3


def test_non_test_database_is_rejected_before_upstream(monkeypatch):
    monkeypatch.setenv("PG_DSN", "postgresql://localhost/qstock")
    listed = FakeInitAdapter("fake-list", list_rows=_list_rows())
    daily = SampleDailyAdapter()
    out = run_live_sample(
        end="20261028",
        daily_adapters=[daily],
        list_adapters=[listed],
        sw_adapters=[FakeSwBoardsAdapter()],
        throttle=0,
    )

    assert out["status"] == "error"
    assert "隔离库" in out["error"]
    assert listed.calls == []
    assert daily.calls == []


def test_scheduler_live_sample_cli(pg_test, capsys):
    rc = scheduler.main(
        ["live-sample", "--seed", "7", "--end", "20261028", "--throttle", "0"],
        adapters=[FakeSwBoardsAdapter()],
        daily_adapters=[SampleDailyAdapter()],
        list_adapters=[FakeInitAdapter("fake-list", list_rows=_list_rows())],
    )
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["status"] == "ok"
    assert out["job"] == "live-sample"
    assert out["mode"] == "live"
    assert out["scope"] == "live_sample"
    assert out["seed"] == 7


def test_live_sample_respects_shared_runner_lock(pg_test):
    root = Path(__file__).resolve().parents[1]
    code = """
import json
import sys
sys.path.insert(0, 'tests')
from qstock_mcp.tools_live_sample import run_live_sample
from fakes import FakeInitAdapter, FakeSwBoardsAdapter
from test_live_sample import SampleDailyAdapter, _list_rows
out = run_live_sample(
    end='20261028', daily_adapters=[SampleDailyAdapter()],
    list_adapters=[FakeInitAdapter('fake-list', list_rows=_list_rows())],
    sw_adapters=[FakeSwBoardsAdapter()], throttle=0,
)
print(json.dumps(out))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    out = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert out["status"] == "error"
    assert "runner 锁" in out["error"]
