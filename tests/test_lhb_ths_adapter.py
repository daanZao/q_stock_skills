"""同花顺历史龙虎榜 basic 后备适配器测试（issue #126）。"""

from pathlib import Path

import pytest

from qstock_mcp.adapters.base import FetchError
from qstock_mcp.adapters.lhb_ths_adapter import ThsLhbAdapter


FIXTURE = Path(__file__).parent / "fixtures" / "lhb" / "ths_daily_basic.html"


def test_fetch_lhb_basic_maps_recorded_daily_page():
    calls = []

    def load(url):
        calls.append(url)
        return FIXTURE.read_text(encoding="utf-8").encode("gb18030")

    rows = ThsLhbAdapter(page_loader=load, clock=lambda _s: None).fetch_lhb_basic(
        "20261020"
    )

    assert calls == [
        "https://data.10jqka.com.cn/ifmarket/lhbggxq/report/2026-10-20/"
    ]
    assert rows == [
        {
            "trade_date": "20261020",
            "stock_code": "920036",
            "stock_name": "同惠电子",
            "close_price": None,
            "change_percent": None,
            "turnover_rate": None,
            "lhb_reason": "当日换手率达到20%的证券",
            "net_buy_amount": pytest.approx(-533.62),
            "buy_amount": pytest.approx(1255.69),
            "sell_amount": pytest.approx(1789.31),
            "total_amount": pytest.approx(3045.0),
            "day_amount": pytest.approx(3044.99),
        },
        {
            "trade_date": "20261020",
            "stock_code": "002242",
            "stock_name": "九阳股份",
            "close_price": None,
            "change_percent": None,
            "turnover_rate": None,
            "lhb_reason": "日涨幅偏离值达7%的证券",
            "net_buy_amount": pytest.approx(10500.0),
            "buy_amount": pytest.approx(33400.0),
            "sell_amount": pytest.approx(22900.0),
            "total_amount": pytest.approx(56300.0),
            "day_amount": pytest.approx(56300.0),
        },
    ]


def test_ths_lhb_pauses_between_dates_and_stops_after_ban():
    calls = []
    sleeps = []

    def load(url):
        calls.append(url)
        if len(calls) == 1:
            return FIXTURE.read_text(encoding="utf-8").encode("gb18030")
        return b"<html>403 Forbidden</html>"

    adapter = ThsLhbAdapter(page_loader=load, clock=sleeps.append)
    assert adapter.fetch_lhb_basic("20261020")
    with pytest.raises(FetchError, match="封禁"):
        adapter.fetch_lhb_basic("20261021")
    with pytest.raises(FetchError, match="停止后续"):
        adapter.fetch_lhb_basic("20261022")

    assert len(calls) == 2
    assert sleeps == [20.0]
