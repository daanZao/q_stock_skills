"""fetch/query 能力面核心：fetch_market_snapshot 全市场快照落库，query_snapshot 库内查询。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake 适配器 +
真实 PG 测试库（见 tests/）。快照契约（issue #4）：单次全市场调用，
API 返回的最新交易日优先于传入日期；按 (trade_date, stock_code) 业务键
幂等 upsert；全失败时报错并给出 attempted_sources，绝不伪造数据。
"""

import logging
from datetime import date
from typing import Sequence

from .adapters import SnapshotAdapter, default_adapters
from .dates import normalize_date
from .db import connect
from .fetch_chain import AllSourcesFailed, fetch_snapshot_with_fallback
from .output import error as _error
from .repository import select_latest_snapshot_date, select_snapshot, upsert_snapshot

log = logging.getLogger(__name__)


def fetch_market_snapshot(
    trade_date: str | None = None,
    adapters: Sequence[SnapshotAdapter] | None = None,
) -> dict:
    """抓取全市场快照并落库：单次调用，按业务键 upsert（幂等）。"""
    tool = "fetch_market_snapshot"
    params = {"trade_date": trade_date}
    passed = None
    if trade_date is not None:
        try:
            passed = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    if adapters is None:
        adapters = default_adapters()
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        try:
            result = fetch_snapshot_with_fallback(adapters)
        except AllSourcesFailed as e:
            return _error(
                tool,
                params,
                "全部数据源失败（各源错误见 attempted_sources）",
                attempted_sources=e.attempted,
            )
        if result["trade_date"] is not None:
            effective, origin = result["trade_date"], "api"
        elif passed is not None:
            effective, origin = passed, "param"
        else:
            effective, origin = date.today().strftime("%Y%m%d"), "today"
        n = upsert_snapshot(conn, effective, result["rows"], result["source"])
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "trade_date": effective,
        "trade_date_origin": origin,
        "source": result["source"],
        "rows_upserted": n,
        "attempted_sources": result["attempted_sources"],
    }


def query_snapshot(
    trade_date: str | None = None,
    stock_code: str | None = None,
) -> dict:
    """库内快照查询：按日期/代码过滤；日期缺省取库内最新交易日。"""
    tool = "query_snapshot"
    params = {"trade_date": trade_date, "stock_code": stock_code}
    if trade_date is not None:
        try:
            trade_date = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        if trade_date is None:
            trade_date = select_latest_snapshot_date(conn)
        rows = select_snapshot(conn, trade_date, stock_code)
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "trade_date": trade_date,
        "count": len(rows),
        "rows": rows,
    }
