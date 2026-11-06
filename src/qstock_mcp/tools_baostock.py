"""baostock 实例租约的 MCP/CLI 共用核心层（issue #173，ADR 0019）。

`server.py` 只做薄包装；租约动作、实例归属、参数校验与结构化错误转换在这里。
CLI 公开动作仅 status/stale force；MCP acquire 由 agent 经用户确认后显式调用。
release 关闭本进程已有的 BaostockApi 会话；无活动会话时不创建 client。
"""

from __future__ import annotations

from .baostock_api import BaostockApi
from .baostock_errors import BaostockError
from .baostock_lease import BaostockLease, current_lease_context
from .output import error as output_error

ACTIONS = ("acquire", "status", "release")
BAOSTOCK_BATCH_STOP_ERRORS = frozenset({
    "baostock_locked",
    "baostock_budget_exhausted",
    "baostock_blacklisted",
    "baostock_login_limit",
})
BAOSTOCK_ERROR_FIELDS = (
    "error_kind",
    "lock",
    "note",
    "token_present",
    "token_valid",
    "budget",
    "error_code",
)
MCP_COMMAND = "mcp:baostock_lock"
MCP_DEFAULT_OWNER = "qstock-mcp"


def _params(
    action: str,
    *,
    owner: str | None,
    reason: str | None,
    force: bool,
) -> dict:
    params: dict = {"action": action}
    if owner:
        params["owner"] = owner
    if reason:
        params["reason"] = reason
    if force:
        params["force"] = True
    return params


def baostock_error(tool: str, params: dict, exc: BaostockError) -> dict:
    payload = exc.to_dict()
    error_kind = payload.pop("error_kind")
    message = payload.pop("error")
    payload.pop("status", None)
    return output_error(tool, params, message, error_kind=error_kind, **payload)


def baostock_lock(
    action: str = "status",
    *,
    owner: str | None = None,
    reason: str | None = None,
    force: bool = False,
    command: str | None = None,
    default_owner: str | None = None,
) -> dict:
    """申请/释放/查询 baostock 显式租约；返回统一结构与结构化错误。

    `owner`/`reason` 只对 acquire 有效，传入其他 action 会被拒绝而不是静默
    忽略；`force` 只对 release 有效。
    """
    action = (action or "status").strip().lower()
    params = _params(action, owner=owner, reason=reason, force=force)
    if action not in ACTIONS:
        return output_error(
            "baostock_lock",
            params,
            f"未知 action: {action!r}（可选 acquire/status/release）",
            error_kind="invalid_argument",
        )
    entry = current_lease_context().identity.entry
    if entry == "cli" and action == "acquire":
        return output_error(
            "baostock_lock",
            params,
            "CLI 数据命令自动 acquire；公开锁命令只支持 status/release --force",
            error_kind="invalid_argument",
        )
    if entry == "cli" and action == "release" and not force:
        return output_error(
            "baostock_lock",
            params,
            "CLI 普通 release 已移除；仅确认持有进程终止、锁已 stale 后可 --force",
            error_kind="invalid_argument",
        )
    if action != "acquire" and (owner or reason):
        return output_error(
            "baostock_lock",
            params,
            "--owner/--reason 仅用于 acquire；status/release 不接受",
            error_kind="invalid_argument",
        )
    if action != "release" and force:
        return output_error(
            "baostock_lock",
            params,
            "--force 仅用于 release",
            error_kind="invalid_argument",
        )

    lease = BaostockLease()
    try:
        if action == "acquire":
            state = lease.acquire(
                owner=owner or default_owner,
                reason=reason,
                command=command,
            )
        elif action == "status":
            state = lease.status()
        else:
            try:
                BaostockApi.reset_instance()
            except Exception as exc:
                return output_error(
                    "baostock_lock",
                    params,
                    f"关闭当前 baostock 会话失败，租约未释放: {type(exc).__name__}: {exc}",
                    error_kind="baostock_cleanup_error",
                )
            expected = lease.status()["generation"]
            state = lease.release(force=force, expected=expected)
    except BaostockError as exc:
        return baostock_error("baostock_lock", params, exc)
    except OSError as exc:
        return output_error(
            "baostock_lock",
            params,
            f"本地租约文件操作失败: {exc}",
            error_kind="io_error",
        )

    return {"status": "ok", "action": action, **state}


def require_mcp_data_lease(tool: str, params: dict) -> dict | None:
    try:
        BaostockLease().require()
    except BaostockError as exc:
        return baostock_error(tool, params, exc)
    return None


__all__ = [
    "ACTIONS",
    "MCP_COMMAND",
    "MCP_DEFAULT_OWNER",
    "baostock_lock",
    "require_mcp_data_lease",
]
