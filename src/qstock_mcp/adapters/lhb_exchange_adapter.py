"""交易所官方龙虎榜适配器（issue #114，spec #111）：沪深北三所一手披露。

主源组合（#97/#107 调研实测）：
- 深交所 ShowReport JSON（零鉴权；汇总翻页 + 个股席位明细）
- 上交所 showTradePublicFile.do 定宽文本（自写解析器，需 Referer）
- 北交所 bseinfo.net JSONP（curl_cffi TLS 指纹；**不走 bse.cn** 的 JS 挑战）

lhb_basic 金额口径：buy/sell/net 由席位明细自算（买前五合计/卖前五合计/差，
东财同口径），total_amount = buy + sell（与存量行口径一致）；全日成交金额
（万元）随行携带 day_amount 元数据键供对拍使用（repository 落库忽略非列键）。
close_price/change_percent 官方汇总不提供（偏离值≠涨跌幅，不伪造置 null；
可与 stock_daily join 补齐）；换手率仅在披露原因为"换手率达到X%"时从文本提取。
lhb_stock_statistic：akshare 新浪 ggtj（近 60 日窗口，万元原生，trade_date=
采集执行日的快照语义）。lhb_yyb_*：akshare 同花顺接口（非东财，#97 勘误幸存）
——子项委托原 akshare 适配器映射，保持四表契约。

子项容错：单所/单个子项失败记入 errors（工具层报 partial_error），拿到什么落
什么；basic/statistic/yyb 全空且全部失败才抛 FetchError 触发链上回退
（default_lhb_adapters 交易所官方单源，issue #121 摘除东财 legacy 兜底；
新浪互补校验走 `qstock-scheduler lhb --check` 对拍，不落库）。
盘中/非交易日三所全空是正常语义（rows 0 + empty_kind，非失败）。
"""

import logging
import re
import time

from . import _lhb_bse, _lhb_sse, _lhb_szse
from ._bse_codes import normalize_stock_code
from ._lhb_yyb import map_lhb_yyb_capital_rows, map_lhb_yyb_most_rows
from .base import FetchError, import_akshare
from ..dates import dash_date

log = logging.getLogger(__name__)

# 席位明细请求间礼貌间隔（秒）；每日量级 ≈ 1 汇总 + N 明细（几十次请求/日）
DETAIL_PAUSE_S = 0.2

_TURNOVER_RE = re.compile(r"换手率达到\s*([-\d.]+)\s*%")

#  Seat 抓取最小协议：adapters 注入用（测试以 fake 实现，不触网）
SZSE_CATALOG = "1842_xxpl"


def _sum_yuan(seats: list[dict]) -> float | None:
    """席位列表（yuan 键）合计 → 万元；全空返回 None（不伪造）。"""
    vals = [s["yuan"] for s in seats if s.get("yuan") is not None]
    if not vals:
        return None
    return sum(vals) / 1e4


def _basic_row(trade_date, code, name, reason, buy_seats, sell_seats,
               day_amount=None, seat_day_amount=None):
    """席位明细 → lhb_basic 行（万元口径 + day_amount 对拍元数据键）。"""
    buy = _sum_yuan(buy_seats)
    sell = _sum_yuan(sell_seats)
    net = None if (buy is None and sell is None) else (buy or 0.0) - (sell or 0.0)
    m = _TURNOVER_RE.search(reason or "")
    return {
        "trade_date": trade_date,
        "stock_code": code,
        "stock_name": name,
        "close_price": None,
        "change_percent": None,
        "turnover_rate": float(m.group(1)) if m else None,
        "lhb_reason": reason or "",
        "net_buy_amount": net,
        "buy_amount": buy,
        "sell_amount": sell,
        "total_amount": None if (buy is None and sell is None) else (buy or 0.0) + (sell or 0.0),
        # 对拍元数据（非落库列）：上游给出的全日成交金额（万元）
        "day_amount": day_amount if day_amount is not None else seat_day_amount,
    }


class ExchangeLhbAdapter:
    """沪深北交易所官方龙虎榜适配器（BoardAdapter.fetch_lhb 契约，四表全键）。"""

    name = "exchange"

    def __init__(self, *, pause_s: float = DETAIL_PAUSE_S, clock=time.sleep):
        self._pause = pause_s
        self._clock = clock

    # ---------------------------------------------------------------- 入口

    def fetch_lhb(self, trade_date: str) -> dict:
        """四子表抓取；子项/单所级失败记入 errors（工具层报 partial_error），
        拿到什么落什么；basic/statistic/yyb 全空且全部失败才抛 FetchError。"""
        result: dict[str, list] = {}
        errors: list[str] = []
        try:
            result["lhb_basic"], basic_errs = self._fetch_basic(trade_date)
            errors += basic_errs
        except Exception as e:  # noqa: BLE001 - 单子项失败不拖垮其他
            errors.append(f"lhb_basic: {e}")
            result["lhb_basic"] = []
        for table, fn in [
            ("lhb_stock_statistic", self._fetch_statistic),
            ("lhb_yyb_capital", self._fetch_yyb_capital),
            ("lhb_yyb_most", self._fetch_yyb_most),
        ]:
            try:
                result[table] = fn(trade_date)
            except Exception as e:  # noqa: BLE001 - 单子项失败不拖垮其他
                errors.append(f"{table}: {e}")
                result[table] = []
        if errors and not any(result.values()):
            raise FetchError(f"交易所龙虎榜抓取失败: {'; '.join(errors)}")
        if errors:
            log.warning("交易所龙虎榜部分子项失败: %s", "; ".join(errors))
            result["errors"] = errors
        return result

    # ---------------------------------------------------------------- lhb_basic

    def _fetch_basic(self, trade_date: str) -> tuple[list[dict], list[str]]:
        rows: list[dict] = []
        errors: list[str] = []
        for market, fn in [
            ("szse", self._szse_basic),
            ("sse", self._sse_basic),
            ("bse", self._bse_basic),
        ]:
            try:
                rows += fn(trade_date)
            except Exception as e:  # noqa: BLE001 - 单所失败不拖垮其他所
                errors.append(f"{market}: {e}")
        if errors and not rows:
            raise FetchError(f"lhb_basic 三所均失败: {'; '.join(errors)}")
        if errors:
            log.warning("lhb_basic 部分市场失败: %s", "; ".join(errors))
        return rows, errors

    def _szse_basic(self, trade_date: str) -> list[dict]:
        """深交所：汇总翻页 + 个股席位明细。"""
        dash = dash_date(trade_date)
        first = _lhb_szse.fetch_summary(dash, dash, page=1)
        pages = [first]
        for page in range(2, first["pagecount"] + 1):
            self._clock(self._pause)
            pages.append(_lhb_szse.fetch_summary(dash, dash, page=page))
        rows = []
        for pg in pages:
            for r in pg["rows"]:
                if not r["zqdm"]:
                    continue
                if r["zbdm"] is None:
                    # 实测个别榜单类型（如 301313）汇总行 bz 链接无 ZBDM 参数：
                    # 跳过该股并告警——单行形状异常不拖垮整个深市（拿到什么落什么）
                    log.warning(
                        "深交所汇总行缺 ZBDM，跳过该股明细（%s %s）", r["zqdm"], dash
                    )
                    continue
                self._clock(self._pause)
                d = _lhb_szse.fetch_detail(dash, r["zqdm"], r["zbdm"])
                rows.append(
                    _basic_row(
                        d["trade_date"] or trade_date,
                        r["zqdm"],
                        d["stock_name"] or r["zqjc"],
                        r["plyy"],
                        d["buy_seats"],
                        d["sell_seats"],
                        day_amount=d["day_amount"],
                    )
                )
        return rows

    def _sse_basic(self, trade_date: str) -> list[dict]:
        parsed = _lhb_sse.fetch_trade_file(dash_date(trade_date))
        rows = []
        for s in parsed["stocks"]:
            rows.append(
                _basic_row(
                    parsed["trade_date"] or trade_date,
                    s["code"],
                    s["name"],
                    s["reason"],
                    s["buy_seats"],
                    s["sell_seats"],
                    day_amount=s["amount_wan"],
                )
            )
        return rows

    def _bse_basic(self, trade_date: str) -> list[dict]:
        dash = dash_date(trade_date)
        rows_all, _total = _lhb_bse.fetch_list(dash, dash)
        rows = []
        for r in rows_all:
            if r["jyrq"] != trade_date:
                continue  # 防御：窗口内其他交易日行不落当日
            code = normalize_stock_code(r["zqdm"])  # 历史旧码 → 920 新码
            self._clock(self._pause)
            seats = _lhb_bse.fetch_info(r["id"])
            rows.append(
                _basic_row(
                    trade_date,
                    code,
                    r["zqmc"],
                    r["plyy"],
                    seats["buy_seats"],
                    seats["sell_seats"],
                    day_amount=r["day_amount_wan"],
                )
            )
        return rows

    # ---------------------------------------------------------------- lhb_stock_statistic

    def _fetch_statistic(self, trade_date: str) -> list[dict]:
        """新浪 ggtj 近 60 日滚动窗口（#97 落地要点：窗口"近三月"→"近 60 日"，
        trade_date=采集执行日的快照语义——ggtj 无行级上榜日，不伪造）。"""
        ak = import_akshare()
        try:
            df = ak.stock_lhb_ggtj_sina(symbol="60")
        except Exception as e:
            raise FetchError(f"新浪龙虎榜统计抓取失败: {e}") from e
        if df is None or df.empty:
            return []
        rows = []
        from ._lhb_sina import map_ggtj_statistic_rows

        for row in map_ggtj_statistic_rows(df.to_dict("records")):
            row["trade_date"] = trade_date
            rows.append(row)
        return rows

    # ---------------------------------------------------------------- lhb_yyb_*（同花顺，委托 akshare 适配器映射）

    def _fetch_yyb_capital(self, trade_date: str) -> list[dict]:
        ak = import_akshare()
        try:
            df = ak.stock_lh_yyb_capital()
        except Exception as e:
            raise FetchError(f"同花顺营业部资金榜抓取失败: {e}") from e
        if df is None or df.empty:
            return []
        return map_lhb_yyb_capital_rows(df.to_dict("records"))

    def _fetch_yyb_most(self, trade_date: str) -> list[dict]:
        ak = import_akshare()
        try:
            df = ak.stock_lh_yyb_most()
        except Exception as e:
            raise FetchError(f"同花顺营业部排行抓取失败: {e}") from e
        if df is None or df.empty:
            return []
        return map_lhb_yyb_most_rows(df.to_dict("records"))
