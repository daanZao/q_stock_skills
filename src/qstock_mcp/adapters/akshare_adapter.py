"""akshare 适配器（fallback 第 2 位）。akshare 库懒加载，未安装时抛 FetchError。"""

from ._eastmoney import map_eastmoney_rows, map_spot_rows
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

    def fetch_market_snapshot(self) -> dict:
        """单次全市场快照；spot_em 无交易日列，trade_date 为 None 由工具层回退。"""
        try:
            import akshare as ak
        except ImportError as e:
            raise FetchError("akshare 未安装：pip install qstock-mcp[sources]") from e
        try:
            df = ak.stock_zh_a_spot_em()
        except Exception as e:
            raise FetchError(f"akshare 快照抓取失败: {e}") from e
        if df is None or df.empty:
            return {"trade_date": None, "rows": []}
        return {"trade_date": None, "rows": map_spot_rows(df.to_dict("records"))}
