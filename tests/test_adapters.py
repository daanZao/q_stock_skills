"""真实适配器的纯逻辑接缝测试：列映射与代码规则，不触网、不需要安装第三方库。

契约（issue #3）：baostock 兜底时拒绝北交所代码（4/8/9 开头）；缺列
（amplitude/turnover 等）置 null；缺失值不伪造。
"""

import pytest

from qstock_mcp.adapters._eastmoney import map_eastmoney_rows, map_spot_rows, spot_trade_date
from qstock_mcp.adapters.baostock_adapter import (
    BaostockAdapter,
    map_baostock_rows,
    to_baostock_code,
)
from qstock_mcp.adapters.base import FetchError, is_bse_code

EASTMONEY_RECORD = {
    "日期": "2024-01-05",
    "开盘": "10.0",
    "收盘": "10.2",
    "最高": "10.5",
    "最低": "9.8",
    "成交量": "12345",
    "成交额": "125000.5",
    "振幅": "7.0",
    "涨跌幅": "2.0",
    "涨跌额": "0.2",
    "换手率": "1.5",
}


def test_eastmoney_mapping_full_row():
    (bar,) = map_eastmoney_rows([EASTMONEY_RECORD])
    assert bar == {
        "trade_date": "20240105",
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "volume": 12345,
        "amount": 125000.5,
        "amplitude": 7.0,
        "change_percent": 2.0,
        "change_amount": 0.2,
        "turnover_rate": 1.5,
    }


def test_eastmoney_mapping_dash_becomes_none():
    (bar,) = map_eastmoney_rows([dict(EASTMONEY_RECORD, 振幅="-", 换手率="-")])
    assert bar["amplitude"] is None
    assert bar["turnover_rate"] is None


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


# --- 全市场快照映射（issue #4）：efinance 与 akshare(spot_em) 同为东财列 ---

AKSHARE_SPOT_RECORD = {
    "代码": "600519",
    "名称": "贵州茅台",
    "最新价": "1700.0",
    "涨跌幅": "2.0",
    "涨跌额": "33.3",
    "成交量": "12345",
    "成交额": "125000.5",
    "振幅": "3.0",
    "最高": "1710.0",
    "最低": "1680.0",
    "今开": "1690.0",
    "昨收": "1666.7",
    "量比": "1.2",
    "换手率": "0.5",
    "市盈率-动态": "25.0",
    "市净率": "8.0",
    "总市值": "2.1e12",
    "流通市值": "2.1e12",
}


def test_spot_mapping_akshare_columns():
    (row,) = map_spot_rows([AKSHARE_SPOT_RECORD])
    assert row == {
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "latest_price": 1700.0,
        "change_percent": 2.0,
        "change_amount": 33.3,
        "amplitude": 3.0,
        "high": 1710.0,
        "low": 1680.0,
        "open": 1690.0,
        "pre_close": 1666.7,
        "volume_ratio": 1.2,
        "turnover_rate": 0.5,
        "pe_ratio": 25.0,
        "pb_ratio": 8.0,
        "volume": 12345,
        "amount": 125000.5,
        "market_cap": 2.1e12,
        "float_cap": 2.1e12,
    }


def test_spot_mapping_efinance_column_aliases():
    # efinance 实测列名（EASTMONEY_QUOTE_FIELDS）：昨日收盘/动态市盈率，且无振幅/市净率列
    record = dict(AKSHARE_SPOT_RECORD)
    record["昨日收盘"] = record.pop("昨收")
    record["动态市盈率"] = record.pop("市盈率-动态")
    del record["振幅"], record["市净率"]
    (row,) = map_spot_rows([record])
    assert row["stock_code"] == "600519"
    assert row["pre_close"] == 1666.7
    assert row["pe_ratio"] == 25.0
    # efinance 无振幅/市净率列 → null，不伪造
    assert row["amplitude"] is None
    assert row["pb_ratio"] is None


def test_spot_mapping_dash_becomes_none():
    (row,) = map_spot_rows([dict(AKSHARE_SPOT_RECORD, 最新价="-", 量比="-")])
    assert row["latest_price"] is None
    assert row["volume_ratio"] is None


def test_spot_trade_date_from_efinance_column():
    records = [
        {"最新交易日": "2024-01-04"},
        {"最新交易日": "2024-01-05"},
        {"最新交易日": "-"},
    ]
    assert spot_trade_date(records) == "20240105"


def test_spot_trade_date_absent_returns_none():
    assert spot_trade_date([{"代码": "600519"}]) is None
    assert spot_trade_date([]) is None


def test_baostock_rejects_market_snapshot():
    with pytest.raises(FetchError, match="不支持全市场快照"):
        BaostockAdapter().fetch_market_snapshot()


# --- 盘面快照映射（issue #5）：列映射以旧项目 appdb 实测写入端为准 ---

from qstock_mcp.adapters._board_em import (
    map_board_rows,
    map_index_rows,
    map_lhb_basic_rows,
    map_lhb_statistic_rows,
    map_lhb_yyb_capital_rows,
    map_lhb_yyb_most_rows,
    map_strong_rows,
    map_zt_pool_rows,
)


def test_index_mapping_akshare_columns():
    record = {
        "代码": "000001", "名称": "上证指数", "最新价": "3000.0",
        "涨跌幅": "0.5", "涨跌额": "15.0", "成交量": "100", "成交额": "1.0e9",
        "振幅": "1.0", "最高": "3010.0", "最低": "2990.0", "今开": "2995.0",
        "昨收": "2985.0", "量比": "1.1",
    }
    (row,) = map_index_rows([record])
    assert row["index_code"] == "000001"
    assert row["index_name"] == "上证指数"
    assert row["amplitude"] == 1.0
    assert row["pre_close"] == 2985.0
    assert row["trade_date"] is None  # akshare 无"最新交易日"列，由工具层回退


def test_index_mapping_efinance_aliases():
    record = {
        "股票代码": "000001", "股票名称": "上证指数", "最新价": "3000.0",
        "涨跌幅": "0.5", "涨跌额": "15.0", "成交量": "100", "成交额": "1.0e9",
        "最高": "3010.0", "最低": "2990.0", "今开": "2995.0",
        "昨日收盘": "2985.0", "量比": "1.1", "最新交易日": "2024-01-05",
    }
    (row,) = map_index_rows([record])
    assert row["index_code"] == "000001"
    assert row["pre_close"] == 2985.0
    assert row["trade_date"] == "20240105"  # efinance 行级"最新交易日"优先
    assert row["amplitude"] is None  # efinance 无振幅列 → null，不伪造


def test_board_mapping():
    record = {
        "板块代码": "BK0477", "板块名称": "酿酒行业", "最新价": "2000.0",
        "涨跌幅": "1.5", "涨跌额": "30.0", "领涨股票": "贵州茅台",
        "领涨股票-涨跌幅": "2.0",
    }
    (row,) = map_board_rows([record], "industry")
    assert row == {
        "board_type": "industry",
        "board_code": "BK0477",
        "board_name": "酿酒行业",
        "latest_price": 2000.0,
        "change_percent": 1.5,
        "change_amount": 30.0,
        "volume": None,
        "amount": None,
        "amplitude": None,
        "high": None,
        "low": None,
        "open": None,
        "pre_close": None,
        "volume_ratio": None,
        "stock_count": None,
        "leading_stock": "贵州茅台",
        "leading_change": 2.0,
    }


def test_zt_pool_mapping_per_pool_type():
    common = {"代码": "600519", "名称": "贵州茅台", "最新价": "1700.0",
              "涨跌幅": "10.0", "成交额": "1.0e9", "所属行业": "酿酒行业"}
    (zt,) = map_zt_pool_rows(
        [dict(common, 首次封板时间="093000", 连板数="1")], "zt")
    assert zt["pool_type"] == "zt"
    assert zt["limit_up_time"] == "093000"
    assert zt["consecutive_boards"] == 1
    assert zt["zt_price"] is None and zt["zb_info"] is None
    (dt,) = map_zt_pool_rows(
        [dict(common, 最后封板时间="143000", 连续跌停="2")], "dt")
    assert dt["limit_up_time"] == "143000"
    assert dt["consecutive_boards"] == 2
    (zb,) = map_zt_pool_rows(
        [dict(common, 首次封板时间="100000", 涨停价="1870.0", 炸板次数="3")], "zb")
    assert zb["zt_price"] == 1870.0
    assert zb["zb_info"] == "3"
    # 缺失值（nan/空）不伪造
    (zb2,) = map_zt_pool_rows(
        [dict(common, 首次封板时间=float("nan"), 涨停价="-", 炸板次数="")], "zb")
    assert zb2["limit_up_time"] is None
    assert zb2["zt_price"] is None
    assert zb2["zb_info"] is None


def test_strong_mapping():
    record = {"代码": "000001", "名称": "平安银行", "最新价": "10.0",
              "涨跌幅": "5.0", "成交额": "5.0e8", "换手率": "3.0",
              "总市值": "2.0e11", "所属行业": "银行", "入选理由": "放量上涨"}
    (row,) = map_strong_rows([record])
    assert row["stock_code"] == "000001"
    assert row["turnover_rate"] == 3.0
    assert row["market_cap"] == 2.0e11
    assert row["reason"] == "放量上涨"


def test_lhb_basic_mapping_listing_date():
    record = {"上榜日": "2024-01-05", "代码": "600519", "名称": "贵州茅台",
              "收盘价": "1700.0", "涨跌幅": "10.0", "换手率": "1.0",
              "上榜原因": "涨幅偏离值达7%", "龙虎榜净买额": "1.0e8",
              "龙虎榜买入额": "2.0e8", "龙虎榜卖出额": "1.0e8",
              "龙虎榜成交额": "3.0e8"}
    (row,) = map_lhb_basic_rows([record])
    assert row["trade_date"] == "20240105"  # 行级"上榜日"
    assert row["close_price"] == 1700.0
    assert row["lhb_reason"] == "涨幅偏离值达7%"
    assert row["net_buy_amount"] == 1.0e8


def test_lhb_statistic_mapping():
    record = {"最近上榜日": "2024-01-05", "代码": "600519", "名称": "贵州茅台",
              "上榜次数": "3", "龙虎榜净买额": "2.0e8", "龙虎榜买入额": "5.0e8",
              "龙虎榜卖出额": "3.0e8", "买方机构次数": "2", "卖方机构次数": "1"}
    (row,) = map_lhb_statistic_rows([record])
    assert row["trade_date"] == "20240105"  # 行级"最近上榜日"
    assert row["appear_count_3m"] == 3
    assert row["buy_seat_count"] == 2
    assert row["sell_seat_count"] == 1


def test_lhb_yyb_mapping():
    (cap,) = map_lhb_yyb_capital_rows(
        [{"序号": "1", "营业部名称": "机构专用",
          "累计参与金额": "1.0e9", "累计买入金额": "6.0e8"}])
    assert cap["rank"] == 1
    assert cap["seat_name"] == "机构专用"
    assert cap["total_amount"] == 1.0e9
    assert cap["buy_amount"] == 6.0e8
    (most,) = map_lhb_yyb_most_rows(
        [{"序号": "1", "营业部名称": "机构专用", "上榜次数": "10",
          "合计动用资金": "1.0e9", "年内买入股票只数": "5"}])
    assert most["appear_count"] == 10
    assert most["buy_amount"] == 1.0e9
    assert most["buy_count"] == 5


def test_efinance_rejects_board_sections_other_than_indices():
    from qstock_mcp.adapters.efinance_adapter import EfinanceAdapter

    ef = EfinanceAdapter()
    with pytest.raises(FetchError, match="不支持"):
        ef.fetch_boards("20240105")
    with pytest.raises(FetchError, match="不支持"):
        ef.fetch_zt_pool("20240105")
    with pytest.raises(FetchError, match="不支持"):
        ef.fetch_strong_stocks("20240105")
    with pytest.raises(FetchError, match="不支持"):
        ef.fetch_lhb("20240105")


def test_baostock_rejects_all_board_sections():
    bs = BaostockAdapter()
    for fn in (bs.fetch_indices, bs.fetch_boards, bs.fetch_zt_pool,
               bs.fetch_strong_stocks, bs.fetch_lhb):
        with pytest.raises(FetchError, match="不支持"):
            fn("20240105")
