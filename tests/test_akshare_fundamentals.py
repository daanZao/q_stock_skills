"""akshare 适配器基本面透传（issue #115）：协议解析用录制响应 fixture，零网络。

fixture 为 2026-10-21 真实拉取样本（tests/fixtures/baidu_valuation/）：
- 600519.json：百度股市通估值近一年五指标日序列（date/value 列）；
- sina_abstract_600519.json：新浪财务摘要节选（E 维度相关指标 × 最近 5 期）。

fake ak 模块按 fixture 返回 DataFrame 驱动 AkshareAdapter.fetch_fundamentals。
"""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from qstock_mcp.adapters import FetchError
from qstock_mcp.adapters import akshare_adapter
from qstock_mcp.adapters.akshare_adapter import (
    _BAIDU_VALUATION_INDICATORS,
    AkshareAdapter,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "baidu_valuation"


def _load(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _valuation_frame(records: list) -> pd.DataFrame:
    """fixture 记录 → DataFrame：date 还原为 date 对象（真实上游经
    akshare pd.to_datetime().dt.date），驱动 json_safe 日期序列化路径。"""
    return pd.DataFrame(
        [
            {"date": date.fromisoformat(r["date"]), "value": r["value"]}
            for r in records
        ]
    )


class FakeAk:
    """按 fixture 返回基本面两接口的 fake akshare 模块。

    fail_abstract: abstract 抓取抛异常；fail_indicators: 指定百度指标抛异常；
    empty_indicators: 指定百度指标返回空 DataFrame。
    """

    def __init__(self, *, fail_abstract=False, fail_indicators=(), empty_indicators=()):
        self._abstract = _load("sina_abstract_600519.json")
        self._valuation = _load("600519.json")
        self._fail_abstract = fail_abstract
        self._fail_indicators = set(fail_indicators)
        self._empty_indicators = set(empty_indicators)
        self.valuation_calls: list[str] = []

    def stock_financial_abstract(self, symbol):
        if self._fail_abstract:
            raise RuntimeError("sina boom")
        return pd.DataFrame(self._abstract)

    def stock_zh_valuation_baidu(self, symbol, indicator, period):
        self.valuation_calls.append(indicator)
        if indicator in self._fail_indicators:
            raise RuntimeError(f"baidu {indicator} boom")
        if indicator in self._empty_indicators:
            return pd.DataFrame(columns=["date", "value"])
        return _valuation_frame(self._valuation[indicator])


@pytest.fixture
def make_adapter(monkeypatch):
    def _make(**kwargs):
        fake = FakeAk(**kwargs)
        monkeypatch.setattr(akshare_adapter, "import_akshare", lambda: fake)
        return AkshareAdapter(), fake

    return _make


def test_happy_path_abstract_and_baidu_valuation(make_adapter):
    adapter, fake = make_adapter()
    payload = adapter.fetch_fundamentals("600519")

    # abstract 原样透传（键名/字段名不动，E 维度关键指标在场）
    abstract = payload["financial_abstract"]
    indicators = {r["指标"] for r in abstract}
    assert {"营业总收入", "基本每股收益", "毛利率", "扣非净利润", "净资产收益率(ROE)"} <= indicators

    # valuation：五指标日序列，date 序列化为 ISO 字符串、value 为数值
    valuation = payload["valuation"]
    assert set(valuation) == set(_BAIDU_VALUATION_INDICATORS)
    first = valuation["总市值"][0]
    assert first["date"] == "2025-10-21"
    assert isinstance(first["value"], (int, float))
    assert len(valuation["市盈率(TTM)"]) == 366
    assert fake.valuation_calls == list(_BAIDU_VALUATION_INDICATORS)
    assert "errors" not in payload


def test_single_baidu_indicator_failure_tolerated(make_adapter):
    adapter, _ = make_adapter(fail_indicators=("市净率",))
    payload = adapter.fetch_fundamentals("600519")
    assert set(payload["valuation"]) == set(_BAIDU_VALUATION_INDICATORS) - {"市净率"}
    assert any("valuation: 市净率" in e for e in payload["errors"])


def test_abstract_failure_keeps_valuation(make_adapter):
    adapter, fake = make_adapter(fail_abstract=True)
    payload = adapter.fetch_fundamentals("600519")
    assert "financial_abstract" not in payload
    assert set(payload["valuation"]) == set(_BAIDU_VALUATION_INDICATORS)
    assert any(e.startswith("financial_abstract") for e in payload["errors"])
    assert fake.valuation_calls  # abstract 失败不阻止估值抓取


def test_empty_baidu_indicator_recorded_as_section_error(make_adapter):
    adapter, _ = make_adapter(empty_indicators=("市现率",))
    payload = adapter.fetch_fundamentals("600519")
    assert "市现率" not in payload["valuation"]
    assert any("市现率: 空返回" in e for e in payload["errors"])


def test_all_baidu_indicators_fail_abstract_survives(make_adapter):
    adapter, _ = make_adapter(fail_indicators=_BAIDU_VALUATION_INDICATORS)
    payload = adapter.fetch_fundamentals("600519")
    assert "valuation" not in payload
    assert "financial_abstract" in payload
    assert len(payload["errors"]) == len(_BAIDU_VALUATION_INDICATORS)


def test_all_sections_no_data_raises_fetch_error(make_adapter):
    adapter, _ = make_adapter(
        fail_abstract=True, fail_indicators=_BAIDU_VALUATION_INDICATORS
    )
    with pytest.raises(FetchError) as ei:
        adapter.fetch_fundamentals("600519")
    assert "akshare 基本面无数据" in str(ei.value)
