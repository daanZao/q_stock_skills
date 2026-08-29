"""indicator-tools（管道版）测试：已知数据集数值、边界规则、数据不足语义、退出码。

口径基线继承旧契约（旧系统 data_provider/base.py calculate_indicators）：
min_periods=窗口、MACD EMA(12,26,9) adjust=False、RSI Wilder、BOLL(20,2) ddof=0。
测试不依赖 pandas/numpy，期望值均手工计算冻结。
"""

import io
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import indicators  # noqa: E402


def _query_payload(closes, code="600519"):
    """构造 query_daily 输出结构（升序 rows）。"""
    rows = [
        {
            "trade_date": f"2026-01-{i + 1:02d}" if i < 31 else f"2026-02-{i - 30:02d}",
            "open": c,
            "high": c + 1,
            "low": c - 1,
            "close": c,
            "volume": 1000000 + i,
            "amount": (1000000 + i) * c,
            "amplitude": None,
            "change_percent": None,
            "change_amount": None,
            "turnover_rate": None,
            "source": "fake",
        }
        for i, c in enumerate(closes)
    ]
    return {
        "status": "ok",
        "tool": "query_daily",
        "params": {"stock_code": code, "adj": "qfq", "days": None,
                   "start": None, "end": None},
        "range": {"start": rows[0]["trade_date"], "end": rows[-1]["trade_date"]},
        "data_range": {"start": rows[0]["trade_date"], "end": rows[-1]["trade_date"]},
        "count": len(rows),
        "healed": [],
        "rows": rows,
    }


def _run_cli(monkeypatch, capsys, payload, *argv):
    """以 payload 为 stdin 跑 main()，返回 (exit_code, 输出 JSON)。"""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "argv", ["indicators.py", *argv])
    rc = indicators.main()
    return rc, json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- 指标数值（已知数据集）

class TestMA:
    def test_values_and_boundary(self):
        got = indicators.calc_ma([1.0, 2.0, 3.0, 4.0, 5.0], [3])
        assert got["ma3"] == [None, None, 2.0, 3.0, 4.0]

    def test_multi_periods(self):
        got = indicators.calc_ma([2.0] * 5, [2, 5])
        assert got["ma2"] == [None, 2.0, 2.0, 2.0, 2.0]
        assert got["ma5"] == [None, None, None, None, 2.0]


class TestMACD:
    def test_hand_computed(self):
        # fast=2, slow=3, signal=2；ema(alpha=2/(span+1), adjust=False) 从头递归，
        # min_periods 只掩码头部。closes=[1,2,3,4]
        got = indicators.calc_macd([1.0, 2.0, 3.0, 4.0], fast=2, slow=3, signal=2)
        ema_fast = [None, 5 / 3, 23 / 9, 95 / 27]
        ema_slow = [None, None, 2.25, 3.125]
        dif = [None, None, 23 / 9 - 2.25, 95 / 27 - 3.125]
        dea = [None, None, None, (2 / 3) * dif[3] + (1 / 3) * dif[2]]
        bar = [None, None, None, (dif[3] - dea[3]) * 2]
        for name, expected in [("dif", dif), ("dea", dea), ("bar", bar)]:
            for g, e in zip(got[name], expected):
                if e is None:
                    assert g is None
                else:
                    assert g == pytest.approx(e, abs=1e-12)

    def test_default_params_boundary(self):
        closes = [100.0 + i * 0.1 for i in range(40)]
        got = indicators.calc_macd(closes)
        assert all(v is None for v in got["dif"][:25])
        assert got["dif"][25] is not None
        assert all(v is None for v in got["dea"][:33])
        assert got["dea"][33] is not None
        assert got["bar"][33] is not None


class TestBOLL:
    def test_hand_computed(self):
        # closes=[1,2,3] period=2 k=2：mid=[_,1.5,2.5]，std(ddof=0)=[_,0.5,0.5]
        got = indicators.calc_boll([1.0, 2.0, 3.0], period=2, k=2.0)
        assert got["mid"] == [None, 1.5, 2.5]
        assert got["upper"] == [None, 2.5, 3.5]
        assert got["lower"] == [None, 0.5, 1.5]

    def test_constant_series_zero_std(self):
        got = indicators.calc_boll([7.0] * 20)
        assert got["mid"][-1] == got["upper"][-1] == got["lower"][-1] == 7.0


class TestRSI:
    def test_hand_computed(self):
        # closes=[10,11,10,11,10] p=2（Wilder alpha=1/2）：
        # gain=[0,1,0,1,0] → avg_gain=[_,0.5,0.25,0.625,0.3125]
        # loss=[0,0,1,0,1] → avg_loss=[_,0,0.5,0.25,0.625]（0 按 1e-10 兜底）
        got = indicators.calc_rsi([10.0, 11.0, 10.0, 11.0, 10.0], [2])["rsi_2"]
        assert got[0] is None
        assert got[1] == pytest.approx(100.0)  # avg_loss=0 → rs 极大
        assert got[2] == pytest.approx(100 - 100 / 1.5, abs=1e-9)   # rs=0.5
        assert got[3] == pytest.approx(100 - 100 / 3.5, abs=1e-9)   # rs=2.5
        assert got[4] == pytest.approx(100 - 100 / 1.5, abs=1e-9)

    def test_boundary_head_nulls(self):
        got = indicators.calc_rsi([float(i) for i in range(1, 30)], [6, 12, 24])
        # ewm min_periods=p：前 p-1 点为 null，第 p-1 位起有值
        for p in (6, 12, 24):
            assert all(v is None for v in got[f"rsi_{p}"][:p - 1])
            assert got[f"rsi_{p}"][p - 1] is not None


class TestDerivative:
    def test_diff1_boundary(self):
        assert indicators.derivative([10.0, 12.0, 11.0, 15.0], 1) == [None, 2.0, -1.0, 4.0]

    def test_diff2_boundary(self):
        # diff1=[_,2,-1,4]; diff2=[_,_,-3,5]
        assert indicators.derivative([10.0, 12.0, 11.0, 15.0], 2) == [None, None, -3.0, 5.0]

    def test_invalid_order(self):
        with pytest.raises(ValueError):
            indicators.derivative([1.0, 2.0], 3)


class TestExtendedTools:
    def test_rolling_max_min(self):
        cols = {"close": [1.0, 3.0, 2.0, 5.0, 4.0]}
        out = indicators.build_series(cols, "rolling_max", {"on": "close", "window": 3})
        assert out["rolling_max3_close"] == [None, None, 3.0, 5.0, 5.0]
        out = indicators.build_series(cols, "rolling_min", {"on": "close", "window": 3})
        assert out["rolling_min3_close"] == [None, None, 1.0, 2.0, 2.0]

    def test_maxdd(self):
        cols = {"close": [10.0, 12.0, 9.0, 11.0, 8.0]}
        out = indicators.build_series(cols, "maxdd", {"on": "close", "window": 3})
        s = out["maxdd3_close"]
        # 窗口 [10,12,9]：dd 最小 9/12-1=-25%；窗口 [12,9,11]：min(-25%, 11/12-1) = -25%
        assert s[:2] == [None, None]
        assert s[2] == pytest.approx(-25.0)
        assert s[3] == pytest.approx(-25.0)
        # 窗口 [9,11,8]：8/11-1=-27.27%
        assert s[4] == pytest.approx((8 / 11 - 1) * 100)

    def test_chg(self):
        cols = {"close": [100.0, 110.0, 121.0]}
        out = indicators.build_series(cols, "chg", {"on": "close", "window": 2})
        assert out["chg2_close"][:2] == [None, None]
        assert out["chg2_close"][2] == pytest.approx(21.0)

    def test_ma_on_volume(self):
        cols = {"close": [1.0] * 5, "volume": [10.0, 20.0, 30.0, 40.0, 50.0]}
        out = indicators.build_series(cols, "ma", {"on": "volume", "periods": [3]})
        assert out["ma3_volume"] == [None, None, 20.0, 30.0, 40.0]

    def test_diff_on_ma(self):
        cols = {"close": [1.0, 2.0, 3.0, 4.0]}
        out = indicators.build_series(cols, "diff1", {"on": "ma2"})
        # ma2=[_,1.5,2.5,3.5]；diff1 首点 null，ma2 头部 null 扩散一位
        assert out["diff1_ma2"] == [None, None, 1.0, 1.0]


# ---------------------------------------------------------------- 最小长度

class TestRequiredLength:
    def test_lengths(self):
        assert indicators.required_length("ma", {"periods": [5, 10, 20, 60]}) == 60
        assert indicators.required_length("macd", {"slow": 26, "signal": 9}) == 35
        assert indicators.required_length("boll", {"period": 20}) == 20
        assert indicators.required_length("rsi", {"periods": [6, 12, 24]}) == 25
        assert indicators.required_length("diff1", {}) == 2
        assert indicators.required_length("diff2", {}) == 3
        assert indicators.required_length("rolling_max", {"window": 250}) == 250
        assert indicators.required_length("maxdd", {"window": 20}) == 20
        assert indicators.required_length("chg", {"window": 250}) == 251


# ---------------------------------------------------------------- CLI 契约

class TestCLI:
    def test_ok_output_contract(self, monkeypatch, capsys):
        closes = [100.0 + (i % 17) * 0.5 for i in range(120)]
        rc, out = _run_cli(monkeypatch, capsys, _query_payload(closes),
                           "--indicator", "ma", "--periods", "5,20", "--tail", "10")
        assert rc == 0
        assert out["status"] == "ok"
        assert out["indicator"] == "ma"
        assert out["stock_code"] == "600519"
        assert out["params"]["periods"] == [5, 20]
        assert out["required_length"] == 20
        assert out["available_length"] == 120
        assert len(out["series"]) == 10
        point = out["series"][0]
        assert set(point) == {"trade_date", "ma5", "ma20"}
        assert out["series"][-1]["ma5"] == pytest.approx(
            sum(closes[-5:]) / 5, abs=1e-4)

    def test_tail_zero_outputs_all(self, monkeypatch, capsys):
        rc, out = _run_cli(monkeypatch, capsys, _query_payload([10.0, 11.0, 12.0]),
                           "--indicator", "diff1", "--tail", "0")
        assert rc == 0
        assert len(out["series"]) == 3
        assert out["series"][0]["diff1_close"] is None
        assert out["series"][1]["diff1_close"] == 1.0

    def test_insufficient_data_exit_0(self, monkeypatch, capsys):
        rc, out = _run_cli(monkeypatch, capsys, _query_payload([10.0, 11.0, 12.0]),
                           "--indicator", "ma")
        assert rc == 0
        assert out["status"] == "insufficient_data"
        assert out["required_length"] == 60
        assert out["available_length"] == 3

    def test_empty_rows_is_insufficient(self, monkeypatch, capsys):
        payload = _query_payload([1.0])
        payload["rows"] = []
        payload["count"] = 0
        rc, out = _run_cli(monkeypatch, capsys, payload, "--indicator", "macd")
        assert rc == 0
        assert out["status"] == "insufficient_data"
        assert out["available_length"] == 0

    def test_bad_on_param_exit_2(self, monkeypatch, capsys):
        rc, out = _run_cli(monkeypatch, capsys, _query_payload([1.0, 2.0, 3.0]),
                           "--indicator", "diff1", "--on", "bogus")
        assert rc == 2
        assert out["status"] == "error"

    def test_bad_on_not_masked_by_insufficient_data(self, monkeypatch, capsys):
        """参数错误优先于数据不足：非法 --on + 短数据仍 exit 2。"""
        rc, out = _run_cli(monkeypatch, capsys, _query_payload([1.0, 2.0]),
                           "--indicator", "ma", "--on", "bogus")
        assert rc == 2
        assert out["status"] == "error"

    def test_bad_indicator_choice_exit_2(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_query_payload([1.0]))))
        monkeypatch.setattr(sys, "argv", ["indicators.py", "--indicator", "kdj"])
        with pytest.raises(SystemExit) as exc:
            indicators.main()
        assert exc.value.code == 2

    def test_malformed_stdin_exit_1(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
        monkeypatch.setattr(sys, "argv", ["indicators.py", "--indicator", "ma"])
        rc = indicators.main()
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"

    def test_upstream_error_status_exit_1(self, monkeypatch, capsys):
        payload = {"status": "error", "tool": "query_daily", "error": "分段补抓失败"}
        rc, out = _run_cli(monkeypatch, capsys, payload, "--indicator", "ma")
        assert rc == 1
        assert out["status"] == "error"

    def test_no_db_access(self):
        """指标不物化：脚本不得引入任何数据库客户端。"""
        src = (SCRIPTS / "indicators.py").read_text(encoding="utf-8")
        assert "psycopg2" not in src
        assert "import db" not in src
