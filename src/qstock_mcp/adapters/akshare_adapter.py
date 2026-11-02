"""akshare 适配器（清单/基本面承载源）。akshare 库懒加载，未安装时抛 FetchError。

承载面（issue #121 定稿，spec #111 剔东财收口）：
- 股票清单：交易所官网源 `stock_info_a_code_name`（清单链 akshare → baostock）；
- 基本面透传：新浪财务摘要（financial_abstract，幸存非东财）+ 百度股市通
  估值日序列（valuation，issue #115）。

东财承载面全部退役：本适配器曾覆盖的盘面五 section / 全市场快照 / 个股日线 /
指数日线 / 板块历史（spot_em、*_em 系列、push2his）均为东财接口，随
issue #121 从运行时代码摘除——相关方法保留为显式 `unsupported` 拒绝桩
（先例：个股日线 #112 退役桩），避免误接线后静默走回东财。
"""

import logging
from typing import NoReturn

from .base import FetchError, import_akshare, json_safe

log = logging.getLogger(__name__)


class AkshareAdapter:
    name = "akshare"

    # ---------------------------------------------------------------- 东财退役桩（issue #121）

    _RETIRED = "akshare 东财承载已退役（issue #121，spec #111 剔东财收口）"

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> NoReturn:
        """东财个股日线已退役（spec #111 / issue #112）。

        个股日线链路为 腾讯 → 新浪 → baostock；`stock_zh_a_hist`（东财 push2his）
        不再被调用。保留方法并显式拒绝，避免误接线后静默走回东财。
        """
        raise FetchError(
            f"{self._RETIRED}：stock_zh_a_hist（东财 push2his）不调用；"
            "个股日线链 腾讯→新浪→baostock（issue #112）",
            kind="unsupported",
        )

    def fetch_market_snapshot(self) -> NoReturn:
        raise FetchError(
            f"{self._RETIRED}：stock_zh_a_spot_em（东财 spot）不调用；"
            "全市场快照链 腾讯批量报价→腾讯rank→新浪直连（issue #116）",
            kind="unsupported",
        )

    def fetch_index_daily(self, index_code: str, start: str, end: str) -> NoReturn:
        raise FetchError(
            f"{self._RETIRED}：index_zh_a_hist（东财 push2his）不调用；"
            "指数日线链 腾讯→baostock（issue #117）",
            kind="unsupported",
        )

    def _retired_board(self, api: str, chain: str, ticket: str) -> NoReturn:
        raise FetchError(
            f"{self._RETIRED}：{api}（东财）不调用；承载链 {chain}（{ticket}）",
            kind="unsupported",
        )

    def fetch_indices(self, trade_date: str) -> NoReturn:
        self._retired_board("stock_zh_index_spot_em", "指数快照链 腾讯→新浪", "issue #117")

    def fetch_index_spot(self, index_code: str) -> NoReturn:
        self._retired_board("stock_zh_index_spot_em", "指数快照链 腾讯→新浪", "issue #117")

    def fetch_boards(self, trade_date: str) -> NoReturn:
        self._retired_board(
            "stock_board_industry/concept_name_em", "boards 行业段申万官网单源", "issue #118"
        )

    def fetch_zt_pool(self, trade_date: str) -> NoReturn:
        self._retired_board(
            "stock_zt_pool_*_em", "zt_pool 同花顺涨停揭秘单源（低频纪律）", "issue #120"
        )

    def fetch_strong_stocks(self, trade_date: str) -> NoReturn:
        self._retired_board("stock_zt_pool_strong_em", "strong_stocks 库内自算", "issue #120")

    def fetch_lhb(self, trade_date: str) -> NoReturn:
        self._retired_board(
            "stock_lhb_detail_em 等东财 lhb 函数", "lhb 交易所官方单源", "issue #114/#121"
        )

    # ---------------------------------------------------------------- 股票清单（issue #8）

    def fetch_stock_list(self) -> list[dict]:
        """股票清单：交易所官网源 stock_info_a_code_name（code/name 两列）。"""
        ak = import_akshare()
        try:
            df = ak.stock_info_a_code_name()
        except Exception as e:
            raise FetchError(f"akshare 股票清单抓取失败: {e}") from e
        if df is None or df.empty:
            raise FetchError("akshare 股票清单返回为空")
        return [
            {"stock_code": str(r["code"]), "stock_name": r.get("name")}
            for r in df.to_dict("records")
        ]

    # ---------------------------------------------------------------- 基本面透传（issue #6/#115）

    def fetch_fundamentals(self, stock_code: str) -> dict:
        """财务摘要 + 估值序列透传：section 名对应上游接口，字段名原样保留。

        financial_abstract 为新浪财务摘要（幸存，非东财）；valuation 为百度
        股市通估值日序列（issue #115，替代已被 akshare 移除的乐咕
        stock_a_indicator_lg——原 valuation_indicator section 随之退役）。
        section 级容错：拿到什么传什么（失败 section 记入 errors 键），
        全部无数据才抛 FetchError 触发 fallback。
        """
        ak = import_akshare()
        payload: dict = {}
        errors: list[str] = []
        try:
            df = ak.stock_financial_abstract(symbol=stock_code)
        except Exception as e:  # noqa: BLE001 - 单 section 失败不拖垮其他
            errors.append(f"financial_abstract: {e}")
        else:
            if df is not None and not df.empty:
                payload["financial_abstract"] = json_safe(df.to_dict("records"))
            else:
                errors.append("financial_abstract: 空返回")
        valuation, valuation_errors = _fetch_baidu_valuation(ak, stock_code)
        if valuation:
            payload["valuation"] = valuation
        errors.extend(f"valuation: {e}" for e in valuation_errors)
        if not payload:
            raise FetchError(
                f"akshare 基本面无数据（{stock_code}）: {'; '.join(errors) or '空返回'}"
            )
        if errors:
            log.warning("akshare 基本面部分 section 失败: %s", "; ".join(errors))
            payload["errors"] = errors
        return payload


# ---------------------------------------------------------------- 百度股市通估值（issue #115）

# stock_zh_valuation_baidu 的指标枚举（akshare docstring 定稿）；
# period 取近一年：单股单指标一次请求即全年日序列，与调研实测一致
#（docs/research/2026-10-20-fundamental-gaps.md）。
_BAIDU_VALUATION_INDICATORS = ("总市值", "市盈率(TTM)", "市盈率(静)", "市净率", "市现率")
_BAIDU_VALUATION_PERIOD = "近一年"


def _fetch_baidu_valuation(ak, stock_code: str) -> tuple[dict, list[str]]:
    """百度股市通估值日序列：逐指标抓取（date/value 列原样保留上游）。

    单指标失败只记 errors 不拖垮其他指标；全部指标无数据返回空 dict
    （由调用方记 valuation section 失败）。上游列为 date（日期）/value
    （数值），经 json_safe 序列化为 ISO 字符串/数值。
    """
    series: dict = {}
    errors: list[str] = []
    for indicator in _BAIDU_VALUATION_INDICATORS:
        try:
            df = ak.stock_zh_valuation_baidu(
                symbol=stock_code, indicator=indicator, period=_BAIDU_VALUATION_PERIOD
            )
        except Exception as e:  # noqa: BLE001 - 单指标失败不拖垮其他指标
            errors.append(f"{indicator}: {e}")
            continue
        if df is not None and not df.empty:
            series[indicator] = json_safe(df.to_dict("records"))
        else:
            errors.append(f"{indicator}: 空返回")
    return series, errors
