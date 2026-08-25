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
