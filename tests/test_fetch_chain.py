"""fallback 编排接缝测试：fake 适配器，不触网不触库。

契约（issue #3）：efinance → akshare → baostock 顺序 fallback；每源最多重试 2 次
（即每源最多尝试 3 次）；空结果视为成功（该区间无交易日/停牌）；全失败时报
attempted_sources（每个源的错误原因），绝不伪造数据。
"""

import pytest

from qstock_mcp.fetch_chain import AllSourcesFailed, fetch_with_fallback

from fakes import FakeAdapter

ROWS = [{"trade_date": "20240102", "close": 10.0}]


def _fake(name, **kwargs):
    kwargs.setdefault("rows", ROWS)
    return FakeAdapter(name, **kwargs)


def test_first_source_succeeds_later_sources_untouched():
    ef, ak, bs = _fake("efinance"), _fake("akshare"), _fake("baostock")
    result = fetch_with_fallback([ef, ak, bs], "600519", "20240101", "20240131")
    assert result["source"] == "efinance"
    assert result["rows"] == ROWS
    assert result["attempted_sources"] == []
    assert ak.calls == [] and bs.calls == []


def test_fallback_order_efinance_then_akshare():
    ef = _fake("efinance", fail_times=99)
    ak, bs = _fake("akshare"), _fake("baostock")
    result = fetch_with_fallback([ef, ak, bs], "600519", "20240101", "20240131")
    assert result["source"] == "akshare"
    assert [a["source"] for a in result["attempted_sources"]] == ["efinance"]
    assert bs.calls == []


def test_each_source_retried_at_most_twice():
    ef = _fake("efinance", fail_times=2)  # 第 3 次尝试成功
    result = fetch_with_fallback([ef], "600519", "20240101", "20240131")
    assert result["source"] == "efinance"
    assert len(ef.calls) == 3


def test_source_exhausted_after_two_retries():
    ef = _fake("efinance", fail_times=3)  # 3 次尝试全败 → 放弃该源
    ak = _fake("akshare")
    result = fetch_with_fallback([ef, ak], "600519", "20240101", "20240131")
    assert len(ef.calls) == 3
    assert result["source"] == "akshare"


def test_all_sources_failed_reports_attempted_sources():
    adapters = [_fake(n, fail_times=99) for n in ("efinance", "akshare", "baostock")]
    with pytest.raises(AllSourcesFailed) as exc_info:
        fetch_with_fallback(adapters, "600519", "20240101", "20240131")
    attempted = exc_info.value.attempted
    assert [a["source"] for a in attempted] == ["efinance", "akshare", "baostock"]
    assert all(a["attempts"] == 3 and a["error"] for a in attempted)


def test_empty_rows_is_success_not_fallback():
    ef = _fake("efinance", rows=[])  # 区间无交易日/停牌：空结果不算失败
    ak = _fake("akshare")
    result = fetch_with_fallback([ef, ak], "600519", "20240101", "20240131")
    assert result["source"] == "efinance"
    assert result["rows"] == []
    assert ak.calls == []
