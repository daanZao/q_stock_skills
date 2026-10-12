"""T2 新增指标（corr / vol_price / promote）管道行为测试。

只测管道外部行为：stdin JSON → stdout JSON + 退出码，不测内部函数。
corr 为多标的输入（{"series": {代码: query_daily 输出}}），vol_price 复用
query_daily 单标的契约，promote 为昨日/今日连板数据输入。
"""

import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import indicators  # noqa: E402


def _run(monkeypatch, capsys, payload, *argv):
    """以 payload 为 stdin 跑 main()，返回 (exit_code, 输出 JSON)。"""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    monkeypatch.setattr(sys, "argv", ["indicators.py", *argv])
    rc = indicators.main()
    return rc, json.loads(capsys.readouterr().out)


def _date(i):
    return (date(2026, 1, 1) + timedelta(days=i)).isoformat()


def _rows(values, amounts=None, start=0):
    """构造日线 rows（values 为 close 序列，amounts 缺省取 100*i 起步）。"""
    return [
        {
            "trade_date": _date(start + i),
            "open": v,
            "high": v + 1,
            "low": v - 1,
            "close": v,
            "volume": 1000.0,
            "amount": (amounts[i] if amounts else 100.0 + i),
        }
        for i, v in enumerate(values)
    ]


def _corr_payload(series_map):
    """series_map: {代码: closes 或 (closes, start_offset)}。"""
    series = {}
    for code, spec in series_map.items():
        closes, start = spec if isinstance(spec, tuple) else (spec, 0)
        series[code] = {"status": "ok", "rows": _rows(closes, start=start)}
    return {"status": "ok", "series": series}


def _daily_payload(closes, amounts=None, code="600519"):
    return {
        "status": "ok",
        "tool": "query_daily",
        "params": {"stock_code": code},
        "rows": _rows(closes, amounts),
    }


def _promote_payload(yesterday, today):
    """yesterday/today: [(code, limit_count), ...] 或完整 dict。"""
    def leg(day, boards):
        if isinstance(boards, dict):
            return boards
        return {"trade_date": day,
                "boards": [{"stock_code": c, "limit_count": lc} for c, lc in boards]}
    return {"status": "ok",
            "yesterday": leg("2026-01-08", yesterday),
            "today": leg("2026-01-09", today)}


# ---------------------------------------------------------------- corr：相关性矩阵

class TestCorr:
    def test_matrix_known_values(self, monkeypatch, capsys):
        # xs=[1,2,3,4]：与 [2,4,6,8] 完全正相关，与 [8,6,4,2] 完全负相关，
        # 与 [1,1,2,2] 的 Pearson = 2/sqrt(5) ≈ 0.8944
        rc, out = _run(monkeypatch, capsys, _corr_payload({
            "AAA": [1.0, 2.0, 3.0, 4.0],
            "BBB": [2.0, 4.0, 6.0, 8.0],
            "CCC": [8.0, 6.0, 4.0, 2.0],
            "DDD": [1.0, 1.0, 2.0, 2.0],
        }), "--indicator", "corr")
        assert rc == 0
        assert out["status"] == "ok"
        assert out["indicator"] == "corr"
        assert out["symbols"] == ["AAA", "BBB", "CCC", "DDD"]
        assert out["aligned_length"] == 4
        assert out["required_length"] == 2
        m = out["matrix"]
        assert m["AAA"]["BBB"] == 1.0
        assert m["AAA"]["CCC"] == -1.0
        assert m["AAA"]["DDD"] == pytest.approx(0.8944, abs=1e-4)
        assert m["BBB"]["AAA"] == 1.0  # 对称
        assert m["AAA"]["AAA"] == 1.0  # 对角线

    def test_window_controlled_by_length(self, monkeypatch, capsys):
        """调用方用序列长度控制窗口：2 点即可计算。"""
        rc, out = _run(monkeypatch, capsys, _corr_payload({
            "AAA": [1.0, 2.0],
            "BBB": [5.0, 3.0],
        }), "--indicator", "corr")
        assert rc == 0
        assert out["aligned_length"] == 2
        assert out["matrix"]["AAA"]["BBB"] == -1.0

    def test_alignment_by_trade_date(self, monkeypatch, capsys):
        # AAA 覆盖 d0..d4，BBB 覆盖 d2..d6 → 对齐 d2..d4，各自 [3,4,5]/[10,20,30]
        rc, out = _run(monkeypatch, capsys, _corr_payload({
            "AAA": ([1.0, 2.0, 3.0, 4.0, 5.0], 0),
            "BBB": ([10.0, 20.0, 30.0, 40.0, 50.0], 2),
        }), "--indicator", "corr")
        assert rc == 0
        assert out["aligned_length"] == 3
        assert out["data_range"] == {"start": _date(2), "end": _date(4)}
        assert out["matrix"]["AAA"]["BBB"] == 1.0

    def test_zero_variance_is_null(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _corr_payload({
            "AAA": [1.0, 2.0, 3.0],
            "BBB": [5.0, 5.0, 5.0],
        }), "--indicator", "corr")
        assert rc == 0
        assert out["matrix"]["AAA"]["BBB"] is None
        assert out["matrix"]["BBB"]["BBB"] is None
        assert out["matrix"]["AAA"]["AAA"] == 1.0

    def test_insufficient_when_no_overlap(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _corr_payload({
            "AAA": ([1.0, 2.0], 0),
            "BBB": ([1.0, 2.0], 10),
        }), "--indicator", "corr")
        assert rc == 0
        assert out["status"] == "insufficient_data"
        assert out["required_length"] == 2
        assert out["available_length"] == 0

    def test_single_symbol_exit_1(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _corr_payload({"AAA": [1.0, 2.0]}),
                       "--indicator", "corr")
        assert rc == 1
        assert out["status"] == "error"

    def test_missing_series_key_exit_1(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, {"status": "ok"}, "--indicator", "corr")
        assert rc == 1
        assert out["status"] == "error"

    def test_bad_rows_exit_1(self, monkeypatch, capsys):
        payload = _corr_payload({"AAA": [1.0, 2.0], "BBB": [1.0, 2.0]})
        del payload["series"]["BBB"]["rows"]
        rc, out = _run(monkeypatch, capsys, payload, "--indicator", "corr")
        assert rc == 1

    def test_malformed_stdin_exit_1(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, "not json", "--indicator", "corr")
        assert rc == 1
        assert out["status"] == "error"

    def test_bad_on_param_exit_2(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys,
                       _corr_payload({"AAA": [1.0, 2.0], "BBB": [1.0, 2.0]}),
                       "--indicator", "corr", "--on", "bogus")
        assert rc == 2
        assert out["status"] == "error"


# ---------------------------------------------------------------- vol_price：量价背离/配合判定

class TestVolPrice:
    def test_patterns_known_dataset(self, monkeypatch, capsys):
        # close [10,11,12,11]，amount [100,130,90,150]（θ=20%）：
        # d1 价+10% 量+30% → 放量上涨；d2 价+9.09% 量-30.77% → 缩量上涨（背离）；
        # d3 价-8.33% 量+66.67% → 放量下跌
        rc, out = _run(monkeypatch, capsys,
                       _daily_payload([10.0, 11.0, 12.0, 11.0],
                                      [100.0, 130.0, 90.0, 150.0]),
                       "--indicator", "vol_price", "--tail", "0")
        assert rc == 0
        pts = out["series"]
        assert pts[0]["price_chg"] is None
        assert pts[0]["vol_price"] is None
        assert pts[1]["price_chg"] == pytest.approx(10.0, abs=1e-4)
        assert pts[1]["amount_chg"] == pytest.approx(30.0, abs=1e-4)
        assert pts[1]["vol_price"] == "放量上涨"
        assert pts[1]["divergence"] is False
        assert pts[2]["amount_chg"] == pytest.approx((90 / 130 - 1) * 100, abs=1e-4)
        assert pts[2]["vol_price"] == "缩量上涨"
        assert pts[2]["divergence"] is True
        assert pts[3]["vol_price"] == "放量下跌"
        assert pts[3]["divergence"] is False

    def test_shrink_down(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys,
                       _daily_payload([10.0, 9.0], [100.0, 50.0]),
                       "--indicator", "vol_price", "--tail", "0")
        assert rc == 0
        assert out["series"][1]["vol_price"] == "缩量下跌"
        assert out["series"][1]["divergence"] is False

    def test_flat_amount(self, monkeypatch, capsys):
        # 量 +10% 在 ±20% 内 → 平量上涨
        rc, out = _run(monkeypatch, capsys,
                       _daily_payload([10.0, 11.0], [100.0, 110.0]),
                       "--indicator", "vol_price", "--tail", "0")
        assert rc == 0
        assert out["series"][1]["vol_price"] == "平量上涨"

    def test_flat_price(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys,
                       _daily_payload([10.0, 10.0], [100.0, 200.0]),
                       "--indicator", "vol_price", "--tail", "0")
        assert rc == 0
        assert out["series"][1]["vol_price"] == "平盘"

    def test_threshold_param(self, monkeypatch, capsys):
        # --threshold 0：任何量增即放量
        rc, out = _run(monkeypatch, capsys,
                       _daily_payload([10.0, 11.0], [100.0, 101.0]),
                       "--indicator", "vol_price", "--threshold", "0", "--tail", "0")
        assert rc == 0
        assert out["series"][1]["vol_price"] == "放量上涨"

    def test_insufficient_data_exit_0(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _daily_payload([10.0]),
                       "--indicator", "vol_price")
        assert rc == 0
        assert out["status"] == "insufficient_data"
        assert out["required_length"] == 2
        assert out["available_length"] == 1

    def test_bad_threshold_exit_2(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _daily_payload([10.0, 11.0]),
                       "--indicator", "vol_price", "--threshold", "-0.5")
        assert rc == 2
        assert out["status"] == "error"

    def test_bad_threshold_not_masked_by_insufficient_data(self, monkeypatch, capsys):
        # 非数值 --threshold 由 argparse 以退出码 2 终止（同非法 --indicator）
        monkeypatch.setattr(sys, "stdin",
                            io.StringIO(json.dumps(_daily_payload([10.0]))))
        monkeypatch.setattr(sys, "argv", ["indicators.py", "--indicator",
                                          "vol_price", "--threshold", "abc"])
        with pytest.raises(SystemExit) as exc:
            indicators.main()
        assert exc.value.code == 2

    def test_malformed_stdin_exit_1(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, "not json", "--indicator", "vol_price")
        assert rc == 1


# ---------------------------------------------------------------- promote：晋级率

class TestPromote:
    def test_ok_contract(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _promote_payload(
            yesterday=[("600001", 2), ("600002", 3), ("600003", 1)],
            today=[("600001", 3), ("000001", 1)],
        ), "--indicator", "promote")
        assert rc == 0
        assert out["status"] == "ok"
        assert out["indicator"] == "promote"
        assert out["yesterday"] == {"trade_date": "2026-01-08", "count": 3}
        assert out["today"] == {"trade_date": "2026-01-09", "count": 2}
        assert out["promote_rate"] == pytest.approx(100 / 3, abs=1e-4)
        assert out["promoted"] == ["600001"]
        assert out["failed"] == ["600002", "600003"]
        assert out["ladder"] == {"1": 1, "3": 1}
        assert out["ladder_detail"] == {"1": ["000001"], "3": ["600001"]}

    def test_all_promoted(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _promote_payload(
            yesterday=[("600001", 1)],
            today=[("600001", 2), ("600002", 1), ("600003", 4)],
        ), "--indicator", "promote")
        assert rc == 0
        assert out["promote_rate"] == 100.0
        assert out["failed"] == []
        assert out["ladder"] == {"1": 1, "2": 1, "4": 1}

    def test_today_empty_rate_zero(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _promote_payload(
            yesterday=[("600001", 2)],
            today=[],
        ), "--indicator", "promote")
        assert rc == 0
        assert out["promote_rate"] == 0.0
        assert out["ladder"] == {}

    def test_yesterday_empty_insufficient_exit_0(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, _promote_payload(
            yesterday=[],
            today=[("600001", 1)],
        ), "--indicator", "promote")
        assert rc == 0
        assert out["status"] == "insufficient_data"

    def test_missing_leg_exit_1(self, monkeypatch, capsys):
        payload = _promote_payload([("600001", 1)], [])
        del payload["today"]
        rc, out = _run(monkeypatch, capsys, payload, "--indicator", "promote")
        assert rc == 1
        assert out["status"] == "error"

    def test_bad_entry_exit_1(self, monkeypatch, capsys):
        payload = _promote_payload([("600001", 1)], [])
        payload["today"] = {"trade_date": "2026-01-09",
                            "boards": [{"stock_code": "600002", "limit_count": 0}]}
        rc, out = _run(monkeypatch, capsys, payload, "--indicator", "promote")
        assert rc == 1

    def test_bad_entry_missing_code_exit_1(self, monkeypatch, capsys):
        payload = _promote_payload([("600001", 1)], [])
        payload["yesterday"]["boards"] = [{"limit_count": 2}]
        rc, out = _run(monkeypatch, capsys, payload, "--indicator", "promote")
        assert rc == 1

    def test_upstream_error_status_exit_1(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys,
                       {"status": "error", "error": "上游失败"},
                       "--indicator", "promote")
        assert rc == 1

    def test_malformed_stdin_exit_1(self, monkeypatch, capsys):
        rc, out = _run(monkeypatch, capsys, "not json", "--indicator", "promote")
        assert rc == 1
