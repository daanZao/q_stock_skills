"""efinance 适配器（fallback 第 1 位）。efinance 库懒加载，未安装时抛 FetchError。

盘面快照（issue #5）：仅支持 indices（沪深系列指数），其余 section 明确拒绝。"""

from typing import NoReturn

from ._board_em import map_index_rows
from ._eastmoney import map_eastmoney_rows, map_spot_rows, spot_trade_date
from .base import FetchError, json_safe

_FQT = {"qfq": 1, "hfq": 2, "none": 0}  # efinance 复权参数


def _ef():
    try:
        import efinance as ef
    except ImportError as e:
        raise FetchError("efinance 未安装：pip install qstock-mcp[sources]") from e
    return ef


class EfinanceAdapter:
    name = "efinance"

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]:
        ef = _ef()
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
        ef = _ef()
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
        ef = _ef()
        try:
            df = ef.stock.get_realtime_quotes(["沪深系列指数"])
        except Exception as e:
            raise FetchError(f"efinance 指数抓取失败: {e}") from e
        if df is None or df.empty:
            raise FetchError("efinance 沪深系列指数返回为空")
        return map_index_rows(df.to_dict("records"))

    # ---------------------------------------------------------------- init 轻量初始化（issue #8）

    def fetch_stock_list(self) -> list[dict]:
        """股票清单：efinance 无专用清单接口，退化为全市场快照取代码/名称。"""
        ef = _ef()
        try:
            df = ef.stock.get_realtime_quotes()
        except Exception as e:
            raise FetchError(f"efinance 股票清单抓取失败: {e}") from e
        if df is None or df.empty:
            raise FetchError("efinance 全市场快照返回为空")
        rows = [
            {"stock_code": r["stock_code"], "stock_name": r["stock_name"]}
            for r in map_spot_rows(df.to_dict("records"))
            if r["stock_code"]
        ]
        if not rows:
            raise FetchError("efinance 股票清单为空")
        return rows

    def fetch_index_daily(self, index_code: str, start: str, end: str) -> list[dict]:
        """efinance 无干净的指数历史接口，明确拒绝（不伪造）。"""
        raise FetchError("efinance 不支持指数日线")

    # ---------------------------------------------------------------- 基本面透传（issue #6）

    def fetch_fundamentals(self, stock_code: str) -> dict:
        """基础/估值快照透传（get_base_info 单行），字段名原样保留上游。"""
        ef = _ef()
        try:
            info = ef.stock.get_base_info(stock_code)
        except Exception as e:
            raise FetchError(f"efinance 基本面抓取失败: {e}") from e
        if info is None or len(info) == 0:
            raise FetchError(f"efinance 基本面无数据（{stock_code}）")
        return {"base_info": json_safe(dict(info))}

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
