"""指数快照两源对拍（issue #122，spec #111 总验收）：逐源直取、字段级比较、不落库。

覆盖：参考源优先级（腾讯为链首主承载）、容差判定、结构性缺项跳过
（sina 缺 volume_ratio）、单源失败不中断、全源失败 error、代码子集、
行级 trade_date 同一交易日判定。不触真实网络（本地 fake）。
"""

from qstock_mcp.adapters.base import FetchError
from qstock_mcp.index_parity import compare_index_spot_sources


def _row(code, name="上证指数", latest=3000.0, **kw):
    row = {
        "index_code": code,
        "index_name": name,
        "latest_price": latest,
        "change_percent": 0.5,
        "change_amount": 15.0,
        "volume": 4.0e8,
        "amount": 4.0e7,  # 万元
        "amplitude": 1.0,
        "high": 3020.0,
        "low": 2980.0,
        "open": 2990.0,
        "pre_close": 2985.0,
        "volume_ratio": 1.1,
        "trade_date": "20261023",
    }
    row.update(kw)
    return row


class _Fake:
    """单 code 指数快照 fake：spot 为 {code: rows} 脚本，exc 时全部抛错。"""

    def __init__(self, name, spot=None, exc=None):
        self.name = name
        self._spot = spot or {}
        self._exc = exc

    def fetch_index_spot(self, index_code):
        if self._exc is not None:
            raise self._exc
        return self._spot.get(index_code, [])


def test_two_sources_agree_status_ok():
    rows = [_row("000001"), _row("399006")]
    spot = {r["index_code"]: [r] for r in rows}
    out = compare_index_spot_sources(
        codes=["000001", "399006"],
        adapters=[_Fake("tencent", spot), _Fake("sina", spot)],
    )
    assert out["status"] == "ok"
    assert out["reference_source"] == "tencent"
    assert out["same_day"] is True
    assert out["common_indices"] == {"sina": 2}
    assert out["diff_total"] == 0
    assert set(out["structural_gaps"]["sina"]) == {"volume_ratio"}


def test_diff_beyond_tolerance_is_mismatch():
    ref = [_row("000001", amount=4.0e7)]
    other = [_row("000001", amount=4.5e7)]  # rel 差 12.5% > 0.005
    out = compare_index_spot_sources(
        codes=["000001"],
        adapters=[
            _Fake("tencent", {r["index_code"]: [r] for r in ref}),
            _Fake("sina", {r["index_code"]: [r] for r in other}),
        ],
    )
    assert out["status"] == "mismatch"
    assert out["field_diff_counts"]["sina"]["amount"] == 1
    assert out["diff_total"] == 1
    (sample,) = out["diff_samples"]
    assert sample["index_code"] == "000001" and sample["field"] == "amount"
    assert sample["reference"] == {"source": "tencent", "value": 4.0e7}
    assert sample["other"] == {"source": "sina", "value": 4.5e7}


def test_structural_gap_field_not_compared():
    """sina 即便带了 volume_ratio 值也不参与比对（by-design 缺项）。"""
    ref = [_row("000001", volume_ratio=1.1)]
    other = [_row("000001", volume_ratio=99.9)]
    out = compare_index_spot_sources(
        codes=["000001"],
        adapters=[
            _Fake("tencent", {r["index_code"]: [r] for r in ref}),
            _Fake("sina", {r["index_code"]: [r] for r in other}),
        ],
    )
    assert out["status"] == "ok"
    assert out["field_diff_counts"]["sina"]["volume_ratio"] == 0
    assert out["compared_values"]["sina"] > 0  # 其余字段照常比对


def test_single_source_failure_falls_back_to_next():
    conn_exc = FetchError("tencent 连接重置")
    conn_exc.__cause__ = ConnectionResetError("reset")
    rows = [_row("000001")]
    out = compare_index_spot_sources(
        codes=["000001"],
        adapters=[
            _Fake("tencent", exc=conn_exc),
            _Fake("sina", {r["index_code"]: [r] for r in rows}),
        ],
    )
    assert out["status"] == "ok"  # 单源无对比对象
    assert out["reference_source"] == "sina"
    assert out["sources"]["tencent"]["status"] == "error"
    assert out["sources"]["tencent"]["failure_kind"] == "connection"


def test_all_sources_failed_is_error():
    out = compare_index_spot_sources(
        codes=["000001"],
        adapters=[_Fake(n, exc=FetchError(f"{n} boom")) for n in ("tencent", "sina")],
    )
    assert out["status"] == "error"
    assert "全部指数快照源无数据或失败" in out["error"]


def test_codes_accept_csv_string_and_default_major_indices():
    rows = [_row("000001")]
    adapters = [_Fake("tencent", {r["index_code"]: [r] for r in rows})]
    out = compare_index_spot_sources(codes="000001, 000001", adapters=adapters)
    assert out["status"] == "ok"
    assert out["params"]["codes"] == "000001, 000001"
    out_default = compare_index_spot_sources(
        adapters=[_Fake("tencent", {rows[0]["index_code"]: rows}), _Fake("sina", {})]
    )
    # 缺省 7 大指数：无行情代码只记 no_data，不影响整体
    assert out_default["status"] == "ok"
    assert out_default["sources"]["sina"]["status"] == "no_data"


def test_trade_date_mismatch_flags_same_day_false():
    ref = [_row("000001", trade_date="20261023")]
    other = [_row("000001", trade_date="20261022")]
    out = compare_index_spot_sources(
        codes=["000001"],
        adapters=[
            _Fake("tencent", {r["index_code"]: [r] for r in ref}),
            _Fake("sina", {r["index_code"]: [r] for r in other}),
        ],
    )
    assert out["same_day"] is False
    assert out["sources"]["tencent"]["trade_dates"] == ["20261023"]
    assert out["sources"]["sina"]["trade_dates"] == ["20261022"]


def test_none_value_in_either_side_skips_comparison():
    ref = [_row("000001", amount=None)]
    other = [_row("000001", amount=4.0e7)]
    out = compare_index_spot_sources(
        codes=["000001"],
        adapters=[
            _Fake("tencent", {r["index_code"]: [r] for r in ref}),
            _Fake("sina", {r["index_code"]: [r] for r in other}),
        ],
    )
    assert out["status"] == "ok"
    assert out["field_diff_counts"]["sina"]["amount"] == 0
