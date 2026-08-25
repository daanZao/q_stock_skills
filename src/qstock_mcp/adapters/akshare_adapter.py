"""akshare 适配器（fallback 第 2 位）。akshare 库懒加载，未安装时抛 FetchError。"""

from ._eastmoney import map_eastmoney_rows
from .base import FetchError

_ADJUST = {"qfq": "qfq", "hfq": "hfq", "none": ""}  # akshare 复权参数


class AkshareAdapter:
    name = "akshare"

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]:
        try:
            import akshare as ak
        except ImportError as e:
            raise FetchError("akshare 未安装：pip install qstock-mcp[sources]") from e
        try:
            df = ak.stock_zh_a_hist(
                symbol=stock_code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust=_ADJUST.get(adj, "qfq"),
            )
        except Exception as e:
            raise FetchError(f"akshare 抓取失败: {e}") from e
        if df is None or df.empty:
            return []
        return map_eastmoney_rows(df.to_dict("records"))
