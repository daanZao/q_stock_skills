"""数据源适配层接缝：协议 + 错误类型 + 代码工具。

真实适配器（efinance/akshare/baostock）与测试注入的 fake 都实现同一协议：
    name: str
    fetch_daily(stock_code, start, end, adj) -> list[bar]
bar 为 dict，键见 BAR_FIELDS；trade_date 为 yyyymmdd 字符串，数值缺失为 None。
失败一律抛 FetchError；返回空列表表示该区间无数据（停牌/非交易日），不是错误。

全市场快照（issue #4）走另一协议：fetch_market_snapshot() 一次调用返回
{"trade_date": str | None, "rows": [snapshot]}；trade_date 为 None 表示 API 未报告，
由工具层回退到传入日期/当天。snapshot 键见 SNAPSHOT_FIELDS。
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

SNAPSHOT_FIELDS = (
    "stock_code",
    "stock_name",
    "latest_price",
    "change_percent",
    "change_amount",
    "amplitude",
    "high",
    "low",
    "open",
    "pre_close",
    "volume_ratio",
    "turnover_rate",
    "pe_ratio",
    "pb_ratio",
    "volume",
    "amount",
    "market_cap",
    "float_cap",
)


class FetchError(RuntimeError):
    """单个数据源抓取失败（库未安装、网络错误、接口报错、代码不支持等）。"""


class DailyAdapter(Protocol):
    name: str

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]: ...


class SnapshotAdapter(Protocol):
    name: str

    def fetch_market_snapshot(self) -> dict:
        """单次全市场快照 → {"trade_date": str | None, "rows": [snapshot dict]}。"""
        ...


class DataAdapter(DailyAdapter, SnapshotAdapter, Protocol):
    """同时支持日线与全市场快照的适配器（default_adapters 链的元素类型）。"""


def is_bse_code(stock_code: str) -> bool:
    """北交所代码：4/8/9 开头（baostock 不支持，需明确拒绝）。"""
    return stock_code[:1] in ("4", "8", "9")
