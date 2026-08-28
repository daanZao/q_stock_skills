"""baostock 适配器（fallback 第 3 位，兜底）。

约束（issue #3）：北交所代码（4/8/9 开头）明确拒绝；baostock 无振幅/换手/涨跌额列，
对应字段置 null（不伪造）。日期参数为 yyyy-mm-dd，adjustflag: 1 后复权/2 前复权/3 不复权。
baostock 无全市场快照接口，fetch_market_snapshot 明确拒绝（issue #4）。
init 轻量初始化（issue #8）：支持股票清单（query_stock_basic 过滤 A 股前缀）
与指数日线（指数不复权，adjustflag=3）。
"""

from datetime import date
from typing import NoReturn

from ._eastmoney import _int, _num
from .base import FetchError, is_bse_code, json_safe

_FIELDS = "date,open,high,low,close,volume,amount,pctChg"
_ADJUSTFLAG = {"qfq": "2", "hfq": "1", "none": "3"}


def _login():
    """懒加载 baostock 并登录，返回模块句柄；失败抛 FetchError。调用方负责 logout。"""
    try:
        import baostock as bs
    except ImportError as e:
        raise FetchError("baostock 未安装：pip install qstock-mcp[sources]") from e
    try:
        lg = bs.login()
    except Exception as e:
        raise FetchError(f"baostock 登录失败: {e}") from e
    if lg.error_code != "0":
        raise FetchError(f"baostock 登录失败: {lg.error_msg}")
    return bs


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
                "amount": _num(r.get("amount")),
                "amplitude": None,
                "change_percent": _num(r.get("pctChg")),
                "change_amount": None,
                "turnover_rate": None,
            }
        )
    return bars


class BaostockAdapter:
    name = "baostock"

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]:
        if is_bse_code(stock_code):
            raise FetchError(f"baostock 不支持北交所代码 {stock_code}（4/8/9 开头）")
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
            bs.logout()

    def fetch_market_snapshot(self) -> dict:
        """baostock 无全市场快照接口，明确拒绝（不伪造）。"""
        raise FetchError("baostock 不支持全市场快照")

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
            bs.logout()

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
            bs.logout()

    # ---------------------------------------------------------------- 基本面透传（issue #6）

    def fetch_fundamentals(self, stock_code: str) -> dict:
        """最近季度利润/成长数据透传：字段名原样保留上游。

        北交所代码明确拒绝（同日线约束）；接口按年+季度查询，从当前季度
        向前最多回溯 4 季取最新有数据的一季。profit/growth 彼此独立，
        均无数据才抛 FetchError 结束 fallback 链。
        """
        if is_bse_code(stock_code):
            raise FetchError(f"baostock 不支持北交所代码 {stock_code}（4/8/9 开头）")
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
            bs.logout()
        if not payload:
            raise FetchError(
                f"baostock 基本面无数据（{stock_code}）: {'; '.join(errors) or '空返回'}"
            )
        if errors:
            payload["errors"] = errors
        return json_safe(payload)

    def _unsupported_board(self, section: str) -> NoReturn:
        """baostock 无盘面快照接口（issue #5），明确拒绝（不伪造）。"""
        raise FetchError(f"baostock 不支持盘面 section: {section}")

    def fetch_indices(self, trade_date: str) -> list[dict]:
        self._unsupported_board("indices")

    def fetch_boards(self, trade_date: str) -> list[dict]:
        self._unsupported_board("boards")

    def fetch_zt_pool(self, trade_date: str) -> list[dict]:
        self._unsupported_board("zt_pool")

    def fetch_strong_stocks(self, trade_date: str) -> list[dict]:
        self._unsupported_board("strong_stocks")

    def fetch_lhb(self, trade_date: str) -> dict:
        self._unsupported_board("lhb")
