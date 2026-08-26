"""efinance 适配器（fallback 第 1 位）。efinance 库懒加载，未安装时抛 FetchError。

盘面快照（issue #5）：仅支持 indices（沪深系列指数），其余 section 明确拒绝。"""

from typing import NoReturn

from ._board_em import map_index_rows
from ._eastmoney import map_eastmoney_rows, map_spot_rows, spot_trade_date
from .base import FetchError

_FQT = {"qfq": 1, "hfq": 2, "none": 0}  # efinance 复权参数


class EfinanceAdapter:
    name = "efinance"

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]:
        try:
            import efinance as ef
        except ImportError as e:
            raise FetchError("efinance 未安装：pip install qstock-mcp[sources]") from e
        try:
            df = ef.stock.get_quote_history(
                stock_codes=stock_code, beg=start, end=end, klt=101, fqt=_FQT.get(adj, 1)
            )
        except Exception as e:
            raise FetchError(f"efinance 抓取失败: {e}") from e
        if df is None or df.empty:
            return []
        return map_eastmoney_rows(df.to_dict("records"))

    def fetch_market_snapshot(self) -> dict:
        """单次全市场快照；交易日取自"最新交易日"列。"""
        try:
            import efinance as ef
        except ImportError as e:
            raise FetchError("efinance 未安装：pip install qstock-mcp[sources]") from e
        try:
            df = ef.stock.get_realtime_quotes()
        except Exception as e:
            raise FetchError(f"efinance 快照抓取失败: {e}") from e
        if df is None or df.empty:
            return {"trade_date": None, "rows": []}
        records = df.to_dict("records")
        return {"trade_date": spot_trade_date(records), "rows": map_spot_rows(records)}

    def fetch_indices(self, trade_date: str) -> list[dict]:
        """沪深系列指数快照；行级"最新交易日"在映射层写入 trade_date。"""
        try:
            import efinance as ef
        except ImportError as e:
            raise FetchError("efinance 未安装：pip install qstock-mcp[sources]") from e
        try:
            df = ef.stock.get_realtime_quotes(["沪深系列指数"])
        except Exception as e:
            raise FetchError(f"efinance 指数抓取失败: {e}") from e
        if df is None or df.empty:
            raise FetchError("efinance 沪深系列指数返回为空")
        return map_index_rows(df.to_dict("records"))

    def _unsupported(self, section: str) -> NoReturn:
        raise FetchError(f"efinance 不支持盘面 section: {section}")

    def fetch_boards(self, trade_date: str) -> list[dict]:
        self._unsupported("boards")

    def fetch_zt_pool(self, trade_date: str) -> list[dict]:
        self._unsupported("zt_pool")

    def fetch_strong_stocks(self, trade_date: str) -> list[dict]:
        self._unsupported("strong_stocks")

    def fetch_lhb(self, trade_date: str) -> dict:
        self._unsupported("lhb")
