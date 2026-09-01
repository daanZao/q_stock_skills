"""proxy 能力面核心：mx_query 妙想 mx-data 透传（issue #23/T1）。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake client 与
临时配额 ledger（见 tests/）。透传契约：原样回传上游响应 body（data 为
完整 body，键名不动），不规格化、不连接数据库、不落库、不进 fallback 链；
输出自描述 JSON（quota 回显 mx-data 当日用量）。配额触顶不调上游；上游
业务码 code!=0 与 MXError 均走统一 error 契约，绝不伪造数据；任何路径
不抛异常。
"""

import logging
from typing import Protocol

from .mx_client import MXError, MxClient
from .mx_quota import MxQuota
from .output import error as _error

log = logging.getLogger(__name__)

SKILL = "mx-data"  # 配额 ledger 的 skill 键（与 MX_DAILY_LIMIT_MX_DATA 对应）


class _MxQueryable(Protocol):
    def query(self, tool_query: str) -> dict: ...


def mx_query(
    tool_query: str,
    client: _MxQueryable | None = None,
    quota: MxQuota | None = None,
) -> dict:
    """mx-data 透传：自然语言问句 → 上游原始 JSON；本地每日配额先检后记。"""
    tool = "mx_query"
    params = {"tool_query": tool_query}
    if quota is None:
        quota = MxQuota()
    snap = quota.snapshot(SKILL)
    if quota.is_exhausted(SKILL):
        return _error(
            tool,
            params,
            f"妙想 {SKILL} 当日配额触顶（{snap['used']}/{snap['limit']}），未调用上游",
            quota=snap,
        )
    try:
        if client is None:
            client = MxClient()
    except MXError as e:
        # key 缺失等构造期错误：未触达上游，不计配额
        return _error(tool, params, str(e), quota=snap)
    mx_error: MXError | None = None
    body: dict | None = None
    try:
        body = client.query(tool_query)
    except MXError as e:
        mx_error = e  # 传输错误：上游已触达，照样记账
    except Exception as e:  # noqa: BLE001 - 工具层任何路径不抛异常
        log.exception("mx_query 内部错误")
        return _error(tool, params, f"内部错误：{e}", quota=snap)
    try:
        quota.record(SKILL)
    except Exception:  # noqa: BLE001 - ledger 写盘失败不拖垮透传结果
        log.warning("配额 ledger 记账失败（不影响本次结果）", exc_info=True)
    snap = quota.snapshot(SKILL)
    if mx_error is not None:
        return _error(tool, params, str(mx_error), quota=snap)
    assert body is not None
    code = body.get("code")
    if code == 0:
        return {
            "status": "ok",
            "tool": tool,
            "params": params,
            "data": body,
            "quota": snap,
        }
    message = body.get("message") or "（无 message）"
    return _error(
        tool,
        params,
        f"妙想 {SKILL} 上游错误 code={code}：{message}",
        upstream_code=code,
        upstream_message=body.get("message"),
        quota=snap,
    )
