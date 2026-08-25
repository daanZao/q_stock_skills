"""数据源适配层接缝：协议 + 错误类型 + 代码工具。

真实适配器（efinance/akshare/baostock）与测试注入的 fake 都实现同一协议：
    name: str
    fetch_daily(stock_code, start, end, adj) -> list[bar]
bar 为 dict，键见 BAR_FIELDS；trade_date 为 yyyymmdd 字符串，数值缺失为 None。
失败一律抛 FetchError；返回空列表表示该区间无数据（停牌/非交易日），不是错误。
"""

from typing import Protocol

BAR_FIELDS = (
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "change_percent",
    "change_amount",
    "turnover_rate",
)


class FetchError(RuntimeError):
    """单个数据源抓取失败（库未安装、网络错误、接口报错、代码不支持等）。"""


class DailyAdapter(Protocol):
    name: str

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]: ...


def is_bse_code(stock_code: str) -> bool:
    """北交所代码：4/8/9 开头（baostock 不支持，需明确拒绝）。"""
    return stock_code[:1] in ("4", "8", "9")
