"""新浪个股日线适配器（issue #112）：字段映射与单位口径。

数据为 2026-10-21 实调 akshare stock_zh_a_daily 的录制值；映射测试不触网。
"""

from datetime import date

import pytest

from qstock_mcp.adapters.base import FetchError
from qstock_mcp.adapters.sina_adapter import SinaAdapter, map_sina_rows

SINA_RECORDS = [
    {
        "date": date(2026, 10, 19),
        "open": 1322.50,
        "high": 1348.60,
        "low": 1321.00,
        "close": 1324.97,
        "volume": 3972894.0,
        "amount": 5308935918.98,
        "outstanding_share": 1.250082e9,
        "turnover": 0.003178,
    },
    {
        "date": date(2026, 10, 20),
        "open": 1321.01,
        "high": 1335.00,
        "low": 1316.14,
        "close": 1331.88,
        "volume": 3413565.0,
        "amount": 4525398958.59,
        "outstanding_share": 1.250082e9,
        "turnover": 0.002731,
    },
]


def test_sina_adapter_requests_extra_days_and_filters(monkeypatch):
    stub = _StubAk(_StubFrame(SINA_RECORDS))
    monkeypatch.setattr(
        "qstock_mcp.adapters.sina_adapter.import_akshare", lambda: stub
    )
    bars = SinaAdapter().fetch_daily("600519", "20261020", "20261020")
    assert [b["trade_date"] for b in bars] == ["20261020"]
    call = stub.calls[0]
    assert call["symbol"] == "sh600519"
    assert call["adjust"] == "qfq"
    assert call["start_date"] == "20261013"  # start 前推 7 个自然日取前收
    assert call["end_date"] == "20261020"


def test_sina_adapter_normalizes_bse_old_code(monkeypatch):
    stub = _StubAk(_StubFrame(SINA_RECORDS))
    monkeypatch.setattr(
        "qstock_mcp.adapters.sina_adapter.import_akshare", lambda: stub
    )
    SinaAdapter().fetch_daily("430047", "20261020", "20261020")
    assert stub.calls[0]["symbol"] == "bj920047"


def test_map_sina_rows_units_and_derived_fields():
    first, last = map_sina_rows(SINA_RECORDS)
    assert first["trade_date"] == "20261019"
    assert first["volume"] == 3972894  # 原生股（ADR 0008）
    assert first["amount"] == pytest.approx(530893.591898)  # 元 → 万元（ADR 0006）
    assert first["turnover_rate"] == pytest.approx(0.3178)  # fraction → 百分数
    assert first["change_percent"] is None  # 序列首行无前收，不伪造
    assert last["change_amount"] == pytest.approx(6.91)
    assert last["change_percent"] == pytest.approx(0.5215, abs=1e-3)
    assert last["amplitude"] == pytest.approx(1.4234, abs=1e-3)


class _StubFrame:
    empty = False

    def __init__(self, records):
        self._records = records

    def to_dict(self, orient):
        assert orient == "records"
        return self._records


class _StubAk:
    def __init__(self, frame=None, exc=None):
        self.frame = frame
        self.exc = exc
        self.calls = []

    def stock_zh_a_daily(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.frame


def test_sina_adapter_empty_and_errors(monkeypatch):
    empty = _StubAk(_StubFrame([]))
    empty.frame.empty = True
    monkeypatch.setattr(
        "qstock_mcp.adapters.sina_adapter.import_akshare", lambda: empty
    )
    assert SinaAdapter().fetch_daily("600519", "20261020", "20261020") == []

    boom = _StubAk(exc=RuntimeError("ip banned"))
    monkeypatch.setattr(
        "qstock_mcp.adapters.sina_adapter.import_akshare", lambda: boom
    )
    with pytest.raises(FetchError, match="新浪日线抓取失败"):
        SinaAdapter().fetch_daily("600519", "20261020", "20261020")


def test_sina_adapter_rejects_unknown_adjust():
    with pytest.raises(FetchError) as ei:
        SinaAdapter().fetch_daily("600519", "20261020", "20261020", adj="xx")
    assert ei.value.kind == "unsupported"
