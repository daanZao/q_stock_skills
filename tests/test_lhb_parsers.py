"""交易所官方龙虎榜协议解析测试（issue #114，spec #111）：录制响应 fixture 驱动。

fixture 为 2026-10-19/20 实测响应裁剪（tests/fixtures/lhb/，样本取自
docs/research 调研报告实测记录与本 issue 实测观测），覆盖：
- 深交所汇总（翻页/空日/详情链接 ZBDM 解析）与个股席位明细（买卖两侧分流）
- 上交所定宽文本（14 类情形骨架、A/B 股与基金小节、折行情形标题、机构/沪股通
  席位、汇总行从右往左拆列、控制字符容忍）
- 北交所 JSONP（反热链注释/var 赋值包装、list/info 单位规约、旧码归一化入口）
- 新浪席位 JSONP（var details= 赋值形态、重复行去重）
测试不触网。
"""

import json
from pathlib import Path

import pytest

from qstock_mcp.adapters import _lhb_bse, _lhb_sse, _lhb_szse
from qstock_mcp.adapters._lhb_sina import parse_seat_jsonp, seat_net
from qstock_mcp.adapters.base import FetchError

FIXTURES = Path(__file__).parent / "fixtures" / "lhb"


def _json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 深交所


def test_szse_summary_pagination_fields():
    p1 = _lhb_szse.parse_summary_page(_json("szse_summary_p1.json"))
    assert p1["recordcount"] == 5 and p1["pagecount"] == 2
    assert len(p1["rows"]) == 4
    r = p1["rows"][1]
    assert r["zqdm"] == "000530"
    assert r["zqjc"] == "冰山冷热"
    assert r["cjje_yi"] == pytest.approx(20.42)  # 展示值亿元
    assert r["plyy"] == "日价格振幅达到18.39%"
    assert r["zbdm"] == "0902"  # 明细入口从 bz 链接解析
    p2 = _lhb_szse.parse_summary_page(_json("szse_summary_p2.json"))
    assert p2["rows"][0]["zqdm"] == "300750"


def test_szse_summary_empty_day_is_empty_list():
    parsed = _lhb_szse.parse_summary_page(_json("szse_summary_empty.json"))
    assert parsed["recordcount"] == 0 and parsed["rows"] == []


def test_szse_detail_seat_split_and_units():
    d = _lhb_szse.parse_detail(_json("szse_detail.json"))
    assert d["trade_date"] == "20261020"
    assert d["stock_name"] == "冰山冷热"  # &nbsp; 与括号代码清洗
    assert d["day_amount"] == pytest.approx(204283.8632)  # 2,042,838,632 元 → 万元
    assert len(d["buy_seats"]) == 5 and len(d["sell_seats"]) == 5
    # 买行取 mrje（买入额）；卖行取 mcje（卖出额）；席位 yuan 为原始单位（元）；
    # 合计与换算在适配器 _sum_wan 收敛（全库万元口径，ADR 0006）
    assert d["buy_seats"][0]["seat"] == "国信证券股份有限公司浙江互联网分公司"
    assert d["buy_seats"][0]["yuan"] == pytest.approx(37975089.0)
    assert d["sell_seats"][0]["seat"] == "机构专用"
    assert d["sell_seats"][0]["yuan"] == pytest.approx(46689716.0)


# ---------------------------------------------------------------- 上交所


def _sse_payload():
    text = _text("sse_file.jsonp")
    assert text.startswith("cb(")
    return json.loads(text[3:-1])


def test_sse_parse_sections_summary_and_seats():
    parsed = _lhb_sse.parse_sse_trade_file(_sse_payload())
    assert parsed["trade_date"] == "20261020"
    stocks = parsed["stocks"]
    # A 股小节 10 行（情形一 5 + 情形二 5）；B 股/基金小节与空情形不产生行
    assert len(stocks) == 10
    s600302 = stocks[0]
    assert s600302["code"] == "600302" and s600302["name"] == "ST标准"
    assert s600302["reason"].startswith("一、有价格涨跌幅限制")
    assert s600302["deviation"] == pytest.approx(9.93)
    assert s600302["amount_wan"] == pytest.approx(4461.78)  # 成交金额(万元) 原生
    assert len(s600302["buy_seats"]) == 5 and len(s600302["sell_seats"]) == 5
    assert s600302["buy_seats"][0] == {
        "seat": "申万宏源证券有限公司上海黄浦区陆家浜路证券营业部",
        "yuan": 2935474.00,
    }
    # 机构专用/沪股通专用席位标识保留
    codes = {s["code"]: s for s in stocks}
    assert any(seat["seat"] == "沪股通专用" for seat in codes["600267"]["buy_seats"])
    assert any(seat["seat"] == "沪股通专用" for seat in codes["603636"]["sell_seats"])
    # 多情形同股（603105 跌幅偏离）席位块挂到本小节汇总行
    assert len(codes["603105"]["buy_seats"]) == 5


def test_sse_parse_tolerates_wrapped_title_and_control_chars():
    parsed = _lhb_sse.parse_sse_trade_file(_sse_payload())
    # 折行情形标题（十、）不产生伪行；尾部控制字符（\u001a）不炸
    reasons = [s["reason"] for s in parsed["stocks"]]
    assert not any("式基金连续三个交易日" in r for r in reasons)
    assert all(r.startswith(("一、", "二、")) for r in reasons)


def test_sse_parse_empty_file_returns_no_stocks():
    parsed = _lhb_sse.parse_sse_trade_file(
        {"fileContents": ["  上海证券交易所每日交易信息", "", "交易日期:2026年10月18日"]}
    )
    assert parsed["trade_date"] == "20261018" and parsed["stocks"] == []


# ---------------------------------------------------------------- 北交所


def test_bse_unwrap_jsonp_variants():
    assert _lhb_bse.unwrap_jsonp('cb([{"a": 1}])') == [{"a": 1}]
    assert _lhb_bse.unwrap_jsonp("/*<script>x</script>*/\nvar details=({\"a\": 2});") == {"a": 2}
    with pytest.raises(Exception):
        _lhb_bse.unwrap_jsonp("not-jsonp")


def test_bse_list_rows_units_and_ids():
    rows = _lhb_bse.parse_bse_list(_lhb_bse.unwrap_jsonp(_text("bse_list.jsonp")))
    assert len(rows) == 5
    r = rows[0]
    assert r["zqdm"] == "920036" and r["id"] == 217089
    assert r["jyrq"] == "20261020"
    assert r["day_amount_wan"] == pytest.approx(10645.89)  # 万元原生
    assert r["plyy"] == "当日换手率达到25.16%"
    # 同股当日多情形：嘉晨智能两条独立 id（对齐 lhb_basic 业务键）
    jiachen = [x for x in rows if x["zqdm"] == "920096"]
    assert len(jiachen) == 2 and jiachen[0]["id"] != jiachen[1]["id"]
    # 异常期间情形（zflx 60，北交所特有口径）保留披露原文
    assert rows[3]["zflx"] == "60"


def test_bse_info_seat_units():
    info = _lhb_bse.parse_bse_info(_lhb_bse.unwrap_jsonp(_text("bse_info.jsonp")))
    assert len(info["buy_seats"]) == 5 and len(info["sell_seats"]) == 5
    # jgmrje（买入，元）→ 万元
    assert info["buy_seats"][0]["seat"] == "东海证券股份有限公司南京洪武北路营业部"
    assert info["buy_seats"][0]["yuan"] == pytest.approx(2852262.39)
    assert info["sell_seats"][0]["yuan"] == pytest.approx(6699236.31)


def test_bse_curl_cffi_missing_raises_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("curl_cffi"):
            raise ImportError("No module named 'curl_cffi'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(FetchError, match="curl_cffi"):
        _lhb_bse._import_curl_cffi()


# ---------------------------------------------------------------- 新浪席位 JSONP


def test_sina_seat_jsonp_var_assignment_and_dedup():
    seats = parse_seat_jsonp(_text("sina_seats.jsonp"))
    # 实测响应有重复行（国泰海通深圳分 208.958 出现两次）→ 去重保留首条
    buy = seats["buy"]
    assert len(buy) == 3
    assert buy[0]["seat"] == "东莞证券股份有限公司江西分公司"
    assert buy[0]["buy_wan"] == pytest.approx(580.3030)  # 万元
    assert buy[0]["net_wan"] == pytest.approx(580.303)
    assert len(seats["sell"]) == 2
    # 机构专用席位标识保留
    assert any(s["seat"] == "机构专用" for s in seats["sell"])


def test_sina_seat_net_uses_em_formula():
    """净买额对拍口径 = 东财口径：Σ买侧 buyAmount − Σ卖侧 sellAmount
    （各席位反向金额不计入）。"""
    seats = {
        "buy": [{"seat": "a", "buy_wan": 100.0, "sell_wan": 40.0}],
        "sell": [{"seat": "b", "buy_wan": 10.0, "sell_wan": 30.0}],
    }
    # 100 − 30 = 70（a 的卖出 40、b 的买入 10 均不计）
    assert seat_net(seats) == pytest.approx(70.0)
    assert seat_net({"buy": [], "sell": []}) is None  # 不伪造
    assert seat_net({"buy": [{"buy_wan": 100.0}], "sell": []}) == pytest.approx(100.0)


def test_sina_daily_detail_html_two_row_header():
    """真实页面每榜为双行头（首行榜名=指标，次行列头），逐表 skiprows=1 解析。"""
    pd = pytest.importorskip("pandas")
    pytest.importorskip("bs4")
    from qstock_mcp.adapters._lhb_sina import parse_daily_detail_html

    rows = parse_daily_detail_html(_text("sina_daily_detail.html"))
    assert len(rows) == 3
    r600302 = rows[0]
    assert r600302["stock_code"] == "600302"
    assert r600302["stock_name"] == "ST标准"
    assert r600302["close_price"] == pytest.approx(12.30)
    assert r600302["amount_wan"] == pytest.approx(4461.78)
    assert r600302["volume_wan"] == pytest.approx(672.121)
    assert r600302["indicator"] == "涨幅偏离值达7%的证券"  # 榜名行提取
    assert r600302["seat_type"] == "01"  # showDetail onclick 提取
    r000056 = rows[2]
    assert r000056["indicator"] == "跌幅偏离值达7%的证券"
    assert r000056["seat_type"] == "02"
