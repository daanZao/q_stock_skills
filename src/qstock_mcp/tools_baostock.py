"""baostock 显式租约的 MCP/CLI 共用核心层（issue #170，ADR 0019）。

`server.py` 只做薄包装；租约动作分发、参数校验与结构化错误转换都在这里。
acquire/status 只读写本地文件；release 还会关闭本进程已有的 BaostockApi
会话（可能 logout），无活动会话时不创建 client、不发 baostock 请求。
"""

from __future__ import annotations

from .baostock_api import BaostockApi
from .baostock_errors import BaostockError
from .baostock_lease import BaostockLease
from .output import error as output_error

ACTIONS = ("acquire", "status", "release")
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
            state = lease.release(force=force)
            BaostockApi.reset_instance()
    except BaostockError as exc:
        payload = exc.to_dict()
        error_kind = payload.pop("error_kind")
        message = payload.pop("error")
        payload.pop("status", None)
        return output_error(
            "baostock_lock",
            params,
            message,
            error_kind=error_kind,
            **payload,
        )
    except OSError as exc:
        return output_error(
            "baostock_lock",
            params,
            f"本地租约文件操作失败: {exc}",
            error_kind="io_error",
        )

    return {"status": "ok", "action": action, **state}


__all__ = [
    "ACTIONS",
    "MCP_COMMAND",
    "MCP_DEFAULT_OWNER",
    "baostock_lock",
]
