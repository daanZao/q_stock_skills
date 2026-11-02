"""fallback 编排接缝测试：fake 适配器，不触网不触库。

契约（issue #3）：按链顺序 fallback（fake 名镜像生产个股日线链
腾讯→新浪→baostock）；每源最多重试 2 次（即每源最多尝试 3 次）；
空结果视为成功（该区间无交易日/停牌）；全失败时报 attempted_sources
（每个源的错误原因），绝不伪造数据。
"""

import pytest

from qstock_mcp.fetch_chain import AllSourcesFailed, fetch_with_fallback

from fakes import FakeAdapter

ROWS = [{"trade_date": "20240102", "close": 10.0}]


def _fake(name, **kwargs):
    kwargs.setdefault("rows", ROWS)
    return FakeAdapter(name, **kwargs)


def test_first_source_succeeds_later_sources_untouched():
    tx, sn, bs = _fake("tencent"), _fake("sina"), _fake("baostock")
    result = fetch_with_fallback([tx, sn, bs], "600519", "20240101", "20240131")
    assert result["source"] == "tencent"
    assert result["rows"] == ROWS
    assert result["attempted_sources"] == []
    assert sn.calls == [] and bs.calls == []


def test_fallback_order_first_to_second():
    tx = _fake("tencent", fail_times=99)
    sn, bs = _fake("sina"), _fake("baostock")
    result = fetch_with_fallback([tx, sn, bs], "600519", "20240101", "20240131")
    assert result["source"] == "sina"
    assert [a["source"] for a in result["attempted_sources"]] == ["tencent"]
    assert bs.calls == []


def test_each_source_retried_at_most_twice():
    tx = _fake("tencent", fail_times=2)  # 第 3 次尝试成功
    result = fetch_with_fallback([tx], "600519", "20240101", "20240131")
    assert result["source"] == "tencent"
    assert len(tx.calls) == 3


def test_source_exhausted_after_two_retries():
    tx = _fake("tencent", fail_times=3)  # 3 次尝试全败 → 放弃该源
    sn = _fake("sina")
    result = fetch_with_fallback([tx, sn], "600519", "20240101", "20240131")
    assert len(tx.calls) == 3
    assert result["source"] == "sina"


def test_all_sources_failed_reports_attempted_sources():
    adapters = [_fake(n, fail_times=99) for n in ("tencent", "sina", "baostock")]
    with pytest.raises(AllSourcesFailed) as exc_info:
        fetch_with_fallback(adapters, "600519", "20240101", "20240131")
    attempted = exc_info.value.attempted
    assert [a["source"] for a in attempted] == ["tencent", "sina", "baostock"]
    assert all(a["attempts"] == 3 and a["error"] for a in attempted)


def test_empty_rows_is_success_not_fallback():
    tx = _fake("tencent", rows=[])  # 区间无交易日/停牌：空结果不算失败
    sn = _fake("sina")
    result = fetch_with_fallback([tx, sn], "600519", "20240101", "20240131")
    assert result["source"] == "tencent"
    assert result["rows"] == []
    assert sn.calls == []


def test_retry_backoff_sleeps_increasing_with_jitter(monkeypatch):
    """同源重试间退避（issue #87）：0.5s/1s 基数 ±25% 抖动（区间互不重叠）。"""
    sleeps = []
    monkeypatch.setattr("qstock_mcp.fetch_chain.sleep", sleeps.append)
    tx = _fake("tencent", fail_times=3)  # 3 次尝试全败 → 2 次退避
    sn = _fake("sina")
    result = fetch_with_fallback([tx, sn], "600519", "20240101", "20240131")
    assert result["source"] == "sina"
    assert len(sleeps) == 2
    assert 0.375 <= sleeps[0] <= 0.625
    assert 0.75 <= sleeps[1] <= 1.25


def test_unsupported_source_not_retried():
    """by-design 不支持的源单次即弃（issue #87），记录 attempts=1 并正常 fallback。"""
    from qstock_mcp.adapters import FetchError

    tx = _fake("tencent", fail_times=99,
               exc=FetchError("tencent 不支持该能力", kind="unsupported"))
    sn = _fake("sina")
    result = fetch_with_fallback([tx, sn], "600519", "20240101", "20240131")
    assert result["source"] == "sina"
    assert len(tx.calls) == 1
    assert result["attempted_sources"][0]["attempts"] == 1
    assert result["attempted_sources"][0]["failure_kind"] == "unsupported"
