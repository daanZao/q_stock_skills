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

# 盘面快照（issue #5）：各 section 行 dict 的键集，与落库表列一一对应（日期列除外，
# 见 repository.BOARD_SECTION_TABLES）。行可自带日期键（trade_date/fetch_date）覆盖
# 工具层传入的日期（如 lhb_basic 的"上榜日"、efinance 指数的"最新交易日"）。
INDEX_FIELDS = (
    "index_code",
    "index_name",
    "latest_price",
    "change_percent",
    "change_amount",
    "volume",
    "amount",
    "amplitude",
    "high",
    "low",
    "open",
    "pre_close",
    "volume_ratio",
)

BOARD_FIELDS = (
    "board_type",
    "board_code",
    "board_name",
    "latest_price",
    "change_percent",
    "change_amount",
    "volume",
    "amount",
    "amplitude",
    "high",
    "low",
    "open",
    "pre_close",
    "volume_ratio",
    "stock_count",
    "leading_stock",
    "leading_change",
)

ZT_POOL_FIELDS = (
    "pool_type",
    "stock_code",
    "stock_name",
    "latest_price",
    "change_percent",
    "zt_price",
    "volume",
    "amount",
    "limit_up_time",
    "limit_up_type",
    "consecutive_boards",
    "industry",
    "dt_price",
    "zb_info",
)

STRONG_FIELDS = (
    "stock_code",
    "stock_name",
    "latest_price",
    "change_percent",
    "volume",
    "amount",
    "turnover_rate",
    "market_cap",
    "consecutive_boards",
    "industry",
    "reason",
)

LHB_BASIC_FIELDS = (
    "stock_code",
    "stock_name",
    "close_price",
    "change_percent",
    "turnover_rate",
    "lhb_reason",
    "net_buy_amount",
    "buy_amount",
    "sell_amount",
    "total_amount",
)

LHB_STATISTIC_FIELDS = (
    "stock_code",
    "stock_name",
    "appear_count_3m",
    "buy_amount_3m",
    "sell_amount_3m",
    "net_buy_3m",
    "buy_seat_count",
    "sell_seat_count",
)

LHB_YYB_CAPITAL_FIELDS = (
    "rank",
    "seat_name",
    "total_amount",
    "buy_amount",
    "sell_amount",
    "net_buy_amount",
    "avg_amount_per_trade",
)

LHB_YYB_MOST_FIELDS = (
    "rank",
    "seat_name",
    "appear_count",
    "buy_amount",
    "buy_count",
    "sell_amount",
    "sell_count",
    "net_buy_amount",
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


class BoardAdapter(Protocol):
    """盘面快照（issue #5）协议：按 section 抓取，trade_date 为 yyyymmdd。

    前四个方法返回行 dict 列表（键见 *_FIELDS）；fetch_lhb 返回
    {"lhb_basic": [...], "lhb_stock_statistic": [...], "lhb_yyb_capital": [...],
     "lhb_yyb_most": [...]}；子项部分失败时额外带 "errors" 键（原因列表），
    工具层据此报告 partial_error。盘中/非交易日 lhb_basic 为空列表是正常语义
    （盘后发布），适配器应将其归一化为空列表而非抛错；不支持某 section 的源
    抛 FetchError。
    """

    name: str

    def fetch_indices(self, trade_date: str) -> list[dict]: ...
    def fetch_boards(self, trade_date: str) -> list[dict]: ...
    def fetch_zt_pool(self, trade_date: str) -> list[dict]: ...
    def fetch_strong_stocks(self, trade_date: str) -> list[dict]: ...
    def fetch_lhb(self, trade_date: str) -> dict: ...


class DataAdapter(DailyAdapter, SnapshotAdapter, BoardAdapter, Protocol):
    """同时支持日线、全市场快照与盘面快照的适配器（default_adapters 链的元素类型）。"""


def is_bse_code(stock_code: str) -> bool:
    """北交所代码：4/8/9 开头（baostock 不支持，需明确拒绝）。"""
    return stock_code[:1] in ("4", "8", "9")
