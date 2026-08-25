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
