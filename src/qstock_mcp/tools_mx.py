"""proxy 能力面核心：mx_query 妙想 mx-data 透传（issue #23/T1）、
mx_search 妙想 mx-search 资讯搜索落库（issue #24/T2）。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake client 与
临时配额 ledger（见 tests/）。透传契约：原样回传上游响应 body（data 为
完整 body，键名不动），不规格化、不连接数据库、不落库、不进 fallback 链；
mx_search 例外：搜索成功即按业务键幂等落库 news_items（默认挂 market/_market
主体）。输出自描述 JSON（quota 回显当日用量）。配额触顶不调上游；上游
业务码 code!=0 与 MXError 均走统一 error 契约，绝不伪造数据；任何路径
不抛异常。
"""

import logging
from typing import Any, Callable, Protocol

from .db import connect
from .mx_client import MXError, MxClient
from .mx_quota import MxQuota
from .output import error as _error
from .repository import upsert_news_items

log = logging.getLogger(__name__)

SKILL = "mx-data"  # 配额 ledger 的 skill 键（与 MX_DAILY_LIMIT_MX_DATA 对应）
SEARCH_SKILL = "mx-search"  # 与 MX_DAILY_LIMIT_MX_SEARCH 对应（issue #24/T2）


def extract_news_items(body: dict) -> list[dict]:
    """从 news-search 响应 body 取条目列表，按 news_code（条目 code）去重。

    路径 data.data.llmSearchResponse.data；实测单次响应内有重复条目，
    去重保留首次出现。内层结构缺失按空列表处理（不抛异常）。
    """
    inner = (body.get("data") or {}).get("data") or {}
    items = ((inner.get("llmSearchResponse") or {}).get("data")) or []
    seen: set = set()
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if code is not None:
            if code in seen:
                continue
            seen.add(code)
        out.append(item)
    return out


class _MxQueryable(Protocol):
    def query(self, tool_query: str) -> dict: ...


class _MxSearchable(Protocol):
    def search(self, query: str) -> dict: ...


def _mx_exchange(
    skill: str,
    tool: str,
    params: dict,
    invoke: Callable[[Any], dict],
    client: Any | None,
    quota: MxQuota | None,
) -> tuple[dict | None, dict, dict | None]:
    """mx 调用公共骨架：配额先检（触顶不调上游）→ 调上游 → 触达即记账
    （ledger 失败降级日志）→ code 判定。返回 (body, quota_snapshot, error)；
    error 非 None 即失败（body 必为 None），否则 body 为 code==0 的上游响应。
    """
    if quota is None:
        quota = MxQuota()
    snap = quota.snapshot(skill)
    if quota.is_exhausted(skill):
        return None, snap, _error(
            tool,
            params,
            f"妙想 {skill} 当日配额触顶（{snap['used']}/{snap['limit']}），未调用上游",
            quota=snap,
        )
    try:
        if client is None:
            client = MxClient()
    except MXError as e:
        # key 缺失等构造期错误：未触达上游，不计配额
        return None, snap, _error(tool, params, str(e), quota=snap)
    mx_error: MXError | None = None
    body: dict | None = None
    try:
        body = invoke(client)
    except MXError as e:
        mx_error = e  # 传输错误：上游已触达，照样记账
    except Exception as e:  # noqa: BLE001 - 工具层任何路径不抛异常
        log.exception("%s 内部错误", tool)
        return None, snap, _error(tool, params, f"内部错误：{e}", quota=snap)
    try:
        quota.record(skill)
    except Exception:  # noqa: BLE001 - ledger 写盘失败不拖垮结果
        log.warning("配额 ledger 记账失败（不影响本次结果）", exc_info=True)
    snap = quota.snapshot(skill)
    if mx_error is not None:
        return None, snap, _error(tool, params, str(mx_error), quota=snap)
    assert body is not None
    code = body.get("code")
    if code != 0:
        message = body.get("message") or "（无 message）"
        return None, snap, _error(
            tool,
            params,
            f"妙想 {skill} 上游错误 code={code}：{message}",
            upstream_code=code,
            upstream_message=body.get("message"),
            quota=snap,
        )
    return body, snap, None


def mx_query(
    tool_query: str,
    client: _MxQueryable | None = None,
    quota: MxQuota | None = None,
) -> dict:
    """mx-data 透传：自然语言问句 → 上游原始 JSON；本地每日配额先检后记。"""
    tool = "mx_query"
    params = {"tool_query": tool_query}
    body, snap, err = _mx_exchange(
        SKILL, tool, params, lambda c: c.query(tool_query), client, quota
    )
    if err is not None:
        return err
    assert body is not None
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "data": body,
        "quota": snap,
    }


def mx_search(
    query: str,
    subject_type: str = "market",
    subject_code: str = "_market",
    client: _MxSearchable | None = None,
    quota: MxQuota | None = None,
) -> dict:
    """mx-search 资讯搜索并落库：配额先检后记（语义同 mx_query），成功时按
    news_code 去重后 upsert news_items（幂等），返回落库计数与去重后条目。"""
    tool = "mx_search"
    params = {"query": query, "subject_type": subject_type, "subject_code": subject_code}
    body, snap, err = _mx_exchange(
        SEARCH_SKILL, tool, params, lambda c: c.search(query), client, quota
    )
    if err is not None:
        return err
    assert body is not None
    items = extract_news_items(body)
    conn, conn_err = connect()
    if conn_err:
        return _error(tool, params, conn_err, quota=snap)
    try:
        report = upsert_news_items(conn, subject_type, subject_code, items)
    except Exception as e:  # noqa: BLE001 - 工具层任何路径不抛异常
        log.exception("mx_search 落库失败")
        return _error(tool, params, f"落库失败：{e}", quota=snap)
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "rows": len(items),
        "inserted": report["inserted"],
        "updated": report["updated"],
        "skipped": report["skipped"],
        "items": items,
        "quota": snap,
    }
