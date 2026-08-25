"""工具层测试共用的 fake 数据源：按工作日生成确定性日线行。

工作日近似交易日，足以驱动头尾分段与自愈逻辑；数据本身不参与断言数值正确性。
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
