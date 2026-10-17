"""停牌行剔除测试（spec #71 / #81，TDD：先于实现）。

停牌占位行（query_daily 输出的 suspended=true / tradestatus=0 行，或无标注的
OHLC 全等且 volume/change_percent 为 null 的行）按"无该交易日数据"剔除，
不填充不伪造；剔除后必需列校验继续严格（非停牌行的 null 仍报错）。
剔除数量以 excluded_suspended 字段记入响应。
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


def _bar(i, cp=1.0):
    return {
        "trade_date": _date(i),
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        "volume": 1000.0, "amount": 100.0,
        "change_percent": cp,
    }


def _suspended_annotated(i):
    """query_daily 停牌标注行（tradestatus=0 权威口径）。"""
    return {
        "trade_date": _date(i),
        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
        "volume": None, "amount": None,
        "change_percent": None,
        "tradestatus": 0, "suspended": True, "suspended_basis": "tradestatus",
    }


def _suspended_heuristic(i):
    """无标注的停牌占位行：OHLC 全等且 volume/change_percent 为 null。"""
    return {
        "trade_date": _date(i),
        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
        "volume": None, "amount": None,
        "change_percent": None,
    }


def _deviation_payload(stock_rows, bench_cps, stock_code="002813"):
    return {
        "status": "ok",
        "stock": {"status": "ok", "params": {"stock_code": stock_code},
                  "rows": stock_rows},
        "benchmark": {"status": "ok", "params": {"index_code": "399001"},
                      "rows": [_bar(i, cp) for i, cp in enumerate(bench_cps)]},
    }


class TestDeviationSuspendedRows:
    def test_annotated_rows_excluded_endpoint_matches(self, monkeypatch, capsys):
        """个股序列混入停牌标注行：不再 exit 1，dev10/dev30 与仅按有效交易日对齐一致。"""
        # 40 根有效日线，第 5、17 日为停牌占位行；有效日恒比基准多涨 1%
        stock_rows = []
        for i in range(42):
            if i in (5, 17):
                stock_rows.append(_suspended_annotated(i))
            else:
                stock_rows.append(_bar(i, 1.0))
        bench_cps = [0.0] * 42
        rc, out = _run(monkeypatch, capsys,
                       _deviation_payload(stock_rows, bench_cps),
                       "--indicator", "deviation", "--tail", "0")
        assert rc == 0
        assert out["status"] == "ok"
        assert out["aligned_length"] == 40
        assert out["excluded_suspended"]["stock"] == {
            "count": 2, "dates": [_date(5), _date(17)]}
        last = out["series"][-1]
        assert last["trade_date"] == _date(41)
        assert last["dev10"] == 10.0
        assert last["dev30"] == 30.0

    def test_heuristic_rows_excluded(self, monkeypatch, capsys):
        """无标注停牌占位行（OHLC 全等 + volume/change_percent 为 null）同样剔除。"""
        stock_rows = [_suspended_heuristic(3) if i == 3 else _bar(i, 1.0)
                      for i in range(15)]
        rc, out = _run(monkeypatch, capsys,
                       _deviation_payload(stock_rows, [0.0] * 15),
                       "--indicator", "deviation", "--tail", "0")
        assert rc == 0
        assert out["status"] == "ok"
        assert out["aligned_length"] == 14
        assert out["excluded_suspended"]["stock"] == {"count": 1, "dates": [_date(3)]}
        assert out["series"][-1]["dev10"] == 10.0

    def test_tradestatus_zero_without_suspended_flag(self, monkeypatch, capsys):
        """只有 tradestatus=0 标注（无 suspended 字段）也按停牌剔除。"""
        row = _suspended_heuristic(7)
        row["tradestatus"] = 0
        stock_rows = [row if i == 7 else _bar(i, 1.0) for i in range(12)]
        rc, out = _run(monkeypatch, capsys,
                       _deviation_payload(stock_rows, [0.0] * 12),
                       "--indicator", "deviation")
        assert rc == 0
        assert out["status"] == "ok"
        assert out["aligned_length"] == 11

    def test_explicit_not_suspended_null_volume_still_error(self, monkeypatch, capsys):
        """标注 suspended=false 但 volume 为 null：非停牌行，必需列校验仍严格。"""
        row = _bar(4, 1.0)
        row["volume"] = None
        row["suspended"] = False
        stock_rows = [row if i == 4 else _bar(i, 1.0) for i in range(15)]
        rc, out = _run(monkeypatch, capsys,
                       _deviation_payload(stock_rows, [0.0] * 15),
                       "--indicator", "deviation")
        assert rc == 1
        assert out["status"] == "error"


class TestSingleSymbolSuspendedRows:
    def _payload(self, rows, code="002860"):
        return {"status": "ok", "params": {"stock_code": code}, "rows": rows}

    def test_ma_excludes_suspended(self, monkeypatch, capsys):
        """ma：停牌行剔除后参与窗口计算，available_length 为剔除后长度。"""
        rows = []
        for i in range(12):
            if i == 6:
                rows.append(_suspended_annotated(i))
            else:
                r = _bar(i, 1.0)
                r["close"] = float(100 + i)
                rows.append(r)
        rc, out = _run(monkeypatch, capsys, self._payload(rows),
                       "--indicator", "ma", "--periods", "5", "--tail", "0")
        assert rc == 0
        assert out["status"] == "ok"
        assert out["available_length"] == 11
        assert out["excluded_suspended"] == {"count": 1, "dates": [_date(6)]}
        # 手工对照：剔除后 close 序列 [100..105, 107..111]，ma5 末点 = mean(107..111)
        assert out["series"][-1]["ma5"] == 109.0
        assert len(out["series"]) == 11

    def test_no_suspended_no_field(self, monkeypatch, capsys):
        """无停牌行时不出现 excluded_suspended 字段（输出结构不变）。"""
        rows = [_bar(i, 1.0) for i in range(10)]
        rc, out = _run(monkeypatch, capsys, self._payload(rows),
                       "--indicator", "ma", "--periods", "5")
        assert rc == 0
        assert "excluded_suspended" not in out

    def test_vol_price_strict_on_non_suspended_null(self, monkeypatch, capsys):
        """vol_price：非停牌行 amount 为 null 仍 exit 1（全有全无校验不放松）。"""
        rows = [_bar(i, 1.0) for i in range(5)]
        rows[2]["amount"] = None
        rc, out = _run(monkeypatch, capsys, self._payload(rows),
                       "--indicator", "vol_price")
        assert rc == 1
        assert out["status"] == "error"

    def test_insufficient_after_exclusion(self, monkeypatch, capsys):
        """剔除停牌行后不足最小长度：insufficient_data，available_length 为剔除后值。"""
        rows = [_bar(i, 1.0) for i in range(4)]
        rows.insert(1, _suspended_annotated(100))
        rc, out = _run(monkeypatch, capsys, self._payload(rows),
                       "--indicator", "ma", "--periods", "5")
        assert rc == 0
        assert out["status"] == "insufficient_data"
        assert out["available_length"] == 4
        assert out["excluded_suspended"] == {"count": 1, "dates": [_date(100)]}


class TestCorrSuspendedRows:
    def _payload(self, series):
        return {"status": "ok", "series": series}

    def test_corr_excludes_suspended(self, monkeypatch, capsys):
        """corr：某标的含停牌行，剔除后按交集对齐正常出矩阵。"""
        rows_a = []
        for i in range(10):
            if i == 4:
                rows_a.append(_suspended_annotated(i))
            else:
                r = _bar(i, float(i))
                rows_a.append(r)
        rows_b = [_bar(i, float(i) * 2) for i in range(10)]
        payload = self._payload({
            "000001": {"status": "ok", "rows": rows_a},
            "000002": {"status": "ok", "rows": rows_b},
        })
        rc, out = _run(monkeypatch, capsys, payload,
                       "--indicator", "corr", "--on", "change_percent")
        assert rc == 0
        assert out["status"] == "ok"
        assert out["aligned_length"] == 9
        assert out["excluded_suspended"]["000001"] == {"count": 1, "dates": [_date(4)]}
        assert "000002" not in out.get("excluded_suspended", {})
        assert out["matrix"]["000001"]["000002"] == 1.0

    def test_corr_strict_on_non_suspended_null(self, monkeypatch, capsys):
        """corr：非停牌行必需列为 null 仍 exit 1。"""
        rows_a = [_bar(i, float(i)) for i in range(5)]
        rows_a[2]["close"] = None
        payload = self._payload({
            "000001": {"status": "ok", "rows": rows_a},
            "000002": {"status": "ok", "rows": [_bar(i, float(i)) for i in range(5)]},
        })
        rc, out = _run(monkeypatch, capsys, payload, "--indicator", "corr")
        assert rc == 1
        assert out["status"] == "error"
