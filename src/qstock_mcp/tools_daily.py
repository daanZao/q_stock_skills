"""fetch/query 能力面核心：fetch_daily 抓取落库，query_daily 自愈查询。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake 适配器 +
真实 PG 测试库（见 tests/）。查询自愈契约（issue #3）：库里缺数据时只补
请求区间的头部和尾部缺口（中段缺口视为停牌/非交易日），按 efinance →
akshare → baostock fallback；全失败时报错并给出 attempted_sources，
绝不返回过期/伪造数据。
"""

import logging
from datetime import datetime, timezone
from typing import NamedTuple, Sequence

from .adapters import DailyAdapter, default_adapters
from .dates import resolve_range
from .db import connect
from .fetch_chain import AllSourcesFailed, aggregate_error_kind, fetch_with_fallback
from .gaps import head_tail_gaps
from .ledger import record_collection, summarize_attempted
from .output import error as _error
from .repository import select_daily, select_dates, upsert_daily

log = logging.getLogger(__name__)


def ensure_coverage(
    conn, adapters: Sequence[DailyAdapter], stock_code: str, adj: str, start: str, end: str
) -> dict:
    """补齐 [start, end] 的头尾缺口，返回 {"ok", "segments", ...}。

    公共能力：fetch/query 自愈与 init 全量回溯（issue #8）共用。
    """
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


def _ledger_daily(
    conn, start: str, end: str, segments: list[dict], started: datetime
) -> None:
    """日线采集落账（issue #77）：一次区间补抓一行，trade_date 记区间尾日。"""
    rows = sum(s["rows"] for s in segments)
    sources = ",".join(sorted({s["source"] for s in segments}))
    seg_detail = "; ".join(
        f"{s['start']}~{s['end']}:{s['source']}:{s['rows']}" for s in segments
    )
    record_collection(
        conn,
        trade_date=end,
        section="stock_daily",
        status="ok" if rows > 0 else "empty",
        source=sources,
        rows=rows,
        error_kind=None if rows > 0 else "no_data_for_date",
        detail=f"range {start}~{end}; {seg_detail}",
        started_at=started,
    )


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
    conn, err = connect()
    if err:
        return None, _error(tool, params, err)
    try:
        started = datetime.now(timezone.utc)
        cover = ensure_coverage(conn, adapters, stock_code, adj, start_d, end_d)
        if cover["ok"] and cover["segments"]:
            # 采集台账（issue #77）：真实发生抓取/写库才落账；无缺口零动作不落账
            _ledger_daily(conn, start_d, end_d, cover["segments"], started)
        if not cover["ok"]:
            record_collection(
                conn,
                trade_date=end_d,
                section="stock_daily",
                status="error",
                error_kind=aggregate_error_kind(cover["attempted_sources"]),
                detail=f"range {start_d}~{end_d}; "
                + (summarize_attempted(cover["attempted_sources"]) or ""),
                started_at=started,
            )
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
