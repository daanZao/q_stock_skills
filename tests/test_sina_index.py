"""新浪指数快照薄封装（issue #117 / spec #111）：批量报价解析 + 单位陷阱。

fixture 为 2026-10-23 收盘后实抓响应（tests/fixtures/sina/index_batch_quote.txt，
GBK 原始字节）。实测口径：沪系指数成交量为手（×100 入库），深系为股（透传），
同响应内口径不一致；量比无源置 None；涨跌额/涨跌幅/振幅由最新价与昨收推算；
行级日期取响应尾部 yyyy-mm-dd 字段。测试不触网。
"""

from pathlib import Path

import pytest

from qstock_mcp.adapters.base import FetchError
from qstock_mcp.adapters.sina_adapter import (
    SinaAdapter,
    parse_sina_index_quotes,
    to_sina_index_code,
)
from qstock_mcp.tools_init import MAJOR_INDICES

FIXTURES = Path(__file__).parent / "fixtures" / "sina"


def _rows() -> list[dict]:
    raw = (FIXTURES / "index_batch_quote.txt").read_bytes().decode("gbk")
    return parse_sina_index_quotes(raw)


def test_parse_fields_and_unit_trap():
    """沪深成交量口径不一致（实测陷阱）：沪手×100、深股透传；额元→万元。"""
    rows = _rows()
    assert [r["index_code"] for r in rows] == [c for c, _ in MAJOR_INDICES]
    by_code = {r["index_code"]: r for r in rows}
    sh = by_code["000001"]
    assert sh["index_name"] == "上证指数"
    assert sh["trade_date"] == "20261023"
    assert sh["latest_price"] == pytest.approx(3690.4753)
    assert sh["pre_close"] == pytest.approx(3658.0256)
    assert sh["volume"] == 55_284_291_200  # 552842912 手 ×100（沪系手）
    assert sh["amount"] == pytest.approx(89_866_687.4902)  # 元 → 万元
    assert sh["volume_ratio"] is None  # 新浪快照无量比（不伪造）
    assert sh["change_percent"] == pytest.approx(0.8871, abs=1e-3)
    assert sh["change_amount"] == pytest.approx(32.4497, abs=1e-3)
    assert sh["amplitude"] == pytest.approx(3.2019, abs=1e-3)
    # 深系股透传：数量级 10^10（与腾讯快照 690752095 手 = 6.9e10 股同量级）
    sz = by_code["399001"]
    assert sz["volume"] == 69_011_837_385
    assert sz["volume"] != 6_901_183_738_500  # 深系不再 ×100
    assert by_code["000688"]["change_percent"] == pytest.approx(3.0356, abs=1e-3)


def test_parse_skips_empty_and_malformed_statements():
    text = (
        'var hq_str_sh000001="";'
        "var hq_str_sz399001=garbage;"
        'var hq_str_sz399006="创业板指,1,1,1,1,1,0,0,100";'  # 字段数不足
    )
    assert parse_sina_index_quotes(text) == []


def test_fetch_indices_and_spot(monkeypatch):
    raw = (FIXTURES / "index_batch_quote.txt").read_bytes().decode("gbk")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "qstock_mcp.adapters.sina_adapter.fetch_sina_index_quotes",
        lambda codes: calls.append(tuple(codes)) or raw,
    )
    adapter = SinaAdapter()
    rows = adapter.fetch_indices("20261023")
    assert len(rows) == 7
    assert calls[0] == tuple(
        ("sz" if c.startswith("399") else "sh") + c for c, _ in MAJOR_INDICES
    )
    spot = adapter.fetch_index_spot("000688")
    assert [r["index_code"] for r in spot] == ["000688"]
    assert calls[1] == ("sh000688",)
    monkeypatch.setattr(
        "qstock_mcp.adapters.sina_adapter.fetch_sina_index_quotes", lambda codes: ""
    )
    assert adapter.fetch_index_spot("000688") == []


def test_fetch_indices_empty_raises_fetch_error(monkeypatch):
    monkeypatch.setattr(
        "qstock_mcp.adapters.sina_adapter.fetch_sina_index_quotes", lambda codes: ""
    )
    with pytest.raises(FetchError, match="为空"):
        SinaAdapter().fetch_indices("20261023")


def test_index_daily_not_in_chain():
    """新浪指数日线无成交额列，明确拒绝（不入指数日线链，链：腾讯→baostock）。"""
    with pytest.raises(FetchError) as ei:
        SinaAdapter().fetch_index_daily("000688", "20261013", "20261023")
    assert ei.value.kind == "unsupported"


def test_to_sina_index_code_routing():
    assert to_sina_index_code("000001") == "sh000001"
    assert to_sina_index_code("399006") == "sz399006"
    with pytest.raises(FetchError):
        to_sina_index_code("899001")
