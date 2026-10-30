"""同花顺历史龙虎榜 basic 后备适配器测试（issue #126/#127）。"""

from pathlib import Path

import pytest

from qstock_mcp.adapters.base import FetchError
from qstock_mcp.adapters.lhb_ths_adapter import ThsLhbAdapter


FIXTURE = Path(__file__).parent / "fixtures" / "lhb" / "ths_daily_basic.html"
COMPLEX_FIXTURE = Path(__file__).parent / "fixtures" / "lhb" / "ths_daily_complex.html"
SEATS_FIXTURE = Path(__file__).parent / "fixtures" / "lhb" / "ths_daily_seats.html"


def test_fetch_lhb_maps_basic_and_szse_details_from_same_page():
    calls = []

    def load(url):
        calls.append(url)
        return SEATS_FIXTURE.read_text(encoding="utf-8").encode("gb18030")

    out = ThsLhbAdapter(page_loader=load, clock=lambda _s: None).fetch_lhb(
        "20261020"
    )

    assert len(calls) == 1
    assert out["lhb_basic"][0]["buy_amount"] == pytest.approx(17800.0)
    assert out["lhb_stock_detail"] == [
        {
            "trade_date": "20261020",
            "stock_code": "000530",
            "lhb_reason": "日振幅值达15%的证券;日换手率达20%的证券",
            "seat_name": "国信证券股份有限公司浙江互联网分公司",
            "seat_code": "9ef239188b9d9bcf",
            "seat_type": "buy",
            "amount": pytest.approx(3797.51),
            "amount_percent": None,
            "buy_count_3m": None,
            "sell_count_3m": None,
            "buy_amount": pytest.approx(3797.51),
            "sell_amount": pytest.approx(3342.48),
            "net_buy_amount": pytest.approx(455.03),
        },
        {
            "trade_date": "20261020",
            "stock_code": "000530",
            "lhb_reason": "日振幅值达15%的证券;日换手率达20%的证券",
            "seat_name": "机构专用",
            "seat_code": None,
            "seat_type": "buy",
            "amount": pytest.approx(2601.51),
            "amount_percent": None,
            "buy_count_3m": None,
            "sell_count_3m": None,
            "buy_amount": pytest.approx(2601.51),
            "sell_amount": pytest.approx(4668.97),
            "net_buy_amount": pytest.approx(-2067.46),
        },
        {
            "trade_date": "20261020",
            "stock_code": "000530",
            "lhb_reason": "日振幅值达15%的证券;日换手率达20%的证券",
            "seat_name": "国信证券股份有限公司浙江互联网分公司",
            "seat_code": "9ef239188b9d9bcf",
            "seat_type": "sell",
            "amount": pytest.approx(3342.48),
            "amount_percent": None,
            "buy_count_3m": None,
            "sell_count_3m": None,
            "buy_amount": pytest.approx(3797.51),
            "sell_amount": pytest.approx(3342.48),
            "net_buy_amount": pytest.approx(455.03),
        },
        {
            "trade_date": "20261020",
            "stock_code": "000530",
            "lhb_reason": "日振幅值达15%的证券;日换手率达20%的证券",
            "seat_name": "东亚前海证券有限责任公司苏州分公司",
            "seat_code": "cdab1cd0cb07204d",
            "seat_type": "sell",
            "amount": pytest.approx(3250.10),
            "amount_percent": None,
            "buy_count_3m": None,
            "sell_count_3m": None,
            "buy_amount": pytest.approx(37.02),
            "sell_amount": pytest.approx(3250.10),
            "net_buy_amount": pytest.approx(-3213.08),
        },
    ]


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


def test_complex_page_multi_reason_cumulative_bse_and_security_filter():
    rows = ThsLhbAdapter(
        page_loader=lambda _url: COMPLEX_FIXTURE.read_text(encoding="utf-8").encode(
            "gb18030"
        ),
        clock=lambda _s: None,
    ).fetch_lhb_basic("20261020")

    assert len(rows) == 3
    assert {"113001", "900901"}.isdisjoint(row["stock_code"] for row in rows)
    multi = [row for row in rows if row["stock_code"] == "002164"]
    assert len(multi) == 2
    ordinary = next(row for row in multi if "连续三个交易日" not in row["lhb_reason"])
    cumulative = next(row for row in multi if "连续三个交易日" in row["lhb_reason"])
    assert ordinary["day_amount"] == pytest.approx(44300.0)
    assert ordinary["buy_amount"] == pytest.approx(11500.0)
    assert cumulative["day_amount"] is None
    assert cumulative["total_amount"] == pytest.approx(25600.0)
    bse = next(row for row in rows if row["stock_name"] == "同心传动")
    assert bse["stock_code"] == "920454"
    assert bse["net_buy_amount"] == pytest.approx(50.0)


@pytest.mark.parametrize("scenario", ["date_mismatch", "partial_html", "duplicate_key"])
def test_invalid_page_is_rejected_as_whole(scenario):
    html = FIXTURE.read_text(encoding="utf-8")
    if scenario == "date_mismatch":
        html = html.replace('report="2026-10-20"', 'report="2026-10-19"')
    elif scenario == "partial_html":
        html = html.replace("合计买入：", "", 1)
    else:
        start = html.index('<div class="stockcont"')
        end = html.index("</div>", start) + len("</div>")
        html = html.replace("</body>", html[start:end] + "\n</body>")

    adapter = ThsLhbAdapter(
        page_loader=lambda _url: html.encode("gb18030"), clock=lambda _s: None
    )
    with pytest.raises(FetchError):
        adapter.fetch_lhb_basic("20261020")


def test_duplicate_seat_invalidates_only_its_parent_details():
    html = SEATS_FIXTURE.read_text(encoding="utf-8")
    seat = html.index("国信证券股份有限公司浙江互联网分公司")
    start = html.rfind("<tr>", 0, seat)
    end = html.index("</tr>", seat) + len("</tr>")
    html = html[:end] + html[start:end] + html[end:]

    out = ThsLhbAdapter(
        page_loader=lambda _url: html.encode("gb18030"), clock=lambda _s: None
    ).fetch_lhb("20261020")

    assert len(out["lhb_basic"]) == 1
    assert out["lhb_stock_detail"] == []
    assert len(out["detail_errors"]) == 1
    assert "席位重复" in out["detail_errors"][0]


def test_malformed_seat_row_invalidates_parent_details():
    html = SEATS_FIXTURE.read_text(encoding="utf-8").replace(
        "</tbody>", "<tr><td>残缺席位行</td></tr></tbody>", 1
    )

    out = ThsLhbAdapter(
        page_loader=lambda _url: html.encode("gb18030"), clock=lambda _s: None
    ).fetch_lhb("20261020")

    assert len(out["lhb_basic"]) == 1
    assert out["lhb_stock_detail"] == []
    assert len(out["detail_errors"]) == 1
    assert "席位行不完整" in out["detail_errors"][0]
