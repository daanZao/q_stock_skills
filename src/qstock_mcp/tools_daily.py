"""fetch/query 能力面核心：fetch_daily 抓取落库，query_daily 自愈查询。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake 适配器 +
真实 PG 测试库（见 tests/）。查询自愈契约（issue #3）：库里缺数据时只补
请求区间的头部和尾部缺口（中段缺口视为停牌/非交易日），按 efinance →
akshare → baostock fallback；全失败时报错并给出 attempted_sources，
绝不返回过期/伪造数据。
"""

import logging
from typing import Any, NamedTuple

import psycopg2

from .adapters import DailyAdapter, default_adapters
from .dates import resolve_range
from .db import MissingDsnError, get_dsn
from .fetch_chain import AllSourcesFailed, fetch_with_fallback
from .gaps import head_tail_gaps
from .repository import select_daily, select_dates, upsert_daily

log = logging.getLogger(__name__)


def _connect() -> tuple[Any, str | None]:
    try:
        dsn = get_dsn()
    except MissingDsnError as e:
        return None, str(e)
    try:
        return psycopg2.connect(dsn), None
    except Exception as e:  # noqa: BLE001 - 工具层把异常收敛为自描述错误
        log.exception("数据库连接失败")
        return None, f"数据库连接失败: {e}"


def _ensure_coverage(
    conn, adapters: list[DailyAdapter], stock_code: str, adj: str, start: str, end: str
) -> dict:
    """补齐 [start, end] 的头尾缺口，返回 {"ok", "segments", ...}。"""
    existing = select_dates(conn, stock_code, adj, start, end)
    segments: list[dict] = []
    for seg_start, seg_end in head_tail_gaps(existing, start, end):
        try:
            result = fetch_with_fallback(adapters, stock_code, seg_start, seg_end, adj)
        except AllSourcesFailed as e:
            log.warning("分段补抓失败 %s~%s: %s", seg_start, seg_end, e)
            return {
                "ok": False,
                "failed_segment": {"start": seg_start, "end": seg_end},
                "attempted_sources": e.attempted,
                "segments": segments,
            }
        n = upsert_daily(conn, stock_code, adj, result["rows"], result["source"])
        segments.append(
            {"start": seg_start, "end": seg_end, "source": result["source"], "rows": n}
        )
    return {"ok": True, "segments": segments}


def _error(tool: str, params: dict, msg: str, **extra) -> dict:
    return {"status": "error", "tool": tool, "params": params, "error": msg, **extra}


def _data_range(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    return {"start": rows[0]["trade_date"], "end": rows[-1]["trade_date"]}


class _Ctx(NamedTuple):
    """_run 公共骨架的产物：参数回显、解析后区间、自愈分段、库内行。"""

    params: dict
    start: str
    end: str
    segments: list[dict]
    rows: list[dict]


def _run(stock_code: str, days, start, end, adj, adapters, tool: str):
    """公共骨架：参数解析 → 连接 → 头尾自愈。失败返回 (None, error_dict)。"""
    params = {
        "stock_code": stock_code,
        "adj": adj,
        "days": days,
        "start": start,
        "end": end,
    }
    try:
        start_d, end_d = resolve_range(days, start, end)
    except ValueError as e:
        return None, _error(tool, params, str(e))
    if adapters is None:
        adapters = default_adapters()
    conn, err = _connect()
    if err:
        return None, _error(tool, params, err)
    try:
        cover = _ensure_coverage(conn, adapters, stock_code, adj, start_d, end_d)
        if not cover["ok"]:
            return None, _error(
                tool,
                params,
                "分段补抓失败（各数据源错误见 attempted_sources）",
                failed_segment=cover["failed_segment"],
                attempted_sources=cover["attempted_sources"],
                healed=cover["segments"],
            )
        rows = select_daily(conn, stock_code, adj, start_d, end_d)
    finally:
        conn.close()
    return _Ctx(params, start_d, end_d, cover["segments"], rows), None


def fetch_daily(
    stock_code: str,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    adj: str = "qfq",
    adapters: list[DailyAdapter] | None = None,
) -> dict:
    """抓取个股日线并落库：只补头尾缺口，按业务键 upsert（幂等）。"""
    ctx, err = _run(stock_code, days, start, end, adj, adapters, "fetch_daily")
    if err:
        return err
    return {
        "status": "ok",
        "tool": "fetch_daily",
        "params": ctx.params,
        "range": {"start": ctx.start, "end": ctx.end},
        "data_range": _data_range(ctx.rows),
        "segments": ctx.segments,
        "rows_upserted": sum(s["rows"] for s in ctx.segments),
    }


def query_daily(
    stock_code: str,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    adj: str = "qfq",
    adapters: list[DailyAdapter] | None = None,
) -> dict:
    """查询个股日线：库里缺数据自动补抓（自愈）后返回完整区间。"""
    ctx, err = _run(stock_code, days, start, end, adj, adapters, "query_daily")
    if err:
        return err
    rows = ctx.rows[-days:] if days is not None else ctx.rows
    return {
        "status": "ok",
        "tool": "query_daily",
        "params": ctx.params,
        "range": {"start": ctx.start, "end": ctx.end},
        "data_range": _data_range(rows),
        "count": len(rows),
        "healed": ctx.segments,
        "rows": rows,
    }
