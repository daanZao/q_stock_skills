"""baostock 进程内单会话代理（issue #170，ADR 0019）。

`BaostockApi.get()` 返回进程内单例：同一租约内所有 baostock 请求复用一次
`login`，串行执行，不每请求 `login/logout`。负责：

- 单请求 60s 超时（主线程用 SIGALRM 外部 deadline；非主线程用登录时配置的 socket 超时）；
- `10001001` 会话被踢：重登一次并重试**同一请求**；
- `10002004/10002007/10002008`/超时/连接异常：重置会话并最多重试一次；
- `10001011` 黑名单 / `10001005` 登录数上限：致命，零重试；
- 协议动作计数（login/logout/query/page），45,000 警告、50,000 停止新请求。

数据能力方法返回上游原始 `QueryResult`（fields/rows/pages；基本面返回原始
section payload）；字段映射仍在 `adapters/baostock_adapter.py`，本 ticket 不改变
旧适配器的每请求登录路径。
"""

from __future__ import annotations

import contextlib
import io
import logging
import signal
import socket
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from .baostock_errors import (
    BaostockBlacklisted,
    BaostockBudgetExhausted,
    BaostockError,
    BaostockLoginLimit,
    BaostockTimeout,
    BaostockUpstreamError,
)
from .baostock_lease import BaostockBudget
from .dates import dash_date

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_RETRY_BACKOFF_S = 1.5
# 参数来源（ADR 0019）：2026-11-05 正式回填每请求握手约 508 次/45 分钟后，
# 登录/查询退化到 20s 无响应（10002007），该事故支持“一次登录、串行复用、
# 60s 单请求硬超时、被踢重登一次”。1.5s 退避沿用 2026-10-31/11-04 单会话
# 串行 ≥1.5s 的实测节奏；长期 TPS/并发上限未验证，不得写成已验证容量。
TRANSIENT_ERROR_CODES = frozenset({"10002004", "10002007", "10002008"})
KICKED_ERROR_CODE = "10001001"
BLACKLIST_ERROR_CODE = "10001011"
LOGIN_LIMIT_ERROR_CODE = "10001005"


@dataclass(frozen=True)
class QueryResult:
    """一次逻辑查询的原始上游结果（翻页已展开）。"""

    fields: list[str]
    rows: list[list[str]]
    pages: int


class _SessionKicked(Exception):
    pass


class _TransientError(Exception):
    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


def _load_baostock():
    try:
        import baostock as bs  # type: ignore[import-untyped]
    except ImportError as exc:
        raise BaostockUpstreamError(
            "baostock 未安装：pip install qstock-mcp[sources]"
        ) from exc
    return bs


@contextlib.contextmanager
def _quiet_stdout():
    """baostock 客户端会向 stdout print；stdio MCP 通道必须保持纯净。"""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


class BaostockApi:
    """进程内单例 baostock 会话与请求纪律。"""

    _instance: "BaostockApi | None" = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] | None = None,
        budget: BaostockBudget | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client_factory = client_factory or _load_baostock
        self.budget = budget or BaostockBudget()
        self._timeout_s = timeout_s
        self._retry_backoff_s = retry_backoff_s
        self._sleep = sleep
        self._lock = threading.Lock()
        self._session = None
        self._warned_date: str | None = None

    # ------------------------------------------------------------- singleton
    @classmethod
    def get(cls, **kwargs) -> "BaostockApi":
        """进程内单例；首次调用可注入 client/budget/超时测试接缝。"""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(**kwargs)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """丢弃进程内单例（测试/进程生命周期用），存在会话时显式登出。"""
        with cls._instance_lock:
            instance = cls._instance
            cls._instance = None
        if instance is not None:
            instance.close()

    # -------------------------------------------------------------- lifecycle
    def ensure_session(self) -> None:
        """预登录（批量任务前置探测）；同一会话内重复调用不重复 login。"""
        with self._lock:
            self._ensure_session_locked()

    def close(self) -> None:
        """显式关闭并报告 logout 失败；重复调用幂等。"""
        with self._lock:
            self._reset_locked(strict=True)

    # --------------------------------------------------------------- requests
    def query(
        self,
        operation: Callable[[Any], Any],
        *,
        label: str = "baostock 查询",
    ) -> QueryResult:
        """执行一次逻辑查询：会话复用 + 超时 + 重登/瞬时重试 + 预算计数。"""
        with self._lock:
            return self._execute_locked(operation, label)

    def fetch_daily(
        self,
        code: str,
        start: str,
        end: str,
        *,
        fields: str,
        adjustflag: str,
    ) -> QueryResult:
        """个股日线（code 为 baostock 代码，如 sh.600519）。"""
        return self.query(
            lambda client: client.query_history_k_data_plus(
                code,
                fields,
                start_date=dash_date(start),
                end_date=dash_date(end),
                frequency="d",
                adjustflag=adjustflag,
            ),
            label=f"baostock 日线 {code}",
        )

    def fetch_astock_daily(self, trade_date: str) -> QueryResult:
        """某交易日全市场 A 股日线（一把抓，单页 20,000 行）。"""
        return self.query(
            lambda client: client.query_daily_history_k_AStock(
                date=dash_date(trade_date)
            ),
            label=f"baostock 全市场日线 {trade_date}",
        )

    def fetch_daily_adjust_factor(self, trade_date: str) -> QueryResult:
        """某交易日全市场复权因子事件。"""
        return self.query(
            lambda client: client.query_daily_adjust_factor(
                date=dash_date(trade_date)
            ),
            label=f"baostock 复权因子 {trade_date}",
        )

    def fetch_stock_list(self) -> QueryResult:
        """股票清单（query_stock_basic）。"""
        return self.query(
            lambda client: client.query_stock_basic(),
            label="baostock 股票清单",
        )

    def fetch_index_daily(
        self,
        code: str,
        start: str,
        end: str,
        *,
        fields: str,
    ) -> QueryResult:
        """指数日线（不复权，adjustflag=3）。"""
        return self.query(
            lambda client: client.query_history_k_data_plus(
                code,
                fields,
                start_date=dash_date(start),
                end_date=dash_date(end),
                frequency="d",
                adjustflag="3",
            ),
            label=f"baostock 指数日线 {code}",
        )

    def fetch_fundamentals(
        self, code: str, *, today: date | None = None
    ) -> dict:
        """最近季度利润/成长数据（query_profit_data + query_growth_data）。

        从当前季度向前最多回溯 4 季，profit/growth 彼此独立；单 section 失败
        只记入 errors 并继续，全部无数据才抛 `BaostockUpstreamError`。致命
        错误（黑名单/登录上限/预算耗尽）不降级、直接抛出。
        """
        moment = today or date.today()
        year, quarter = moment.year, (moment.month - 1) // 3 + 1
        payload: dict = {}
        errors: list[str] = []
        for _ in range(4):
            for section, method_name in (
                ("profit", "query_profit_data"),
                ("growth", "query_growth_data"),
            ):
                if section in payload:
                    continue
                try:
                    result = self.query(
                        lambda client: getattr(client, method_name)(
                            code=code, year=year, quarter=quarter
                        ),
                        label=f"baostock 基本面 {section} {year}Q{quarter}",
                    )
                except (
                    BaostockBlacklisted,
                    BaostockLoginLimit,
                    BaostockBudgetExhausted,
                ):
                    raise
                except BaostockError as exc:
                    errors.append(f"{section} {year}Q{quarter}: {exc}")
                    continue
                if result.rows:
                    payload[section] = [
                        dict(zip(result.fields, row)) for row in result.rows
                    ]
            if len(payload) == 2:
                break
            quarter -= 1
            if quarter == 0:
                year, quarter = year - 1, 4
        if not payload:
            raise BaostockUpstreamError(
                f"baostock 基本面无数据（{code}）: "
                f"{'; '.join(errors) or '空返回'}"
            )
        if errors:
            payload["errors"] = errors
        return payload

    # ---------------------------------------------------------------- internal
    def _execute_locked(
        self, operation: Callable[[Any], Any], label: str
    ) -> QueryResult:
        kicked = False
        transient = False
        while True:
            try:
                self._ensure_session_locked()
                self._record_or_raise_locked("query")
                stop_for_budget = self.budget.snapshot()["exhausted"]
                with _quiet_stdout(), self._request_deadline():
                    try:
                        result = operation(self._session)
                    except TimeoutError as exc:
                        raise BaostockTimeout(
                            f"{label}: socket 请求超时",
                            timeout_s=self._timeout_s,
                        ) from exc
                    except (OSError, ConnectionError) as exc:
                        raise _TransientError(
                            f"{label}: 连接异常 {exc}"
                        ) from exc
                    self._raise_for_result(result, label)
                    rows: list[list[str]] = []
                    pages = 1
                    # 已知客户端限制（baostock 0.9.3）：ResultData.next() 在翻页
                    # recv 空数据时返回 False 但不置 error_code，静默截断无法在本层
                    # 可靠区分“正常 2000 行结束”和“下一页失败”；只对显式
                    # error_code/超时重试，页级校验留给 #171 适配层。
                    try:
                        while True:
                            if stop_for_budget:
                                raise BaostockBudgetExhausted(
                                    self.budget.snapshot(),
                                    note=(
                                        "翻页使当日协议动作达到 50,000 上限；"
                                        "停止批次，等 Asia/Shanghai 次日重置"
                                    ),
                                )
                            has_next = result.next()
                            current_pages = _page_number(result)
                            if current_pages > pages:
                                # 翻页已实际发生：立即计数，再判断是否还能继续。
                                self._record_pages_locked(current_pages - pages)
                                pages = current_pages
                                if self.budget.snapshot()["exhausted"]:
                                    stop_for_budget = True
                            if not has_next:
                                break
                            rows.append(result.get_row_data())
                    except TimeoutError as exc:
                        raise BaostockTimeout(
                            f"{label}: socket 翻页超时",
                            timeout_s=self._timeout_s,
                        ) from exc
                    except (OSError, ConnectionError) as exc:
                        raise _TransientError(
                            f"{label}: 翻页连接异常 {exc}"
                        ) from exc
                    if (
                        rows
                        and _page_number(result) == pages
                        and str(getattr(result, "error_code", "0") or "0")
                        != "0"
                    ):
                        # 页请求失败时客户端可能不推进 cur_page_num；按一次保守计数。
                        self._record_pages_locked(1)
                    self._raise_for_result(result, label)
                fields = list(getattr(result, "fields", []) or [])
                return QueryResult(fields=fields, rows=rows, pages=pages)
            except _SessionKicked as exc:
                self._reset_locked()
                if kicked:
                    raise BaostockUpstreamError(
                        f"{label}: baostock 会话被踢，重登一次后仍失败",
                        error_code=KICKED_ERROR_CODE,
                    ) from exc
                kicked = True
                self._sleep(self._retry_backoff_s)
            except _TransientError as exc:
                self._reset_locked()
                if transient:
                    raise BaostockUpstreamError(
                        f"{label}: {exc}",
                        error_code=exc.error_code,
                        retryable=True,
                    ) from exc
                transient = True
                self._sleep(self._retry_backoff_s)
            except BaostockTimeout:
                self._reset_locked()
                if transient:
                    raise
                transient = True
                self._sleep(self._retry_backoff_s)
            except (BaostockBlacklisted, BaostockLoginLimit):
                # 致命错误零重试；不再向黑名单/已达登录上限的会话发 logout。
                self._discard_locked()
                raise

    def _ensure_session_locked(self) -> None:
        if self._session is not None:
            return
        # 先查预算，避免 50k 之后仍 import/触达 baostock。
        self._check_budget_locked()
        if self._timeout_s is not None and self._timeout_s > 0:
            socket.setdefaulttimeout(self._timeout_s)
        elif socket.getdefaulttimeout() is None:
            socket.setdefaulttimeout(DEFAULT_TIMEOUT_S)
        client = self._client_factory()
        self._record_or_raise_locked("login")
        try:
            with _quiet_stdout(), self._request_deadline():
                login_result = client.login()
        except BaostockTimeout:
            raise
        except TimeoutError as exc:
            raise BaostockTimeout(
                "baostock 登录 socket 超时", timeout_s=self._timeout_s
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 客户端登录异常按瞬时错误处理
            raise _TransientError(f"baostock 登录失败: {exc}") from exc
        self._raise_for_login(login_result)
        self._session = client

    def _raise_for_login(self, result: Any) -> None:
        self._raise_if_error(result, "baostock 登录失败", allow_kick=False)

    def _raise_for_result(self, result: Any, label: str) -> None:
        self._raise_if_error(result, label, allow_kick=True)

    def _raise_if_error(
        self, result: Any, label: str, *, allow_kick: bool
    ) -> None:
        code = str(getattr(result, "error_code", "0") or "0")
        if code == "0":
            return
        message = str(getattr(result, "error_msg", "") or "")
        raise _classify_error(code, message, label, allow_kick=allow_kick)

    # ------------------------------------------------------------------ budget
    def _check_budget_locked(self) -> None:
        snapshot = self.budget.snapshot()
        if snapshot["exhausted"]:
            raise BaostockBudgetExhausted(
                snapshot,
                note="当日协议动作已达 50,000 硬上限；停止新请求，等 Asia/Shanghai 次日重置",
            )
        self._warn_if_needed(snapshot)

    def _record_or_raise_locked(self, action: str) -> None:
        self._check_budget_locked()
        self._warn_if_needed(self.budget.record(action))

    def _record_pages_locked(self, count: int) -> None:
        if count <= 0:
            return
        self._warn_if_needed(self.budget.record("page", count))

    def _warn_if_needed(self, snapshot: dict) -> None:
        if not snapshot["warning"]:
            return
        if self._warned_date == snapshot["date"]:
            return
        self._warned_date = snapshot["date"]
        logger.warning(
            "baostock 当日协议动作已达 %d，超过 45,000 警告线（50,000 停止）",
            snapshot["total"],
        )

    # ---------------------------------------------------------------- session
    def _reset_locked(self, *, strict: bool = False) -> None:
        """错误恢复保留原异常；显式 close 以 strict 报告 logout 失败。"""
        session, self._session = self._session, None
        if session is None:
            return
        if self.budget.snapshot()["exhausted"]:
            return
        try:
            self._record_or_raise_locked("logout")
        except BaostockError:
            if strict:
                raise
            return
        try:
            with _quiet_stdout(), self._request_deadline():
                session.logout()
        except Exception:  # noqa: BLE001 - 非 strict 清理不得覆盖原数据错误
            if strict:
                raise

    def _discard_locked(self) -> None:
        """丢弃会话但不发 logout（黑名单/登录上限等致命路径）。"""
        self._session = None

    # ----------------------------------------------------------------- timeout
    @contextlib.contextmanager
    def _request_deadline(self):
        seconds = self._timeout_s
        if (
            seconds is None
            or seconds <= 0
            or not hasattr(signal, "SIGALRM")
            or threading.current_thread() is not threading.main_thread()
        ):
            # MCP server 的同步工具在 anyio worker thread 执行，SIGALRM 不可用；
            # 此时使用 _ensure_session_locked 在 login 前配置的 socket 超时。
            # 该方案只覆盖 socket 阻塞，不是非 socket 代码的外部硬 deadline。
            yield
            return

        def timeout_handler(_signum, _frame):
            raise BaostockTimeout(
                f"baostock 请求超过 {seconds:g}s 外部硬超时",
                timeout_s=seconds,
            )

        previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _classify_error(
    code: str, message: str, label: str, *, allow_kick: bool
) -> Exception:
    if code == KICKED_ERROR_CODE:
        if allow_kick:
            return _SessionKicked(message)
        return BaostockUpstreamError(
            f"{label}: {message}", error_code=code, retryable=False
        )
    if code == BLACKLIST_ERROR_CODE:
        return BaostockBlacklisted(
            f"{label}: {message}（{BLACKLIST_ERROR_CODE}）",
            note="黑名单致命：零重试、零换源；先看官方统计页待释放时间",
        )
    if code == LOGIN_LIMIT_ERROR_CODE:
        return BaostockLoginLimit(
            f"{label}: {message}（{LOGIN_LIMIT_ERROR_CODE}）",
            note="账号登录数达上限：不自动重试，等待其他会话释放",
        )
    if code in TRANSIENT_ERROR_CODES:
        return _TransientError(f"{label}: {message}", error_code=code)
    return BaostockUpstreamError(
        f"{label}: {message}", error_code=code, retryable=False
    )


def _page_number(result: Any) -> int:
    raw = getattr(result, "cur_page_num", 1)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1
