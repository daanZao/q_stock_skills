"""真实适配器的纯逻辑接缝测试：列映射与代码规则，不触网、不需要安装第三方库。

契约（issue #3）：baostock 兜底时拒绝北交所代码（4/8/9 开头）；缺列
（amplitude/turnover 等）置 null；缺失值不伪造。东财列映射（efinance/
akshare *_em/push2his）已随 issue #121（spec #111 收口）整体退役，本文件
只保留现存承载源的映射测试。
"""

import socket

import pytest

import qstock_mcp.adapters  # noqa: F401 - 导入即装配全局 socket 超时（issue #88）

from qstock_mcp.adapters._num import _money_cn
from qstock_mcp.adapters._lhb_yyb import (
    map_lhb_yyb_capital_rows,
    map_lhb_yyb_most_rows,
)
from qstock_mcp.adapters.baostock_adapter import (
    BaostockAdapter,
    map_astock_daily_rows,
    map_baostock_rows,
    map_stock_basic_rows,
    to_baostock_code,
)
from qstock_mcp.adapters.base import FetchError, is_bse_code


def test_default_socket_timeout_assembled():
    """全局 socket 超时装配（issue #88，spec #86）：akshare 等第三方封装不透传
    timeout，requests/urllib3 在 timeout=None 时回落到 socket 默认值；
    导入 adapters 包即生效，连接挂死从无限阻塞变为 60s 有界失败。"""
    assert socket.getdefaulttimeout() == 60


def test_baostock_mapping_missing_columns_are_none():
    fields = "date,open,high,low,close,volume,amount,pctChg".split(",")
    rows = [["2024-01-05", "10.0", "10.5", "9.8", "10.2", "12345.0", "125000.5", "2.0"]]
    (bar,) = map_baostock_rows(fields, rows)
    assert bar["trade_date"] == "20240105"
    assert bar["close"] == 10.2
    assert bar["volume"] == 12345
    assert bar["change_percent"] == 2.0
    # baostock 无振幅/涨跌额/换手率列 → null，不伪造
    assert bar["amplitude"] is None
    assert bar["change_amount"] is None
    assert bar["turnover_rate"] is None


def test_astock_daily_mapping_full_row():
    """次晨批一把抓（issue #83）：字段映射 + adjustflag → adj + 前缀过滤。"""
    fields = ("date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
              "turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST").split(",")
    rows = [
        ["2026-10-16", "sh.600519", "1700.0", "1720.0", "1690.0", "1710.0",
         "1700.0", "3000000", "5130000000", "3", "0.24", "1", "0.59",
         "20.0", "8.0", "5.0", "30.0", "0"],
        ["2026-10-16", "sz.002813", "26.38", "26.38", "26.38", "26.38",
         "26.38", "", "", "3", "", "0", "", "", "", "", "", "0"],
    ]
    bars = map_astock_daily_rows(fields, rows)
    assert len(bars) == 2
    normal, suspended = bars
    assert normal["stock_code"] == "600519"
    assert normal["trade_date"] == "20261016"
    assert normal["volume"] == 3000000  # 股口径透传（ADR 0008）
    assert normal["amount"] == 513000.0  # 元 → 万元（ADR 0006）
    assert normal["turnover_rate"] == 0.24  # turn 列映射
    assert normal["tradestatus"] == 1
    assert normal["adj"] == "none"  # adjustflag=3 不复权
    assert suspended["tradestatus"] == 0  # 停牌权威标注
    assert suspended["volume"] is None
    assert suspended["turnover_rate"] is None


def test_astock_daily_mapping_filters_non_a_share():
    fields = "date,code,close,volume,amount,adjustflag,turn,tradestatus,pctChg".split(",")
    rows = [
        ["2026-10-16", "sz.200002", "1", "1", "1", "3", "1", "1", "1"],  # 深 B 股
        ["2026-10-16", "sh.000001", "1", "1", "1", "3", "1", "1", "1"],  # 指数
        ["2026-10-16", "sz.300408", "1", "1", "1", "3", "1", "1", "1"],
    ]
    bars = map_astock_daily_rows(fields, rows)
    assert [b["stock_code"] for b in bars] == ["300408"]


def test_astock_daily_mapping_unknown_adjustflag_defaults_none():
    fields = "date,code,close,volume,amount,adjustflag,turn,tradestatus,pctChg".split(",")
    rows = [["2026-10-16", "sh.600519", "1", "1", "1", "", "1", "1", "1"]]
    (bar,) = map_astock_daily_rows(fields, rows)
    assert bar["adj"] == "none"


def test_bse_code_detection():
    assert is_bse_code("430047") and is_bse_code("830799") and is_bse_code("920001")
    assert not is_bse_code("600519") and not is_bse_code("000001") and not is_bse_code("300408")


def test_baostock_rejects_bse_code_before_any_network():
    # 不装 baostock 库也必须能明确拒绝（检查在 import 之前）
    with pytest.raises(FetchError, match="北交所"):
        BaostockAdapter().fetch_daily("920001", "20240101", "20240131")


def test_to_baostock_code():
    assert to_baostock_code("600519") == "sh.600519"
    assert to_baostock_code("000001") == "sz.000001"
    assert to_baostock_code("300408") == "sz.300408"
    assert to_baostock_code("688981") == "sh.688981"  # 科创板归 sh
    with pytest.raises(FetchError):
        to_baostock_code("110001")  # 未覆盖前缀明确报错


def test_baostock_rejects_market_snapshot():
    with pytest.raises(FetchError, match="不支持全市场快照"):
        BaostockAdapter().fetch_market_snapshot()


# --- 股票清单映射（issue #8）：baostock query_stock_basic 过滤 ---


def test_stock_basic_mapping_filters_b_share_index_and_delisted():
    fields = ["code", "code_name", "status"]
    rows = [
        ["sh.600519", "贵州茅台", "1"],  # 沪主板 A 股
        ["sz.000001", "平安银行", "1"],  # 深主板 A 股
        ["sz.300408", "三环集团", "1"],  # 创业板 A 股
        ["sz.200002", "万科B", "1"],     # 深圳 B 股 → 剔除
        ["sh.000001", "上证指数", "1"],  # 指数 → 剔除
        ["sh.600000", "浦发银行", "0"],  # 退市（status=0）→ 剔除
    ]
    assert map_stock_basic_rows(fields, rows) == [
        {"stock_code": "600519", "stock_name": "贵州茅台"},
        {"stock_code": "000001", "stock_name": "平安银行"},
        {"stock_code": "300408", "stock_name": "三环集团"},
    ]


def test_stock_basic_mapping_missing_status_kept():
    # status 列缺失时保留该行（不臆断）；前缀剥掉 sh./sz.
    fields = ["code", "code_name"]
    (row,) = map_stock_basic_rows(fields, [["sh.600519", "贵州茅台"]])
    assert row == {"stock_code": "600519", "stock_name": "贵州茅台"}


# --- 龙虎榜营业部两子表（issue #114，同花顺口径非东财） ---


def test_lhb_yyb_mapping():
    (cap,) = map_lhb_yyb_capital_rows(
        [{"序号": "1", "营业部名称": "机构专用",
          "累计参与金额": "6.59亿", "累计买入金额": "9785.87万"}])
    assert cap["rank"] == 1
    assert cap["seat_name"] == "机构专用"
    assert cap["total_amount"] == 65900.0  # 万元
    assert cap["buy_amount"] == 9785.87  # 万元
    (most,) = map_lhb_yyb_most_rows(
        [{"序号": "1", "营业部名称": "机构专用", "上榜次数": "10",
          "合计动用资金": "5.26亿", "年内买入股票只数": "5"}])
    assert most["appear_count"] == 10
    assert most["buy_amount"] == 52600.0  # 万元；上游口径为"合计动用资金"非纯买入
    assert most["buy_count"] == 5


def test_money_cn_formats():
    """中文金额 → 万元："亿"换算、"万"原值、无单位纯数字按万元计（上游实测推断）、
    缺失值 None（ADR 0006）。"""
    assert _money_cn("3.55亿") == 35500.0
    assert _money_cn("9785.87万") == 9785.87
    assert _money_cn("9950.85") == 9950.85  # 无单位按万元
    assert _money_cn(9950.85) == 9950.85
    assert _money_cn(None) is None
    assert _money_cn("-") is None
    assert _money_cn("") is None


def test_baostock_rejects_all_board_sections():
    bs = BaostockAdapter()
    for fn in (bs.fetch_indices, bs.fetch_boards, bs.fetch_zt_pool,
               bs.fetch_strong_stocks, bs.fetch_lhb):
        with pytest.raises(FetchError, match="不支持"):
            fn("20240105")
    with pytest.raises(FetchError, match="不支持"):
        bs.fetch_index_spot("000688")


def test_baostock_fields_include_turn():
    """issue #112：单股日线 _FIELDS 补 turn，恢复换手率（个股/指数实测均可用）。"""
    from qstock_mcp.adapters.baostock_adapter import _FIELDS

    assert "turn" in _FIELDS.split(",")


def test_baostock_mapping_turnover_rate_from_turn():
    fields = "date,open,high,low,close,volume,amount,pctChg,tradestatus,turn".split(",")
    rows = [["2026-10-20", "1321.01", "1335.00", "1316.14", "1331.88",
             "3413565", "4525398958.59", "0.5215", "1", "0.2731"]]
    (bar,) = map_baostock_rows(fields, rows)
    assert bar["turnover_rate"] == 0.2731  # 百分数口径直接入库
    assert bar["tradestatus"] == 1
