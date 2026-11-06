"""baostock 结构化错误契约（issue #170，ADR 0019）。

错误对象同时用于 CLI/MCP 输出：`to_dict()` 返回 `status=error` +
`error_kind` 的统一错误载荷。错误种类与 spec #169 的错误契约一致：
`baostock_locked` / `baostock_budget_exhausted` / `baostock_blacklisted` /
`baostock_login_limit` / `baostock_timeout` / `baostock_upstream_error`；
CLI 收尾另输出 `baostock_cleanup_error`。
它们是 baostock 专属错误，到 fetch 边界再映射到抓取三态（ADR 0007）。
"""

from __future__ import annotations


class BaostockError(RuntimeError):
    """所有 baostock 显式租约/会话错误的基类。"""

    kind = "baostock_error"

    def __init__(self, message: str, *, note: str | None = None, **details) -> None:
        super().__init__(message)
        self.note = note
        self.details = details

    def to_dict(self) -> dict:
        payload = {
            "status": "error",
            "error_kind": self.kind,
            "error": str(self),
        }
        if self.note:
            payload["note"] = self.note
        payload.update(self.details)
        return payload


class BaostockLocked(BaostockError):
    """没有有效租约，或租约被其他会话占用。"""

    kind = "baostock_locked"

    def __init__(
        self,
        message: str,
        *,
        lock: dict | None = None,
        token_present: bool | None = None,
        token_valid: bool | None = None,
        note: str | None = None,
    ) -> None:
        details: dict = {}
        if lock is not None:
            details["lock"] = lock
        if token_present is not None:
            details["token_present"] = token_present
        if token_valid is not None:
            details["token_valid"] = token_valid
        super().__init__(message, note=note, **details)


class BaostockBudgetExhausted(BaostockError):
    """当日协议动作达到 50,000 硬上限，停止新请求/新租约。"""

    kind = "baostock_budget_exhausted"

    def __init__(self, budget: dict, *, note: str | None = None) -> None:
        total = budget.get("total")
        limit = budget.get("stop_at", 50_000)
        super().__init__(
            f"baostock 当日协议动作已达 {total}，达到 {limit} 上限，停止新请求",
            note=note,
            budget=budget,
        )


class BaostockBlacklisted(BaostockError):
    """10001011 黑名单：致命，零重试、零换源。"""

    kind = "baostock_blacklisted"

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "10001011",
        note: str | None = None,
    ) -> None:
        super().__init__(message, note=note, error_code=error_code)


class BaostockLoginLimit(BaostockError):
    """10001005 账号登录数达上限：致命，不自动重试。"""

    kind = "baostock_login_limit"

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "10001005",
        note: str | None = None,
    ) -> None:
        super().__init__(message, note=note, error_code=error_code)


class BaostockTimeout(BaostockError):
    """单请求 60s 超时（外部 deadline）。"""

    kind = "baostock_timeout"

    def __init__(
        self,
        message: str,
        *,
        timeout_s: float | None = None,
        note: str | None = None,
    ) -> None:
        details: dict = {}
        if timeout_s is not None:
            details["timeout_s"] = timeout_s
        super().__init__(message, note=note, **details)


class BaostockUpstreamError(BaostockError):
    """上游错误码/连接异常；retryable 表示是否属于瞬时错误。"""

    kind = "baostock_upstream_error"

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        retryable: bool = False,
        note: str | None = None,
    ) -> None:
        details: dict = {"retryable": retryable}
        if error_code is not None:
            details["error_code"] = error_code
        super().__init__(message, note=note, **details)
