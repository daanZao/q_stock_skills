"""baostock 适配器（个股日线 fallback 第 3 位，兜底）。

约束（issue #3）：北交所代码（4/8/9 开头）明确拒绝；baostock 无振幅/涨跌额列，
对应字段置 null（不伪造）。日期参数为 yyyy-mm-dd，adjustflag: 1 后复权/2 前复权/3 不复权。
issue #112：`_FIELDS` 补 turn 列恢复换手率（个股与指数接口实测均可用，
百分数口径直接入 turnover_rate）；`fetch_index_daily` 共用 `_FIELDS`，因此
`index_daily.turnover_rate` 同步恢复为 baostock 实测值（此前置 null）。
baostock 无全市场快照接口，fetch_market_snapshot 明确拒绝（issue #4）。
init 轻量初始化（issue #8）：支持股票清单（query_stock_basic 过滤 A 股前缀）
与指数日线（指数不复权，adjustflag=3）。
"""

import contextlib
import io
from datetime import date
from typing import NoReturn

from ..dates import dash_date
from ._eastmoney import _int, _num, _wan
from .base import FetchError, is_bse_code, json_safe

_FIELDS = "date,open,high,low,close,volume,amount,pctChg,tradestatus,turn"
_ADJUSTFLAG = {"qfq": "2", "hfq": "1", "none": "3"}


def _login():
    """懒加载 baostock 并登录，返回模块句柄；失败抛 FetchError。调用方负责 _logout。

    baostock 登录会向 stdout print，重定向以保住 stdio JSON 通道纯净（旧契约）。
    """
    try:
        import baostock as bs
    except ImportError as e:
        raise FetchError("baostock 未安装：pip install qstock-mcp[sources]") from e
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            lg = bs.login()
    except Exception as e:
        raise FetchError(f"baostock 登录失败: {e}") from e
    if lg.error_code != "0":
        raise FetchError(f"baostock 登录失败: {lg.error_msg}")
    return bs


def _logout(bs) -> None:
    """登出；baostock logout 同样向 stdout print，一并重定向。"""
    with contextlib.redirect_stdout(io.StringIO()):
        bs.logout()


def to_baostock_code(stock_code: str) -> str:
    """600519 → sh.600519，000001/300408 → sz.*；不识别的代码明确报错。"""
    if stock_code[:1] == "6":
        return f"sh.{stock_code}"
    if stock_code[:1] in ("0", "2", "3"):
        return f"sz.{stock_code}"
    raise FetchError(f"无法识别的 A 股代码: {stock_code}")


# 股票清单（issue #8）A 股个股前缀：sh.6 沪主板 / sz.0 深主板 / sz.3 创业板；
# sz.2 为深圳 B 股（如 sz.200002），sh.000 等为指数，均排除（项目只做 A 股）
_STOCK_PREFIXES = ("sh.6", "sz.0", "sz.3")


def map_stock_basic_rows(fields: list[str], rows: list[list[str]]) -> list[dict]:
    """query_stock_basic 行 → 股票清单行（stock_code 剥掉 sh./sz. 前缀）。

    只保留 A 股个股前缀且在上市状态的行：status 列 "1" 上市 / "0" 退市，
    该列缺失时保留（不臆断）。
    """
    result = []
    for row in rows:
        r = dict(zip(fields, row))
        code = r.get("code") or ""
        if not code.startswith(_STOCK_PREFIXES):
            continue
        if r.get("status", "1") != "1":
            continue
        result.append(
            {"stock_code": code.split(".", 1)[1], "stock_name": r.get("code_name")}
        )
    return result


def map_baostock_rows(fields: list[str], rows: list[list[str]]) -> list[dict]:
    """baostock 行数据 → bar dict；缺失列（amplitude/turnover 等）置 None。"""
    bars = []
    for row in rows:
        r = dict(zip(fields, row))
        bars.append(
            {
                "trade_date": str(r["date"]).replace("-", ""),
                "open": _num(r.get("open")),
                "high": _num(r.get("high")),
                "low": _num(r.get("low")),
                "close": _num(r.get("close")),
                "volume": _int(r.get("volume")),
                "amount": _wan(r.get("amount")),
                "amplitude": None,
                "change_percent": _num(r.get("pctChg")),
                "change_amount": None,
                "turnover_rate": _num(r.get("turn")),  # issue #112：turn 列补换手率
                "tradestatus": _int(r.get("tradestatus")),  # 权威停牌状态（issue #80）
            }
        )
    return bars


# adjustflag（次晨批一把抓返回列）→ 库内 adj 词汇；未知/缺失按不复权计
_ADJUSTFLAG_TO_ADJ = {"1": "hfq", "2": "qfq", "3": "none"}


def map_astock_daily_rows(fields: list[str], rows: list[list[str]]) -> list[dict]:
    """query_daily_history_k_AStock 行 → 全市场日线行 dict（次晨批，issue #83）。

    每行带 stock_code（剥 sh./sz. 前缀，过滤非 A 股个股前缀行）与 adj
    （adjustflag 列映射）；turn → turnover_rate，tradestatus 权威停牌状态，
    volume 原生为股透传、amount 元→万元（ADR 0006/0008）；无来源列
    （amplitude/change_amount）置 None 不伪造。
    """
    bars = []
    for row in rows:
        r = dict(zip(fields, row))
        code = r.get("code") or ""
        if not code.startswith(_STOCK_PREFIXES):
            continue
        bars.append(
            {
                "stock_code": code.split(".", 1)[1],
                "trade_date": str(r["date"]).replace("-", ""),
                "open": _num(r.get("open")),
                "high": _num(r.get("high")),
                "low": _num(r.get("low")),
                "close": _num(r.get("close")),
                "volume": _int(r.get("volume")),
                "amount": _wan(r.get("amount")),
                "amplitude": None,
                "change_percent": _num(r.get("pctChg")),
                "change_amount": None,
                "turnover_rate": _num(r.get("turn")),
                "tradestatus": _int(r.get("tradestatus")),
                "adj": _ADJUSTFLAG_TO_ADJ.get(str(r.get("adjustflag") or ""), "none"),
            }
        )
    return bars


class BaostockAdapter:
    name = "baostock"

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]:
        if is_bse_code(stock_code):
            raise FetchError(
                f"baostock 不支持北交所代码 {stock_code}（4/8/9 开头）",
                kind="unsupported",
            )
        bs_code = to_baostock_code(stock_code)
        bs = _login()
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                _FIELDS,
                start_date=f"{start[:4]}-{start[4:6]}-{start[6:]}",
                end_date=f"{end[:4]}-{end[4:6]}-{end[6:]}",
                frequency="d",
                adjustflag=_ADJUSTFLAG.get(adj, "2"),
            )
            if rs.error_code != "0":
                raise FetchError(f"baostock 查询失败: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            return map_baostock_rows(list(rs.fields), rows)
        finally:
            _logout(bs)

    def fetch_market_snapshot(self) -> dict:
        """baostock 无全市场快照接口，明确拒绝（不伪造）。"""
        raise FetchError("baostock 不支持全市场快照", kind="unsupported")

    def fetch_astock_daily(self, trade_date: str) -> list[dict]:
        """一把抓某日全市场 A 股日线（query_daily_history_k_AStock，次晨批用）。

        单次调用全量返回（上游单页上限 20000 行）；映射见 map_astock_daily_rows。
        """
        bs = _login()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rs = bs.query_daily_history_k_AStock(date=dash_date(trade_date))
            if rs.error_code != "0":
                raise FetchError(f"baostock 查询失败: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            return map_astock_daily_rows(list(rs.fields), rows)
        finally:
            _logout(bs)

    # ---------------------------------------------------------------- init 轻量初始化（issue #8）

    def fetch_stock_list(self) -> list[dict]:
        """股票清单：query_stock_basic 过滤 A 股个股前缀与退市股（映射见 map_stock_basic_rows）。"""
        bs = _login()
        try:
            rs = bs.query_stock_basic()
            if rs.error_code != "0":
                raise FetchError(f"baostock 查询失败: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            return map_stock_basic_rows(list(rs.fields), rows)
        finally:
            _logout(bs)

    def fetch_index_daily(self, index_code: str, start: str, end: str) -> list[dict]:
        """指数日线：399xxx → sz.399xxx，其余 sh.{code}；指数不复权（adjustflag=3）。"""
        bs_code = f"sz.{index_code}" if index_code.startswith("399") else f"sh.{index_code}"
        bs = _login()
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                _FIELDS,
                start_date=f"{start[:4]}-{start[4:6]}-{start[6:]}",
                end_date=f"{end[:4]}-{end[4:6]}-{end[6:]}",
                frequency="d",
                adjustflag="3",  # 指数不复权
            )
            if rs.error_code != "0":
                raise FetchError(f"baostock 查询失败: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            return map_baostock_rows(list(rs.fields), rows)
        finally:
            _logout(bs)

    # ---------------------------------------------------------------- 基本面透传（issue #6）

    def fetch_fundamentals(self, stock_code: str) -> dict:
        """最近季度利润/成长数据透传：字段名原样保留上游。

        北交所代码明确拒绝（同日线约束）；接口按年+季度查询，从当前季度
        向前最多回溯 4 季取最新有数据的一季。profit/growth 彼此独立，
        均无数据才抛 FetchError 结束 fallback 链。
        """
        if is_bse_code(stock_code):
            raise FetchError(
                f"baostock 不支持北交所代码 {stock_code}（4/8/9 开头）",
                kind="unsupported",
            )
        bs_code = to_baostock_code(stock_code)
        bs = _login()
        try:
            today = date.today()
            year, quarter = today.year, (today.month - 1) // 3 + 1
            payload: dict = {}
            errors: list[str] = []
            for _ in range(4):
                for section, query in [
                    ("profit", bs.query_profit_data),
                    ("growth", bs.query_growth_data),
                ]:
                    if section in payload:
                        continue
                    rs = query(code=bs_code, year=year, quarter=quarter)
                    if rs.error_code != "0":
                        errors.append(f"{section} {year}Q{quarter}: {rs.error_msg}")
                        continue
                    rows = []
                    while rs.next():
                        rows.append(dict(zip(rs.fields, rs.get_row_data())))
                    if rows:
                        payload[section] = rows
                if len(payload) == 2:
                    break
                quarter -= 1
                if quarter == 0:
                    year, quarter = year - 1, 4
        finally:
            _logout(bs)
        if not payload:
            raise FetchError(
                f"baostock 基本面无数据（{stock_code}）: {'; '.join(errors) or '空返回'}"
            )
        if errors:
            payload["errors"] = errors
        return json_safe(payload)

    def _unsupported_board(self, section: str) -> NoReturn:
        """baostock 无盘面快照接口（issue #5），明确拒绝（不伪造）。"""
        raise FetchError(f"baostock 不支持盘面 section: {section}", kind="unsupported")

    def fetch_indices(self, trade_date: str) -> list[dict]:
        self._unsupported_board("indices")

    def fetch_index_spot(self, index_code: str) -> list[dict]:
        self._unsupported_board("index_spot")

    def fetch_boards(self, trade_date: str) -> list[dict]:
        self._unsupported_board("boards")

    def fetch_zt_pool(self, trade_date: str) -> list[dict]:
        self._unsupported_board("zt_pool")

    def fetch_strong_stocks(self, trade_date: str) -> list[dict]:
        self._unsupported_board("strong_stocks")

    def fetch_lhb(self, trade_date: str) -> dict:
        self._unsupported_board("lhb")

    def fetch_board_history(
        self, board_type: str, board_name: str, start: str, end: str
    ) -> list[dict]:
        self._unsupported_board("board_history")

    def fetch_board_cons(self, board_name: str) -> list[dict]:
        self._unsupported_board("board_cons")
