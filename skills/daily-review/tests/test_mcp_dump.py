"""mcp_dump 落盘通道测试：本地聚合口径 + stdio JSON-RPC 端到端（fake server，不触网不依赖 PG）。"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mcp_dump  # noqa: E402

FAKE_SERVER = Path(__file__).resolve().parent / "fake_mcp_server.py"


@pytest.mark.parametrize(
    "code,expected",
    [("600519", "sh"), ("000001", "sz"), ("300750", "sz"),
     ("430047", "bj"), ("830799", "bj"), ("920001", "bj")],
)
def test_market_bucket(code, expected):
    assert mcp_dump.market_bucket(code) == expected


def test_aggregate_rows_breadth_and_amount():
    rows = [
        {"stock_code": "600519", "latest_price": 10.0, "pre_close": 9.0, "amount": 100.0},
        {"stock_code": "600001", "latest_price": 8.0, "pre_close": 8.5, "amount": 50.0},
        {"stock_code": "000001", "latest_price": 12.0, "pre_close": 12.0, "amount": 200.0},
        {"stock_code": "300750", "latest_price": None, "pre_close": 20.0, "amount": 300.0},
        {"stock_code": "430047", "latest_price": 5.0, "pre_close": 4.0, "amount": 10.0},
        {"stock_code": "830799", "latest_price": 3.0, "pre_close": 4.0, "amount": None},
    ]
    out = mcp_dump.aggregate_rows(rows, ("breadth", "amount"))
    assert out["breadth"]["by_market"] == {
        "sh": {"up": 1, "down": 1, "flat": 0, "no_price": 0},
        "sz": {"up": 0, "down": 0, "flat": 1, "no_price": 1},
        "bj": {"up": 1, "down": 1, "flat": 0, "no_price": 0},
    }
    assert out["breadth"]["total"] == {"up": 2, "down": 2, "flat": 1, "no_price": 1}
    # 停牌行（最新价/昨收缺失）进 no_price 桶：四桶之和 = 总行数
    assert sum(out["breadth"]["total"].values()) == len(rows)
    assert out["amount"]["by_market"] == {"sh": 150.0, "sz": 500.0, "bj": 10.0}
    assert out["amount"]["total_amount"] == 660.0


def test_aggregate_rows_empty():
    out = mcp_dump.aggregate_rows([], ("breadth", "amount"))
    assert out["breadth"]["total"] == {"up": 0, "down": 0, "flat": 0, "no_price": 0}
    assert out["breadth"]["by_market"]["bj"] == {"up": 0, "down": 0, "flat": 0, "no_price": 0}
    assert out["amount"] == {"total_amount": 0.0, "by_market": {"sh": 0.0, "sz": 0.0, "bj": 0.0}}


def _server_cmd():
    return f"{sys.executable} {FAKE_SERVER}"


def _run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["mcp_dump.py", *argv])
    rc = mcp_dump.main()
    return rc, capsys.readouterr()


def test_dump_end_to_end(monkeypatch, capsys, tmp_path):
    out = tmp_path / "dump" / "snapshot.json"
    rc, captured = _run(
        monkeypatch, capsys,
        "--tool", "query_snapshot",
        "--params", '{"trade_date": "20261014"}',
        "--out", str(out),
        "--local-aggregate", "breadth,amount",
        "--server", _server_cmd(),
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["count"] == 6
    summary = json.loads(captured.out)
    assert summary["out"] == str(out)
    assert summary["aggregates"]["breadth"]["total"] == {"up": 2, "down": 2, "flat": 1, "no_price": 1}
    assert summary["aggregates"]["amount"]["total_amount"] == 660.0


def test_dump_without_aggregate(monkeypatch, capsys, tmp_path):
    out = tmp_path / "snapshot.json"
    rc, captured = _run(
        monkeypatch, capsys,
        "--tool", "query_snapshot", "--params", "{}", "--out", str(out),
        "--server", _server_cmd(),
    )
    assert rc == 0
    summary = json.loads(captured.out)
    assert "aggregates" not in summary
    assert json.loads(out.read_text(encoding="utf-8"))["tool"] == "query_snapshot"


def test_tool_iserror_exits_1(monkeypatch, capsys, tmp_path):
    rc, captured = _run(
        monkeypatch, capsys,
        "--tool", "query_snapshot", "--params", '{"trade_date": "error"}',
        "--out", str(tmp_path / "x.json"), "--server", _server_cmd(),
    )
    assert rc == 1
    assert "上游不可达" in captured.err


def test_tool_returns_non_json_exits_1(monkeypatch, capsys, tmp_path):
    rc, _ = _run(
        monkeypatch, capsys,
        "--tool", "query_snapshot", "--params", '{"trade_date": "badjson"}',
        "--out", str(tmp_path / "x.json"), "--server", _server_cmd(),
    )
    assert rc == 1


def test_server_not_found_exits_1(monkeypatch, capsys, tmp_path):
    rc, captured = _run(
        monkeypatch, capsys,
        "--tool", "query_snapshot", "--params", "{}",
        "--out", str(tmp_path / "x.json"), "--server", "/nonexistent/qstock-mcp",
    )
    assert rc == 1
    assert "server 启动失败" in captured.err


def test_bad_params_exits_2(monkeypatch, capsys, tmp_path):
    rc, _ = _run(
        monkeypatch, capsys,
        "--tool", "query_snapshot", "--params", "[1,2]",
        "--out", str(tmp_path / "x.json"), "--server", _server_cmd(),
    )
    assert rc == 2


def test_unknown_aggregate_exits_2(monkeypatch, capsys, tmp_path):
    rc, _ = _run(
        monkeypatch, capsys,
        "--tool", "query_snapshot", "--params", "{}", "--local-aggregate", "turnover",
        "--out", str(tmp_path / "x.json"), "--server", _server_cmd(),
    )
    assert rc == 2
