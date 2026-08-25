"""真实适配器的纯逻辑接缝测试：列映射与代码规则，不触网、不需要安装第三方库。

契约（issue #3）：baostock 兜底时拒绝北交所代码（4/8/9 开头）；缺列
（amplitude/turnover 等）置 null；缺失值不伪造。
"""

import pytest

from qstock_mcp.adapters._eastmoney import map_eastmoney_rows
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
