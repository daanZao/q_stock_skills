"""akshare 适配器（fallback 第 2 位）。akshare 库懒加载，未安装时抛 FetchError。

盘面快照（issue #5）：五个 section 全部由东财接口提供（旧项目 appdb 实测写入口径）。
zt_pool/lhb 内部子项（池类型/子表）容忍部分失败：拿到什么落什么，全部失败才抛
FetchError 触发 fallback；盘中 lhb_basic 空数据归一化为空列表（盘后发布，非错误）。
"""

import logging

from ._board_em import (
    map_board_rows,
    map_index_rows,
    map_lhb_basic_rows,
    map_lhb_statistic_rows,
    map_lhb_yyb_capital_rows,
    map_lhb_yyb_most_rows,
    map_strong_rows,
    map_zt_pool_rows,
)
from ._eastmoney import map_eastmoney_rows, map_spot_rows
from .base import FetchError, json_safe

log = logging.getLogger(__name__)

_ADJUST = {"qfq": "qfq", "hfq": "hfq", "none": ""}  # akshare 复权参数


def _ak():
    try:
        import akshare as ak
    except ImportError as e:
        raise FetchError("akshare 未安装：pip install qstock-mcp[sources]") from e
    return ak


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

    # ---------------------------------------------------------------- 基本面透传（issue #6）

    def fetch_fundamentals(self, stock_code: str) -> dict:
        """财务摘要 + 估值指标透传：section 名对应上游接口，字段名原样保留。

        section 级容错：拿到什么传什么（失败 section 记入 errors 键），
        全部无数据才抛 FetchError 触发 fallback。
        """
        ak = _ak()
        payload: dict = {}
        errors: list[str] = []
        for section, call in [
            ("financial_abstract", lambda: ak.stock_financial_abstract(symbol=stock_code)),
            ("valuation_indicator", lambda: ak.stock_a_indicator_lg(symbol=stock_code)),
        ]:
            try:
                df = call()
            except Exception as e:  # noqa: BLE001 - 单 section 失败不拖垮其他
                errors.append(f"{section}: {e}")
                continue
            if df is not None and not df.empty:
                payload[section] = json_safe(df.to_dict("records"))
            else:
                errors.append(f"{section}: 空返回")
        if not payload:
            raise FetchError(
                f"akshare 基本面无数据（{stock_code}）: {'; '.join(errors) or '空返回'}"
            )
        if errors:
            log.warning("akshare 基本面部分 section 失败: %s", "; ".join(errors))
            payload["errors"] = errors
        return payload

    # ---------------------------------------------------------------- 盘面快照（issue #5）

    def fetch_indices(self, trade_date: str) -> list[dict]:
        ak = _ak()
        try:
            df = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
        except Exception as e:
            raise FetchError(f"akshare 指数抓取失败: {e}") from e
        if df is None or df.empty:
            raise FetchError("akshare 沪深重要指数返回为空")
        return map_index_rows(df.to_dict("records"))

    def fetch_boards(self, trade_date: str) -> list[dict]:
        ak = _ak()
        rows = []
        for board_type, fn in [
            ("industry", ak.stock_board_industry_name_em),
            ("concept", ak.stock_board_concept_name_em),
        ]:
            try:
                df = fn()
            except Exception as e:
                raise FetchError(f"akshare {board_type} 板块抓取失败: {e}") from e
            if df is None or df.empty:
                raise FetchError(f"akshare {board_type} 板块返回为空")
            rows += map_board_rows(df.to_dict("records"), board_type)
        return rows

    def fetch_zt_pool(self, trade_date: str) -> list[dict]:
        """涨/跌停/炸板池：单池失败记录并继续，三池全失败才抛 FetchError。"""
        ak = _ak()
        rows, errors = [], []
        for pool_type, api_name in [
            ("zt", "stock_zt_pool_em"),
            ("dt", "stock_zt_pool_dtgc_em"),
            ("zb", "stock_zt_pool_zbgc_em"),
        ]:
            try:
                df = getattr(ak, api_name)(date=trade_date)
            except Exception as e:  # noqa: BLE001 - 单池失败不拖垮其他池
                errors.append(f"{pool_type}: {e}")
                continue
            if df is None:
                continue
            rows += map_zt_pool_rows(df.to_dict("records"), pool_type)
        if not rows and errors:
            raise FetchError(f"akshare 涨跌停池抓取失败: {'; '.join(errors)}")
        if errors:
            log.warning("akshare 涨跌停池部分失败: %s", "; ".join(errors))
        return rows

    def fetch_strong_stocks(self, trade_date: str) -> list[dict]:
        ak = _ak()
        try:
            df = ak.stock_zt_pool_strong_em(date=trade_date)
        except Exception as e:
            raise FetchError(f"akshare 强势股池抓取失败: {e}") from e
        if df is None or df.empty:
            return []
        return map_strong_rows(df.to_dict("records"))

    def fetch_lhb(self, trade_date: str) -> dict:
        """龙虎榜四子表：子项容忍部分失败，全部失败才抛 FetchError。

        部分失败时返回 dict 带 "errors" 键（失败子项原因列表），工具层据此
        报告 partial_error；四子表键不受影响。
        """
        result: dict[str, list] = {}
        errors = []
        for table, fn in [
            ("lhb_basic", self._lhb_basic),
            ("lhb_stock_statistic", self._lhb_statistic),
            ("lhb_yyb_capital", self._lhb_yyb_capital),
            ("lhb_yyb_most", self._lhb_yyb_most),
        ]:
            try:
                result[table] = fn(trade_date)
            except FetchError as e:
                errors.append(f"{table}: {e}")
                result[table] = []
        if errors and not any(result.values()):
            raise FetchError(f"akshare 龙虎榜抓取失败: {'; '.join(errors)}")
        if errors:
            log.warning("akshare 龙虎榜部分子项失败: %s", "; ".join(errors))
            result["errors"] = errors
        return result

    def _lhb_basic(self, trade_date: str) -> list[dict]:
        ak = _ak()
        try:
            df = ak.stock_lhb_detail_em(start_date=trade_date, end_date=trade_date)
        except TypeError as e:
            # akshare 上游在当日无龙虎榜数据（盘中/非交易日）时内部抛
            # TypeError: 'NoneType' object is not subscriptable，按空数据处理
            if "NoneType" in str(e):
                return []
            raise FetchError(f"akshare 龙虎榜抓取失败: {e}") from e
        except Exception as e:
            raise FetchError(f"akshare 龙虎榜抓取失败: {e}") from e
        if df is None or df.empty:
            return []  # 盘中空数据是正常语义（盘后发布）
        return map_lhb_basic_rows(df.to_dict("records"))

    def _lhb_statistic(self, trade_date: str) -> list[dict]:
        # 接口为近三月滚动统计，无日期参数；行级"最近上榜日"在映射层写入 trade_date
        ak = _ak()
        try:
            df = ak.stock_lhb_stock_statistic_em(symbol="近三月")
        except Exception as e:
            raise FetchError(f"akshare 龙虎榜统计抓取失败: {e}") from e
        if df is None or df.empty:
            return []
        return map_lhb_statistic_rows(df.to_dict("records"))

    def _lhb_yyb_capital(self, trade_date: str) -> list[dict]:
        # 接口为当前累计资金榜，无日期参数；fetch_date 由工具层记执行日
        ak = _ak()
        try:
            df = ak.stock_lh_yyb_capital()
        except Exception as e:
            raise FetchError(f"akshare 营业部资金榜抓取失败: {e}") from e
        if df is None or df.empty:
            return []
        return map_lhb_yyb_capital_rows(df.to_dict("records"))

    def _lhb_yyb_most(self, trade_date: str) -> list[dict]:
        ak = _ak()
        try:
            df = ak.stock_lh_yyb_most()
        except Exception as e:
            raise FetchError(f"akshare 营业部排行抓取失败: {e}") from e
        if df is None or df.empty:
            return []
        return map_lhb_yyb_most_rows(df.to_dict("records"))
