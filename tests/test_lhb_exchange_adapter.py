"""ExchangeLhbAdapter 组合层测试（issue #114，spec #111）：monkeypatch 子所客户端
与 akshare 入口，验证三所合并、金额口径（万元）、子项容错与三表契约
（statistic 由库内自算承载，不在适配器范围——见 test_lhb_tools）；不触网。
"""

import pytest

from qstock_mcp.adapters import lhb_exchange_adapter as mod
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.adapters.lhb_exchange_adapter import ExchangeLhbAdapter, _basic_row

TODAY = "20261020"


def _szse_summary(dash, dash2, page=1):
    if page == 1:
        return {
            "rows": [
                {"zqdm": "000530", "zqjc": "冰山冷热", "cjje_yi": 20.42,
                 "plyy": "日价格振幅达到18.39%", "dqrq": dash, "zbdm": "0902"},
                {"zqdm": "301001", "zqjc": "示例智能", "cjje_yi": 9.87,
                 "plyy": "日换手率达到44.65%", "dqrq": dash, "zbdm": "0903"},
            ],
            "recordcount": 2,
            "pagecount": 1,
        }
    raise AssertionError("不应翻页")


def _szse_detail(dqrq, zqdm, zbdm):
    return {
        "trade_date": TODAY,
        "stock_name": "冰山冷热",
        "day_amount": 204283.8632,
        "buy_seats": [
            {"seat": "机构专用", "yuan": 26015072.0},
            {"seat": "示例买二", "yuan": 10000000.0},
        ],
        "sell_seats": [
            {"seat": "机构专用", "yuan": 46689716.0},
            {"seat": "示例卖二", "yuan": 5000000.0},
        ],
    }


def _sse_file(dash):
    return {
        "trade_date": TODAY,
        "stocks": [
            {
                "code": "600302", "name": "ST标准",
                "reason": "一、有价格涨跌幅限制的日收盘价格涨幅偏离值达到7%的前五只证券",
                "deviation": 9.93, "volume": 6721210, "amount_wan": 4461.78,
                "buy_seats": [{"seat": "申万宏源陆家浜路", "yuan": 2935474.00}],
                "sell_seats": [{"seat": "示例卖一", "yuan": 3522574.00}],
            }
        ],
    }


def _bse_list(dash, dash2):
    return ([
        {"id": 1, "zqdm": "833454", "zqmc": "同心传动", "jyrq": TODAY,
         "day_amount_wan": 1200.0, "plyy": "当日换手率达到24.97%",
         "sjsx": "当日换手率达到20%的前5只股票", "gjzf": 24.97, "zflx": "10"},
        {"id": 2, "zqdm": "920096", "zqmc": "嘉晨智能", "jyrq": TODAY,
         "day_amount_wan": 11583.31, "plyy": "当日收盘价涨幅达到29.98%",
         "sjsx": "当日收盘价涨幅达到20%的前5只股票", "gjzf": 29.98, "zflx": "50"},
    ], 2)


def _bse_info(list_id):
    if list_id == 1:  # 旧码 833454 → 920 新码归一化（官方对照表）
        return {"buy_seats": [{"seat": "示例买一", "yuan": 1000000.0}],
                "sell_seats": [{"seat": "示例卖一", "yuan": 500000.0}]}
    return {"buy_seats": [{"seat": "示例买一", "yuan": 2000000.0}],
            "sell_seats": [{"seat": "示例卖一", "yuan": 1000000.0}]}


def _patch_markets(monkeypatch):
    monkeypatch.setattr(mod._lhb_szse, "fetch_summary", _szse_summary)
    monkeypatch.setattr(mod._lhb_szse, "fetch_detail", _szse_detail)
    monkeypatch.setattr(mod._lhb_sse, "fetch_trade_file", _sse_file)
    monkeypatch.setattr(mod._lhb_bse, "fetch_list", _bse_list)
    monkeypatch.setattr(mod._lhb_bse, "fetch_info", _bse_info)


def _patch_akshare(monkeypatch):
    """akshare 懒加载替换：同花顺 yyb 两函数（statistic 已由库内自算承载，
    适配器不再触碰 ggtj——issue #114 定稿）。"""
    class FakeAK:
        @staticmethod
        def stock_lh_yyb_capital():
            return _FakeDF([
                {"序号": "1", "营业部名称": "机构专用", "累计参与金额": "6.59亿",
                 "累计买入金额": "9785.87万"},
            ])

        @staticmethod
        def stock_lh_yyb_most():
            return _FakeDF([
                {"序号": "1", "营业部名称": "机构专用", "上榜次数": "10",
                 "合计动用资金": "5.26亿", "年内买入股票只数": "5"},
            ])

    monkeypatch.setattr(mod, "import_akshare", lambda: FakeAK)


class _FakeDF:
    def __init__(self, records):
        self._records = records
        self.empty = not records

    def to_dict(self, _orient):
        return list(self._records)


def test_fetch_lhb_merges_three_exchanges_and_tables(monkeypatch):
    _patch_markets(monkeypatch)
    _patch_akshare(monkeypatch)
    adapter = ExchangeLhbAdapter(pause_s=0.0, clock=lambda _s: None)
    out = adapter.fetch_lhb(TODAY)
    # statistic 不在适配器承载（issue #114 定稿）：库内 lhb_basic 自算
    assert set(out) >= {"lhb_basic", "lhb_yyb_capital", "lhb_yyb_most"}
    assert "lhb_stock_statistic" not in out
    assert "errors" not in out

    basic = {r["stock_code"]: r for r in out["lhb_basic"]}
    assert set(basic) == {"000530", "301001", "920454", "920096", "600302"}
    # 万元口径：深所买 2601.5072+1000 − 卖 4668.9716+500；net = buy − sell
    r = basic["000530"]
    assert r["buy_amount"] == pytest.approx(3601.5072)
    assert r["sell_amount"] == pytest.approx(5168.9716)
    assert r["net_buy_amount"] == pytest.approx(-1567.4644)
    assert r["total_amount"] == pytest.approx(8770.4788)  # buy + sell（东财口径）
    assert r["day_amount"] == pytest.approx(204283.8632)  # 对拍元数据键
    assert r["turnover_rate"] is None  # 振幅情形无可提取换手率
    assert r["stock_name"] == "冰山冷热"
    # 换手率情形文本提取（不伪造：仅"换手率达到X%"模式）
    assert basic["301001"]["turnover_rate"] == pytest.approx(44.65)
    # 上交所：席位元 → 万元
    r = basic["600302"]
    assert r["buy_amount"] == pytest.approx(293.5474)
    assert r["sell_amount"] == pytest.approx(352.2574)
    assert r["day_amount"] == pytest.approx(4461.78)  # 汇总表万元原生
    assert r["lhb_reason"].startswith("一、有价格涨跌幅限制")
    # 北交所旧码 833454 归一化到 920 新码（官方对照表）
    assert "920454" in basic and "833454" not in basic
    assert basic["920454"]["buy_amount"] == pytest.approx(100.0)

    # yyb 两表：同花顺接口委托 akshare 适配器映射（中文金额 → 万元）
    assert out["lhb_yyb_capital"][0]["buy_amount"] == pytest.approx(9785.87)
    assert out["lhb_yyb_most"][0]["buy_amount"] == pytest.approx(52600.0)


def test_single_market_failure_is_partial(monkeypatch):
    _patch_markets(monkeypatch)
    _patch_akshare(monkeypatch)

    def boom(dash, dash2):
        raise FetchError("北交所 boom")

    monkeypatch.setattr(mod._lhb_bse, "fetch_list", boom)
    adapter = ExchangeLhbAdapter(pause_s=0.0, clock=lambda _s: None)
    out = adapter.fetch_lhb(TODAY)
    codes = {r["stock_code"] for r in out["lhb_basic"]}
    assert "920096" not in codes and "000530" in codes  # 拿到什么落什么
    assert any("bse" in e and "boom" in e for e in out["errors"])


def test_all_subitems_failed_raises_fetch_error(monkeypatch):
    _patch_markets(monkeypatch)

    def boom(*a, **k):
        raise FetchError("boom")

    monkeypatch.setattr(mod._lhb_szse, "fetch_summary", boom)
    monkeypatch.setattr(mod._lhb_sse, "fetch_trade_file", boom)
    monkeypatch.setattr(mod._lhb_bse, "fetch_list", boom)

    class BoomAK:
        @staticmethod
        def stock_lh_yyb_capital():
            raise FetchError("ths boom")

        @staticmethod
        def stock_lh_yyb_most():
            raise FetchError("ths boom")

    monkeypatch.setattr(mod, "import_akshare", lambda: BoomAK)
    adapter = ExchangeLhbAdapter(pause_s=0.0, clock=lambda _s: None)
    with pytest.raises(FetchError, match="交易所龙虎榜抓取失败"):
        adapter.fetch_lhb(TODAY)


def test_yyb_failure_keeps_basic_with_partial_error(monkeypatch):
    """yyb 子项失败：basic 照常拿到并记 errors（工具层报 partial_error）；
    statistic 不受适配器子项失败影响（库内自算，见 tools_lhb 测试）。"""
    _patch_markets(monkeypatch)

    class BoomAK:
        @staticmethod
        def stock_lh_yyb_capital():
            raise RuntimeError("同花顺改版")

        @staticmethod
        def stock_lh_yyb_most():
            return _FakeDF([])

    monkeypatch.setattr(mod, "import_akshare", lambda: BoomAK)
    adapter = ExchangeLhbAdapter(pause_s=0.0, clock=lambda _s: None)
    out = adapter.fetch_lhb(TODAY)
    assert out["lhb_basic"] and out["lhb_yyb_capital"] == []
    assert any("lhb_yyb_capital" in e for e in out["errors"])


def test_basic_row_semantics():
    """单元语义：全空席位不伪造；换手率达到X% 提取；偏离值不写 change_percent。"""
    row = _basic_row(
        "20261020", "000001", "示例", "日价格涨幅偏离值达到7.12%",
        [], [], day_amount=None,
    )
    assert row["buy_amount"] is None and row["sell_amount"] is None
    assert row["net_buy_amount"] is None and row["total_amount"] is None
    assert row["turnover_rate"] is None
    assert row["change_percent"] is None  # 偏离值 ≠ 涨跌幅，不伪造
    row2 = _basic_row(
        "20261020", "000002", "示例", "当日换手率达到24.97%",
        [{"seat": "a", "yuan": 20000.0}], [{"seat": "b", "yuan": 5000.0}],
    )
    assert row2["turnover_rate"] == pytest.approx(24.97)
    assert row2["net_buy_amount"] == pytest.approx(1.5)  # 2 − 0.5 万元


def test_szse_missing_zbdm_row_skipped_not_market_killed(monkeypatch):
    """实测形状（2026-10-23 冒烟）：个别榜单类型汇总行 bz 无 ZBDM——跳过该股，
    其余深市行照常落（单行异常不拖垮整个市场）。"""
    _patch_markets(monkeypatch)
    _patch_akshare(monkeypatch)

    def summary_with_bad_row(dash, dash2, page=1):
        return {
            "rows": [
                {"zqdm": "000530", "zqjc": "冰山冷热", "cjje_yi": 20.42,
                 "plyy": "日价格振幅达到18.39%", "dqrq": dash, "zbdm": "0902"},
                {"zqdm": "301313", "zqjc": "无榜单码", "cjje_yi": 1.0,
                 "plyy": "日价格振幅达到15.10%", "dqrq": dash, "zbdm": None},
            ],
            "recordcount": 2,
            "pagecount": 1,
        }

    monkeypatch.setattr(mod._lhb_szse, "fetch_summary", summary_with_bad_row)
    adapter = ExchangeLhbAdapter(pause_s=0.0, clock=lambda _s: None)
    out = adapter.fetch_lhb(TODAY)
    codes = {r["stock_code"] for r in out["lhb_basic"]}
    assert "000530" in codes and "301313" not in codes
    assert "errors" not in out  # 深市其余行照常，不算 partial


def test_szse_transient_summary_page_failure_retries(monkeypatch):
    """汇总中途单页瞬时断连应原地重试，不能把整天深市判为不完整。"""
    _patch_markets(monkeypatch)
    _patch_akshare(monkeypatch)
    attempts = []

    def flaky_summary(dash, dash2, page=1):
        attempts.append(page)
        if page == 6 and attempts.count(6) == 1:
            raise FetchError(
                "深交所龙虎榜汇总解析失败（2026-10-20~2026-10-20 第6页）: "
                "深交所请求失败: Remote end closed connection without response"
            )
        return {
            "rows": [{
                "zqdm": "000530", "zqjc": "冰山冷热", "cjje_yi": 20.42,
                "plyy": "日价格振幅达到18.39%", "dqrq": dash, "zbdm": "0902",
            }] if page == 6 else [],
            "recordcount": 1,
            "pagecount": 6,
        }

    monkeypatch.setattr(mod._lhb_szse, "fetch_summary", flaky_summary)
    adapter = ExchangeLhbAdapter(pause_s=0.0, clock=lambda _s: None)
    out = adapter.fetch_lhb(TODAY)
    assert attempts.count(6) == 2
    assert "errors" not in out
    assert any(r["stock_code"] == "000530" for r in out["lhb_basic"])


def test_szse_persistent_summary_page_failure_stops_after_three_attempts(monkeypatch):
    _patch_markets(monkeypatch)
    _patch_akshare(monkeypatch)
    attempts = []

    def unavailable_summary(dash, dash2, page=1):
        attempts.append(page)
        if page == 2:
            raise FetchError("Remote end closed connection without response")
        return {"rows": [], "recordcount": 0, "pagecount": 2}

    monkeypatch.setattr(mod._lhb_szse, "fetch_summary", unavailable_summary)
    adapter = ExchangeLhbAdapter(pause_s=0.0, clock=lambda _s: None)
    out = adapter.fetch_lhb(TODAY)
    assert attempts.count(2) == 3
    assert any("szse" in error for error in out["errors"])


def test_szse_primary_uses_ths_without_calling_official(monkeypatch):
    _patch_markets(monkeypatch)
    _patch_akshare(monkeypatch)

    class FakeThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, trade_date):
            return [
                _basic_row(
                    trade_date, "000530", "同花顺深圳", "深圳主采", [],
                    [{"seat": "卖一", "yuan": 10000.0}], day_amount=1234.0,
                ),
                _basic_row(
                    trade_date, "600302", "不应采用", "沪市仍用官方", [], [],
                ),
            ]

    adapter = ExchangeLhbAdapter(
        pause_s=0.0, clock=lambda _s: None, szse_primary=FakeThs(),
    )

    def unexpected_szse(_trade_date):
        raise AssertionError("同花顺主采成功时不请求深交所官方")

    monkeypatch.setattr(adapter, "_szse_basic", unexpected_szse)
    out = adapter.fetch_lhb(TODAY)
    szse = next(row for row in out["lhb_basic"] if row["stock_code"] == "000530")
    sse = next(row for row in out["lhb_basic"] if row["stock_code"] == "600302")
    assert szse["stock_name"] == "同花顺深圳"
    assert sse["buy_amount"] == pytest.approx(293.5474)
    assert out["szse_primary"] == {
        "requested": "ths_fake", "source": "ths_fake", "rows": 1,
    }
    assert len(out["ths_basic_rows"]) == 2
    assert "errors" not in out


def test_szse_primary_failure_falls_back_to_official(monkeypatch):
    _patch_markets(monkeypatch)
    _patch_akshare(monkeypatch)

    class BrokenThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, _trade_date):
            raise FetchError("同花顺封禁")

    adapter = ExchangeLhbAdapter(
        pause_s=0.0, clock=lambda _s: None, szse_primary=BrokenThs(),
    )
    out = adapter.fetch_lhb(TODAY)
    szse = next(row for row in out["lhb_basic"] if row["stock_code"] == "000530")
    assert szse["stock_name"] == "冰山冷热"
    assert out["szse_primary"]["source"] == "exchange"
    assert out["szse_primary"]["requested"] == "ths_fake"
    assert "同花顺封禁" in out["szse_primary"]["error"]
    assert "errors" not in out


def test_all_basic_failure_keeps_szse_primary_metadata_on_error(monkeypatch):
    class BrokenThs:
        name = "ths_fake"

        def fetch_lhb_basic(self, _trade_date):
            raise FetchError("同花顺封禁")

    class BoomAK:
        @staticmethod
        def stock_lh_yyb_capital():
            raise FetchError("ths boom")

        @staticmethod
        def stock_lh_yyb_most():
            raise FetchError("ths boom")

    monkeypatch.setattr(mod, "import_akshare", lambda: BoomAK)
    adapter = ExchangeLhbAdapter(
        pause_s=0.0, clock=lambda _s: None, szse_primary=BrokenThs(),
    )

    def boom(_trade_date):
        raise FetchError("官方市场断连")

    for method in ("_szse_basic", "_sse_basic", "_bse_basic"):
        monkeypatch.setattr(adapter, method, boom)
    with pytest.raises(FetchError) as caught:
        adapter.fetch_lhb(TODAY)
    assert caught.value.source_meta["szse_primary"] == {
        "requested": "ths_fake", "source": "exchange", "rows": 0,
        "error": "同花顺封禁",
    }
