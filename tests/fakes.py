"""工具层测试共用的 fake 数据源：按工作日生成确定性日线行。

工作日近似交易日，足以驱动头尾分段与自愈逻辑；数据本身不参与断言数值正确性。

快照 fake（issue #4）：SNAPSHOT_ROWS 为两行确定性全市场快照，
FakeSnapshotAdapter 记录调用次数、按脚本成败。
"""

from datetime import datetime, timedelta

from qstock_mcp.adapters import FetchError


def weekday_rows(start: str, end: str, close: float = 10.0) -> list[dict]:
    rows = []
    d = datetime.strptime(start, "%Y%m%d").date()
    last = datetime.strptime(end, "%Y%m%d").date()
    while d <= last:
        if d.weekday() < 5:
            rows.append(
                {
                    "trade_date": d.strftime("%Y%m%d"),
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 100,
                    "amount": 1000.0,
                    "amplitude": 1.0,
                    "change_percent": 0.5,
                    "change_amount": 0.05,
                    "turnover_rate": 1.0,
                }
            )
        d += timedelta(days=1)
    return rows


class FakeAdapter:
    """记录调用分段、按脚本成败的 fake 数据源。

    rows 为 None 时返回请求分段的工作日行；否则固定返回 rows（可为 []）。
    """

    def __init__(self, name, *, fail_times=0, rows=None):
        self.name = name
        self._fail_times = fail_times
        self._rows = rows
        self.calls = []

    def fetch_daily(self, stock_code, start, end, adj="qfq"):
        self.calls.append({"start": start, "end": end, "adj": adj})
        if len(self.calls) <= self._fail_times:
            raise FetchError(f"{self.name} boom")
        return self._rows if self._rows is not None else weekday_rows(start, end)


def _snapshot_row(stock_code: str, stock_name: str, close: float) -> dict:
    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "latest_price": close,
        "change_percent": 0.5,
        "change_amount": 0.05,
        "amplitude": 1.0,
        "high": close,
        "low": close,
        "open": close,
        "pre_close": close - 0.05,
        "volume_ratio": 1.2,
        "turnover_rate": 1.0,
        "pe_ratio": 20.0,
        "pb_ratio": 2.0,
        "volume": 100,
        "amount": 1000.0,
        "market_cap": 1.0e9,
        "float_cap": 8.0e8,
    }


SNAPSHOT_ROWS = [
    _snapshot_row("600519", "贵州茅台", 1700.0),
    _snapshot_row("000001", "平安银行", 10.0),
]


class FakeSnapshotAdapter:
    """记录调用次数、按脚本成败的 fake 全市场快照源。

    trade_date 模拟 API 报告的最新交易日；None 表示 API 未报告。
    rows 为 None 时返回 SNAPSHOT_ROWS。
    """

    def __init__(self, name, *, fail_times=0, rows=None, trade_date=None):
        self.name = name
        self._fail_times = fail_times
        self._rows = rows
        self._trade_date = trade_date
        self.calls = 0

    def fetch_market_snapshot(self):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise FetchError(f"{self.name} boom")
        return {
            "trade_date": self._trade_date,
            "rows": self._rows if self._rows is not None else SNAPSHOT_ROWS,
        }


# ---------------------------------------------------------------- 盘面快照（issue #5）

INDEX_ROWS = [
    {
        "index_code": "000001",
        "index_name": "上证指数",
        "latest_price": 3000.0,
        "change_percent": 0.5,
        "change_amount": 15.0,
        "volume": 100,
        "amount": 1.0e9,
        "amplitude": 1.0,
        "high": 3010.0,
        "low": 2990.0,
        "open": 2995.0,
        "pre_close": 2985.0,
        "volume_ratio": 1.1,
    }
]

BOARD_ROWS = [
    {
        "board_type": "industry",
        "board_code": "BK0477",
        "board_name": "酿酒行业",
        "latest_price": 2000.0,
        "change_percent": 1.5,
        "change_amount": 30.0,
        "leading_stock": "贵州茅台",
        "leading_change": 2.0,
    },
    {
        "board_type": "concept",
        "board_code": "BK0816",
        "board_name": "白酒概念",
        "latest_price": 1500.0,
        "change_percent": 1.2,
        "change_amount": 18.0,
        "leading_stock": "贵州茅台",
        "leading_change": 2.0,
    },
]

ZT_POOL_ROWS = [
    {
        "pool_type": "zt",
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "latest_price": 1700.0,
        "change_percent": 10.0,
        "amount": 1.0e9,
        "industry": "酿酒行业",
        "limit_up_time": "093000",
        "consecutive_boards": 1,
    }
]

STRONG_ROWS = [
    {
        "stock_code": "000001",
        "stock_name": "平安银行",
        "latest_price": 10.0,
        "change_percent": 5.0,
        "amount": 5.0e8,
        "turnover_rate": 3.0,
        "market_cap": 2.0e11,
        "industry": "银行",
        "reason": "放量上涨",
    }
]

LHB_ROWS = {
    "lhb_basic": [
        {
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "close_price": 1700.0,
            "change_percent": 10.0,
            "turnover_rate": 1.0,
            "lhb_reason": "涨幅偏离值达7%",
            "net_buy_amount": 1.0e8,
            "buy_amount": 2.0e8,
            "sell_amount": 1.0e8,
            "total_amount": 3.0e8,
        }
    ],
    "lhb_stock_statistic": [
        {
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "appear_count_3m": 3,
            "buy_amount_3m": 5.0e8,
            "sell_amount_3m": 3.0e8,
            "net_buy_3m": 2.0e8,
            "buy_seat_count": 2,
            "sell_seat_count": 1,
        }
    ],
    "lhb_yyb_capital": [
        {"rank": 1, "seat_name": "机构专用", "total_amount": 1.0e9, "buy_amount": 6.0e8}
    ],
    "lhb_yyb_most": [
        {
            "rank": 1,
            "seat_name": "机构专用",
            "appear_count": 10,
            "buy_amount": 1.0e9,
            "buy_count": 5,
        }
    ],
}

_BOARD_DEFAULTS = {
    "indices": INDEX_ROWS,
    "boards": BOARD_ROWS,
    "zt_pool": ZT_POOL_ROWS,
    "strong_stocks": STRONG_ROWS,
    "lhb": LHB_ROWS,
}


class FakeBoardAdapter:
    """记录调用、按脚本成败的 fake 盘面数据源。

    fail_sections: 指定 section 永远失败；fail_times: 前 N 次调用（不分 section）失败。
    rows: {section: rows} 覆盖默认返回；lhb 的 rows 为 {表名: [row]} 字典。
    """

    def __init__(self, name, *, fail_times=0, fail_sections=(), rows=None):
        self.name = name
        self._fail_times = fail_times
        self._fail_sections = set(fail_sections)
        self._rows = rows or {}
        self.calls = []

    def _get(self, section):
        self.calls.append(section)
        if section in self._fail_sections or len(self.calls) <= self._fail_times:
            raise FetchError(f"{self.name} {section} boom")
        return self._rows.get(section, _BOARD_DEFAULTS[section])

    def fetch_indices(self, trade_date):
        return self._get("indices")

    def fetch_boards(self, trade_date):
        return self._get("boards")

    def fetch_zt_pool(self, trade_date):
        return self._get("zt_pool")

    def fetch_strong_stocks(self, trade_date):
        return self._get("strong_stocks")

    def fetch_lhb(self, trade_date):
        return self._get("lhb")
