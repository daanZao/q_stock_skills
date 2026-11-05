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
import signal
from datetime import date
from typing import NoReturn

from ..dates import dash_date
from ._num import _int, _num, _wan
from .base import FetchError, is_bse_code, json_safe

_FIELDS = "date,open,high,low,close,preclose,volume,amount,pctChg,tradestatus,turn"
_ADJUSTFLAG = {"qfq": "2", "hfq": "1", "none": "3"}


def _login():
    """懒加载 baostock 并登录，返回模块句柄；失败抛 FetchError。调用方负责 _logout。

    baostock 登录会向 stdout print，重定向以保住 stdio JSON 通道纯净（旧契约）。
    """
    try:
        import baostock as bs  # type: ignore[import-untyped]
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


BAOSTOCK_REQUEST_TIMEOUT_S = 120


class _SessionKicked(RuntimeError):
    pass


@contextlib.contextmanager
def _request_deadline():
    def timeout_handler(_signum, _frame):
        raise FetchError(
            f"baostock 请求超过 {BAOSTOCK_REQUEST_TIMEOUT_S}s 外部硬超时",
            kind="connection",
        )

    previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
    previous_timer = signal.setitimer(
        signal.ITIMER_REAL, BAOSTOCK_REQUEST_TIMEOUT_S
    )
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _logout_quietly(bs) -> None:
    try:
        with _request_deadline():
            _logout(bs)
    except Exception:  # noqa: BLE001 - 清理路径不抛
        pass


def _with_relogin(operation):
    for attempt in range(3):
        with _request_deadline():
            bs = _login()
        try:
            with _request_deadline():
                return operation(bs)
        except _SessionKicked as e:
            if attempt == 2:
                raise FetchError("baostock 会话被踢，重登重试 3 次后仍失败") from e
        finally:
            _logout_quietly(bs)
    raise AssertionError("unreachable")


def _query_result(rs, label: str):
    if rs.error_code == "10001001":
        raise _SessionKicked(rs.error_msg)
    if rs.error_code != "0":
        raise FetchError(f"{label}: {rs.error_msg}")
    return rs


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
                "preclose": _num(r.get("preclose")),
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
                "preclose": _num(r.get("preclose")),
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


def map_adjust_factor_rows(fields: list[str], rows: list[list[str]]) -> list[dict]:
    """逐日因子响应 → 累计 B；第五列 adjustFacto 不参与业务派生（issue #160）。"""
    events = []
    for row in rows:
        r = dict(zip(fields, row))
        code = r.get("code") or ""
        if not code.startswith(_STOCK_PREFIXES):
            continue
        events.append({
            "stock_code": code.split(".", 1)[1],
            "ex_date": str(r["dividOperateDate"]).replace("-", ""),
            "factor_cum": _num(r.get("backAdjustFactor")),
        })
    return events


class BaostockAdapter:
    name = "baostock"

    def __init__(self, persistent: bool = False):
        """persistent=True 时同一 adapter 复用一次登录会话（回填专用）。

        正式回填若每请求登录/登出，数百次握手会触发上游登录限速
        （2026-11-05 实测约 508 次后登录/查询 20s 无响应）；长会话内串行
        查询可显著减少握手。会话被踢时按既有规则重登 ≤3 次。
        """
        self._persistent = persistent
        self._session = None

    def _drop_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            _logout_quietly(session)

    def ensure_session(self) -> None:
        """persistent 模式预登录；登录失败直接抛 FetchError（回填前置探测）。"""
        if not self._persistent:
            return
        if self._session is None:
            with _request_deadline():
                self._session = _login()

    def close(self) -> None:
        """关闭常驻会话（persistent=True 的回填结束时调用）。"""
        self._drop_session()

    def _run(self, operation):
        if not self._persistent:
            return _with_relogin(operation)
        for attempt in range(3):
            if self._session is None:
                with _request_deadline():
                    self._session = _login()
            try:
                with _request_deadline():
                    return operation(self._session)
            except _SessionKicked as e:
                self._drop_session()
                if attempt == 2:
                    raise FetchError(
                        "baostock 会话被踢，重登重试 3 次后仍失败"
                    ) from e
            except Exception:
                # 超时/协议错误后旧 socket 可能不可继续用，丢弃会话。
                self._drop_session()
                raise
        raise AssertionError("unreachable")

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]:
        if is_bse_code(stock_code):
            raise FetchError(
                f"baostock 不支持北交所代码 {stock_code}（4/8/9 开头）",
                kind="unsupported",
            )
        bs_code = to_baostock_code(stock_code)

        def operation(bs):
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

        return self._run(operation)

    def fetch_market_snapshot(self) -> dict:
        """baostock 无全市场快照接口，明确拒绝（不伪造）。"""
        raise FetchError("baostock 不支持全市场快照", kind="unsupported")

    def fetch_astock_daily(self, trade_date: str) -> list[dict]:
        """一把抓某日全市场 A 股日线（query_daily_history_k_AStock，次晨批用）。

        单次调用全量返回（上游单页上限 20000 行）；映射见 map_astock_daily_rows。
        """
        def operation(bs):
            with contextlib.redirect_stdout(io.StringIO()):
                rs = _query_result(
                    bs.query_daily_history_k_AStock(date=dash_date(trade_date)),
                    "baostock 查询失败",
                )
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
            return map_astock_daily_rows(list(rs.fields), rows)

        return _with_relogin(operation)

    def fetch_daily_adjust_factor(self, trade_date: str) -> list[dict]:
        """逐日全市场事件；B 只取 backAdjustFactor（issue #160）。

        2026-11-04 定点实测采用单会话串行、请求间隔 ≥1.5s、90s 外部超时；
        长期 TPS/并发上限仍待验证，调用方不得据此放量。生产单请求硬超时
        120s；10001001 会话被踢时自动重登，最多 3 次尝试。
        """
        def operation(bs):
            with contextlib.redirect_stdout(io.StringIO()):
                rs = _query_result(
                    bs.query_daily_adjust_factor(date=dash_date(trade_date)),
                    "baostock 因子查询失败",
                )
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
            return map_adjust_factor_rows(list(rs.fields), rows)

        return self._run(operation)

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
