"""全市场快照三源对拍（issue #116 / spec #111）：逐源直取、字段级比较、不落库。

覆盖：参考源优先级（有数据的最高优先级源）、容差判定、结构性缺项跳过
（tencent_rank 缺 OHLC/pre_close；sina 缺振幅/量比）、单源失败不中断、
全源失败 error、codes_limit 截断。fixture 级解析在 test_snapshot_adapters.py。
"""

from qstock_mcp.adapters.base import FetchError
from qstock_mcp.snapshot_parity import compare_snapshot_sources


def _row(code, latest=10.0, **kw):
    row = {
        "stock_code": code,
        "stock_name": f"股{code}",
        "latest_price": latest,
        "pre_close": 9.9,
        "open": 9.9,
        "high": 10.2,
        "low": 9.8,
        "change_percent": 1.01,
        "change_amount": 0.1,
        "amplitude": 4.04,
        "volume_ratio": 1.1,
        "turnover_rate": 2.0,
        "pe_ratio": 20.0,
        "pb_ratio": 2.0,
        "volume": 1_000_000,
        "amount": 5000.0,
        "market_cap": 1.0e6,
        "float_cap": 8.0e5,
    }
    row.update(kw)
    return row


class _Fake:
    def __init__(self, name, result=None, exc=None):
        self.name = name
        self._result = result
        self._exc = exc

    def fetch_market_snapshot(self):
        if self._exc is not None:
            raise self._exc
        return self._result


def test_all_sources_agree_status_ok():
    rows = [_row("600519"), _row("000001")]
    out = compare_snapshot_sources(
        adapters=[
            _Fake("tencent", {"trade_date": "20261021", "rows": rows}),
            _Fake("tencent_rank", {"trade_date": None, "rows": [dict(r) for r in rows]}),
            _Fake("sina", {"trade_date": None, "rows": [dict(r) for r in rows]}),
        ]
    )
    assert out["status"] == "ok"
    assert out["reference_source"] == "tencent"
    assert out["sources"]["tencent"]["trade_date"] == "20261021"
    assert out["sources"]["tencent_rank"]["trade_date"] is None
    assert out["common_codes"] == {"tencent_rank": 2, "sina": 2}
    assert out["diff_total"] == 0
    # 结构性缺项如实标注（rank 缺 OHLC、新浪缺振幅/量比）
    assert set(out["structural_gaps"]["tencent_rank"]) == {
        "open", "high", "low", "pre_close",
    }
    assert set(out["structural_gaps"]["sina"]) == {"amplitude", "volume_ratio"}


def test_diff_beyond_tolerance_is_mismatch():
    out = compare_snapshot_sources(
        adapters=[
            _Fake("tencent", {"trade_date": "20261021", "rows": [_row("600519", amount=5000.0)]}),
            _Fake("sina", {"trade_date": None, "rows": [_row("600519", amount=5100.0)]}),
        ]
    )
    assert out["status"] == "mismatch"
    assert out["field_diff_counts"]["sina"]["amount"] == 1
    assert out["diff_total"] == 1
    (sample,) = out["diff_samples"]
    assert sample["stock_code"] == "600519" and sample["field"] == "amount"
    assert sample["reference"] == {"source": "tencent", "value": 5000.0}
    assert sample["other"] == {"source": "sina", "value": 5100.0}
    assert "口径" in out["note"]


def test_structural_gap_fields_not_compared():
    """rank 源即便带了 OHLC 值也不参与比对（by-design 缺项，非数据差异）。"""
    out = compare_snapshot_sources(
        adapters=[
            _Fake("tencent", {"trade_date": "20261021", "rows": [_row("600519", open=9.9)]}),
            _Fake("tencent_rank", {"trade_date": None, "rows": [_row("600519", open=99.9)]}),
        ]
    )
    assert out["status"] == "ok"
    assert out["compared_values"]["tencent_rank"] > 0  # 其余字段照常比对


def test_single_source_failure_does_not_abort_others():
    conn_exc = FetchError("tencent 连接重置")
    conn_exc.__cause__ = ConnectionResetError("reset")
    out = compare_snapshot_sources(
        adapters=[
            _Fake("tencent", exc=conn_exc),
            _Fake("tencent_rank", {"trade_date": None, "rows": [_row("600519")]}),
            _Fake("sina", {"trade_date": None, "rows": [_row("600519")]}),
        ]
    )
    assert out["status"] == "ok"  # 剩余两源一致
    assert out["reference_source"] == "tencent_rank"
    assert out["sources"]["tencent"]["failure_kind"] == "connection"


def test_all_sources_failed_is_error():
    out = compare_snapshot_sources(
        adapters=[_Fake(n, exc=FetchError(f"{n} boom")) for n in ("tencent", "tencent_rank", "sina")]
    )
    assert out["status"] == "error"
    assert "全部快照源无数据或失败" in out["error"]


def test_codes_limit_truncates_rows():
    rows = [_row("600519"), _row("000001"), _row("920001")]
    out = compare_snapshot_sources(
        adapters=[_Fake("tencent", {"trade_date": "20261021", "rows": list(rows)})],
        codes_limit=2,
    )
    assert out["status"] == "ok"  # 单源无对比对象
    assert out["sources"]["tencent"]["rows"] == 2


def test_bad_codes_limit_is_param_error():
    out = compare_snapshot_sources(codes_limit=0)
    assert out["status"] == "error"
    assert "codes_limit" in out["error"]


def test_empty_result_source_is_no_data():
    out = compare_snapshot_sources(
        adapters=[
            _Fake("tencent", {"trade_date": "20261021", "rows": [_row("600519")]}),
            _Fake("tencent_rank", {"trade_date": None, "rows": []}),
        ]
    )
    assert out["status"] == "ok"
    assert out["sources"]["tencent_rank"]["status"] == "no_data"
    assert "common_codes" not in out or "tencent_rank" not in out["common_codes"]


def test_none_values_in_either_side_skip_comparison():
    """单源某字段缺失（停牌等）不比、不计差异。"""
    out = compare_snapshot_sources(
        adapters=[
            _Fake("tencent", {"trade_date": "20261021", "rows": [_row("600519", pe_ratio=None)]}),
            _Fake("sina", {"trade_date": None, "rows": [_row("600519", pe_ratio=20.0)]}),
        ]
    )
    assert out["status"] == "ok"
    assert out["field_diff_counts"]["sina"]["pe_ratio"] == 0


def test_limit_param_type_guard():
    for bad in ("10", 0, -1, True):
        out = compare_snapshot_sources(codes_limit=bad)
        assert out["status"] == "error", bad
