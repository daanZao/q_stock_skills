"""个股日线链路重排与三源对拍（issue #112 / spec #111）。

契约：个股日线 fallback 序 腾讯 → 新浪 → baostock，东财日线退出；对拍逐源
直取、字段级比较、不落库。测试用 fake 适配器，不触网。
"""

import pytest

from fakes import FakeAdapter
from qstock_mcp.adapters import FetchError, default_daily_adapters
from qstock_mcp.adapters.akshare_adapter import AkshareAdapter
from qstock_mcp.fetch_chain import fetch_with_fallback
from qstock_mcp.tools_daily import compare_daily_sources

TRADE_DATE = "20261020"

BAR = {
    "trade_date": TRADE_DATE,
    "open": 1321.01,
    "high": 1335.00,
    "low": 1316.14,
    "close": 1331.88,
    "volume": 3_413_600,
    "amount": 452539.90,
    "amplitude": 1.4234,
    "change_percent": 0.5215,
    "change_amount": 6.91,
    "turnover_rate": 0.27,
}


def _source(name, bar=None, **kwargs):
    return FakeAdapter(name, rows=[dict(bar or BAR)], **kwargs)


# ---------------------------------------------------------------- 链路顺序


def test_default_daily_chain_order():
    """腾讯 → 新浪 → baostock；东财链路不在个股日线链中。"""
    assert [a.name for a in default_daily_adapters()] == [
        "tencent", "sina", "baostock"
    ]


def test_akshare_eastmoney_daily_retired():
    """akshare 东财日线（stock_zh_a_hist）退役：显式拒绝而不是静默走错源。"""
    with pytest.raises(FetchError, match="已退役") as ei:
        AkshareAdapter().fetch_daily("600519", "20261001", "20261020")
    assert ei.value.kind == "unsupported"


def test_chain_falls_through_tencent_to_sina():
    tencent = FakeAdapter("tencent", fail_times=99)
    sina = _source("sina")
    bs = _source("baostock")
    result = fetch_with_fallback([tencent, sina, bs], "600519", "20261020", "20261020")
    assert result["source"] == "sina"
    assert [a["source"] for a in result["attempted_sources"]] == ["tencent"]


# ---------------------------------------------------------------- 三源对拍


def test_compare_ok():
    result = compare_daily_sources(
        ["600519"], TRADE_DATE,
        adapters=[_source("tencent"), _source("sina"), _source("baostock")],
    )
    assert result["status"] == "ok"
    assert result["mismatch_count"] == 0
    assert result["results"][0]["status"] == "ok"
    assert set(result["results"][0]["sources"]) == {"tencent", "sina", "baostock"}


def test_compare_tolerates_lot_rounding_and_unit_conversion():
    """腾讯 手→股 按整数手四舍五入（<100 股）、新浪 amount 元→万元，均不误报。"""
    sina = dict(BAR, volume=3_413_565, amount=452539.8958, turnover_rate=0.2731,
                change_percent=0.5214)
    result = compare_daily_sources(
        ["600519"], TRADE_DATE,
        adapters=[_source("tencent"), _source("sina", sina)],
    )
    assert result["status"] == "ok"
    assert result["results"][0]["diffs"] == []


def test_compare_mismatch_reports_field():
    sina = dict(BAR, close=1340.00)
    result = compare_daily_sources(
        ["600519"], TRADE_DATE,
        adapters=[_source("tencent"), _source("sina", sina)],
    )
    assert result["status"] == "mismatch"
    assert result["mismatch_count"] == 1
    (diff,) = [d for d in result["results"][0]["diffs"] if d["field"] == "close"]
    assert diff["reference"] == {"source": "tencent", "value": 1331.88}
    assert diff["other"] == {"source": "sina", "value": 1340.00}


def test_compare_single_source_is_warning_not_failure():
    """北交所：baostock by-design unsupported、新浪无当日行 → 单源警告。"""
    result = compare_daily_sources(
        ["920001"], TRADE_DATE,
        adapters=[
            _source("tencent"),
            FakeAdapter("sina", rows=[]),
            FakeAdapter("baostock", fail_times=99,
                        exc=FetchError("北交所不支持", kind="unsupported")),
        ],
    )
    assert result["status"] == "ok"
    assert result["single_source_count"] == 1
    assert result["results"][0]["status"] == "single_source"
    assert result["results"][0]["sources"]["baostock"]["status"] == "unsupported"


def test_compare_no_data_is_error():
    result = compare_daily_sources(
        ["920999"], TRADE_DATE,
        adapters=[
            FakeAdapter("tencent", rows=[]),
            FakeAdapter("sina", rows=[]),
            FakeAdapter("baostock", fail_times=99,
                        exc=FetchError("北交所不支持", kind="unsupported")),
        ],
    )
    assert result["status"] == "error"
    assert result["no_data_count"] == 1


def test_compare_reference_source_skips_failed_primary():
    tencent = FakeAdapter("tencent", fail_times=99)
    result = compare_daily_sources(
        ["600519"], TRADE_DATE,
        adapters=[tencent, _source("sina"), _source("baostock")],
    )
    assert result["status"] == "ok"
    bar = result["results"][0]["sources"]["sina"]["bar"]
    assert bar["close"] == BAR["close"]


def test_compare_rejects_bad_input():
    assert compare_daily_sources([], TRADE_DATE)["status"] == "error"
    assert compare_daily_sources(["600519"], "2026-13-40")["status"] == "error"
    assert compare_daily_sources(
        ["600519"], TRADE_DATE, adj="xx"
    )["status"] == "error"
