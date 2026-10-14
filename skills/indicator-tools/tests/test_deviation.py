"""deviation 管道行为测试（TDD：先于实现）。

只测管道外部行为：stdin JSON → stdout JSON + 退出码，不测内部函数。
deviation 输入为双腿结构：{"status":"ok","stock":<query_daily 输出>,
"benchmark":<query_daily/query_index_daily 输出>}，按 trade_date 交集对齐后
计算逐日偏离值（个股涨跌幅 − 基准涨跌幅）的窗口累计。
"""

import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import indicators  # noqa: E402


def _run(monkeypatch, capsys, payload, *argv):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    monkeypatch.setattr(sys, "argv", ["indicators.py", *argv])
    rc = indicators.main()
    return rc, json.loads(capsys.readouterr().out)


def _date(i):
    return (date(2026, 1, 1) + timedelta(days=i)).isoformat()


def _rows_cp(cps, start=0):
    """构造只含偏离值所需字段的日线 rows（cps 为 change_percent 序列，可含 None）。"""
    return [
        {
            "trade_date": _date(start + i),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000.0,
            "amount": 100.0,
            "change_percent": cp,
        }
        for i, cp in enumerate(cps)
    ]


def _payload(stock_cps, bench_cps, stock_start=0, bench_start=0,
             stock_code="002343", bench_code="399001"):
    return {
        "status": "ok",
        "stock": {"status": "ok", "params": {"stock_code": stock_code},
                  "rows": _rows_cp(stock_cps, stock_start)},
        "benchmark": {"status": "ok", "params": {"index_code": bench_code},
                      "rows": _rows_cp(bench_cps, bench_start)},
    }


class TestDeviationKnownValues:
    def test_constant_spread(self, monkeypatch, capsys):
        """个股每日恒比基准多涨 1%：dev10 末点=10，dev30 末点=30。"""
        stock = [float(i % 5) + 1.0 for i in range(40)]
        bench = [cp - 1.0 for cp in stock]
        rc, out = _run(monkeypatch, capsys, _payload(stock, bench),
                       "--indicator", "deviation")
        assert rc == 0
        assert out["status"] == "ok"
        assert out["indicator"] == "deviation"
        assert out["stock_code"] == "002343"
        assert out["benchmark_code"] == "399001"
        last = out["series"][-1]
        assert last["dev10"] == 10.0
        assert last["dev30"] == 30.0

    def test_head_nulls_per_window(self, monkeypatch, capsys):
        """min_periods=窗口：dev10 前 9 点 null，dev30 前 29 点 null。"""
        stock = [1.0] * 35
        bench = [0.0] * 35
        rc, out = _run(monkeypatch, capsys, _payload(stock, bench),
                       "--indicator", "deviation", "--tail", "0")
        assert rc == 0
        pts = out["series"]
        assert all(p["dev10"] is None for p in pts[:9])
        assert pts[9]["dev10"] == 10.0
        assert all(p["dev30"] is None for p in pts[:29])
        assert pts[29]["dev30"] == 30.0

    def test_partial_window_all_null(self, monkeypatch, capsys):
        """aligned 够小窗口但不够大窗口：dev30 全 null 仍 ok。"""
        stock = [1.0] * 15
        bench = [0.0] * 15
        rc, out = _run(monkeypatch, capsys, _payload(stock, bench),
                       "--indicator", "deviation", "--tail", "0")
        assert rc == 0
        assert out["status"] == "ok"
        assert all(p["dev30"] is None for p in out["series"])
        assert out["series"][-1]["dev10"] == 10.0

    def test_sign_symmetry(self, monkeypatch, capsys):
        """个股每日恒比基准少涨 2%：累计为负。"""
        stock = [0.0] * 40
        bench = [2.0] * 40
        rc, out = _run(monkeypatch, capsys, _payload(stock, bench),
                       "--indicator", "deviation")
        assert rc == 0
        assert out["series"][-1]["dev10"] == -20.0
        assert out["series"][-1]["dev30"] == -60.0


class TestDeviationAlignment:
    def test_intersection_by_trade_date(self, monkeypatch, capsys):
        """基准起始日晚 5 天：只对齐交集，dev10 仍可算。"""
        stock = [1.0] * 20
        bench = [0.0] * 15
        rc, out = _run(monkeypatch, capsys,
                       _payload(stock, bench, bench_start=5),
                       "--indicator", "deviation", "--tail", "0")
        assert rc == 0
        assert out["aligned_length"] == 15
        assert out["series"][0]["trade_date"] == _date(5)
        assert out["series"][-1]["dev10"] == 10.0

    def test_null_change_percent_excluded(self, monkeypatch, capsys):
        """任一侧 change_percent 为 null 的交易日从对齐集剔除。"""
        stock = [None] + [1.0] * 19
        bench = [0.0] * 20
        rc, out = _run(monkeypatch, capsys, _payload(stock, bench),
                       "--indicator", "deviation", "--tail", "0")
        assert rc == 0
        assert out["aligned_length"] == 19
        assert out["series"][0]["trade_date"] == _date(1)
        assert out["series"][-1]["dev10"] == 10.0


class TestDeviationSkipLeading:
    def test_ipo_surge_excluded(self, monkeypatch, capsys):
        """--skip-leading 5：新股前 5 日极端涨幅不计入累计。"""
        stock = [100.0] * 5 + [1.0] * 10
        bench = [0.0] * 15
        rc, out = _run(monkeypatch, capsys, _payload(stock, bench),
                       "--indicator", "deviation", "--skip-leading", "5",
                       "--tail", "0")
        assert rc == 0
        assert out["aligned_length"] == 10
        assert out["excluded_leading_days"] == [_date(i) for i in range(5)]
        pts = out["series"]
        assert pts[0]["trade_date"] == _date(5)
        assert all(p["dev10"] is None for p in pts[:-1])
        assert pts[-1]["dev10"] == 10.0

    def test_without_skip_surge_included(self, monkeypatch, capsys):
        """对照组：不传 --skip-leading 时新股涨幅计入。"""
        stock = [100.0] * 5 + [1.0] * 10
        bench = [0.0] * 15
        rc, out = _run(monkeypatch, capsys, _payload(stock, bench),
                       "--indicator", "deviation", "--tail", "0")
        assert rc == 0
        assert out["excluded_leading_days"] == []
        # 第 10 个对齐点（index 9）的 dev10 覆盖 5 日暴涨 + 5 日 +1
        assert out["series"][9]["dev10"] == 505.0

    def test_skip_leading_exhausts_series(self, monkeypatch, capsys):
        """剔除后不足最小窗口：insufficient_data。"""
        stock = [100.0] * 5 + [1.0] * 4
        bench = [0.0] * 9
        rc, out = _run(monkeypatch, capsys, _payload(stock, bench),
                       "--indicator", "deviation", "--skip-leading", "5")
        assert rc == 0
        assert out["status"] == "insufficient_data"
        assert out["available_length"] == 4


class TestDeviationContract:
    def test_insufficient_data_exit_0(self, monkeypatch, capsys):
        stock = [1.0] * 5
        bench = [0.0] * 5
        rc, out = _run(monkeypatch, capsys, _payload(stock, bench),
                       "--indicator", "deviation")
        assert rc == 0
        assert out["status"] == "insufficient_data"
        assert out["required_length"] == 10
        assert out["available_length"] == 5

    def test_custom_windows(self, monkeypatch, capsys):
        stock = [1.0] * 10
        bench = [0.0] * 10
        rc, out = _run(monkeypatch, capsys, _payload(stock, bench),
                       "--indicator", "deviation", "--windows", "5")
        assert rc == 0
        last = out["series"][-1]
        assert last["dev5"] == 5.0
        assert "dev10" not in last

    def test_missing_leg_exit_1(self, monkeypatch, capsys):
        payload = {"status": "ok", "stock": {"status": "ok", "rows": _rows_cp([1.0])}}
        rc, out = _run(monkeypatch, capsys, payload, "--indicator", "deviation")
        assert rc == 1
        assert out["status"] == "error"

    def test_bad_rows_exit_1(self, monkeypatch, capsys):
        payload = _payload([1.0] * 15, [0.0] * 15)
        del payload["benchmark"]["rows"][3]["close"]
        rc, out = _run(monkeypatch, capsys, payload, "--indicator", "deviation")
        assert rc == 1
        assert out["status"] == "error"

    def test_malformed_stdin_exit_1(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, "{not json", "--indicator", "deviation")
        assert rc == 1
        assert out["status"] == "error"

    def test_upstream_error_status_exit_1(self, monkeypatch, capsys):
        payload = {"status": "error", "error": "上游失败"}
        rc, out = _run(monkeypatch, capsys, payload, "--indicator", "deviation")
        assert rc == 1
        assert out["status"] == "error"

    def test_bad_windows_exit_2(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _payload([1.0] * 15, [0.0] * 15),
                       "--indicator", "deviation", "--windows", "abc")
        assert rc == 2
        assert out["status"] == "error"

    def test_bad_skip_leading_exit_2(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _payload([1.0] * 15, [0.0] * 15),
                       "--indicator", "deviation", "--skip-leading", "-1")
        assert rc == 2
        assert out["status"] == "error"
