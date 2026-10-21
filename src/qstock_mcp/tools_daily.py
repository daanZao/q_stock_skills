"""fetch/query 能力面核心：fetch_daily 抓取落库，query_daily 自愈查询。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake 适配器 +
真实 PG 测试库（见 tests/）。查询自愈契约（issue #3）：库里缺数据时只补
请求区间的头部和尾部缺口（中段缺口视为停牌/非交易日），按腾讯 → 新浪 →
baostock fallback（issue #112，东财退出个股日线）；全失败时报错并给出
attempted_sources，绝不返回过期/伪造数据。
盘中禁写（issue #78）：交易时段内（交易日 09:30–15:00 东八区）当日行不落库
（未收盘数据非最终值），fetch/query 正常返回数据并带 note；盘后行为不变。

三源对拍（issue #112）在独立模块 `daily_parity.py`：逐源直取同一交易日 bar 做
字段级比对、不落库——固定 CLI `qstock-scheduler daily --check` 的入口。
"""

import logging
from datetime import datetime, timezone
from typing import NamedTuple, Sequence

from .adapters import DailyAdapter, default_daily_adapters
from .dates import is_trading_time, resolve_range, today_str
from .db import connect
from .fetch_chain import AllSourcesFailed, aggregate_error_kind, fetch_with_fallback
from .gaps import head_tail_gaps
from .ledger import record_collection, summarize_attempted
from .output import error as _error
from .repository import select_daily, select_dates, upsert_daily

log = logging.getLogger(__name__)


def ensure_coverage(
    conn,
    adapters: Sequence[DailyAdapter],
    stock_code: str,
    adj: str,
    start: str,
    end: str,
    defer_dates: frozenset[str] = frozenset(),
) -> dict:
    """补齐 [start, end] 的头尾缺口，返回 {"ok", "segments", "deferred_rows"}。

    公共能力：fetch/query 自愈与 init 全量回溯（issue #8）共用。
    defer_dates：禁写日期集（盘中禁写防线，issue #78）——命中行不落库，
    收集进 deferred_rows 由调用方决定如何呈现。
    """
    existing = select_dates(conn, stock_code, adj, start, end)
    segments: list[dict] = []
    deferred_rows: list[dict] = []
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
                "deferred_rows": deferred_rows,
            }
        keep = [r for r in result["rows"] if r["trade_date"] not in defer_dates]
        deferred = [r for r in result["rows"] if r["trade_date"] in defer_dates]
        n = upsert_daily(conn, stock_code, adj, keep, result["source"])
        segment = {
            "start": seg_start,
            "end": seg_end,
            "source": result["source"],
            "rows": n,
        }
        if result.get("adjust_fallback"):
            # 复权回落可见化（issue #112）：上游缺复权键按 day 数据落库
            segment["adjust_fallback"] = True
        if deferred:
            segment["deferred"] = len(deferred)
            deferred_rows.extend({**r, "source": result["source"]} for r in deferred)
        segments.append(segment)
    return {"ok": True, "segments": segments, "deferred_rows": deferred_rows}


def _ledger_daily(
    conn, start: str, end: str, segments: list[dict], started: datetime
) -> None:
    """日线采集落账（issue #77）：一次区间补抓一行，trade_date 记区间尾日。"""
    rows = sum(s["rows"] for s in segments)
    deferred = sum(s.get("deferred", 0) for s in segments)
    sources = ",".join(sorted({s["source"] for s in segments}))
    seg_detail = "; ".join(
        f"{s['start']}~{s['end']}:{s['source']}:{s['rows']}" for s in segments
    )
    if deferred:
        seg_detail += f"; deferred(盘中禁写)={deferred}"
    if any(s.get("adjust_fallback") for s in segments):
        seg_detail += "; 复权回落=day（上游缺复权键）"
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


def _mark_suspension(row: dict) -> dict:
    """停牌标注（issue #80）：tradestatus 权威优先（0 停牌/1 正常），缺失时
    启发式兜底（OHLC 全等且 volume/change_percent 为 null 即停牌占位行）。

    停牌占位行（行存在但无成交）与数据缺失行（无行）由此在数据层可区分。
    """
    ts = row.get("tradestatus")
    if ts is not None:
        row["suspended"] = ts == 0
        row["suspended_basis"] = "tradestatus"
        return row
    ohlc = (row.get("open"), row.get("high"), row.get("low"), row.get("close"))
    if (
        None not in ohlc
        and ohlc[0] == ohlc[1] == ohlc[2] == ohlc[3]
        and row.get("volume") is None
        and row.get("change_percent") is None
    ):
        row["suspended"] = True
        row["suspended_basis"] = "heuristic"
    else:
        row["suspended"] = False
        row["suspended_basis"] = None
    return row


class _Ctx(NamedTuple):
    """_run 公共骨架的产物：参数回显、解析后区间、自愈分段、库内行、盘中禁写行。"""

    params: dict
    start: str
    end: str
    segments: list[dict]
    rows: list[dict]
    deferred_rows: list[dict]


_INTRADAY_NOTE = (
    "盘中时段：当日未收盘数据不落库（盘中禁写防线，issue #78），收盘后可正常补采"
)

_ADJUST_FALLBACK_NOTE = (
    "上游缺复权键，已按不复权（day）数据落库——响应无法区分"
    "“无复权事件”与“上游不提供调整序列”（ADR 0010）"
)


def _run(stock_code: str, days, start, end, adj, adapters, tool: str, now=None):
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
        adapters = default_daily_adapters()
    conn, err = connect()
    if err:
        return None, _error(tool, params, err)
    try:
        started = datetime.now(timezone.utc)
        # 盘中禁写（issue #78）：交易时段内当日行不落库（未收盘数据非最终值）
        defer = frozenset({today_str(now)}) if is_trading_time(now) else frozenset()
        cover = ensure_coverage(
            conn, adapters, stock_code, adj, start_d, end_d, defer_dates=defer
        )
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
    return _Ctx(params, start_d, end_d, cover["segments"], rows, cover["deferred_rows"]), None


def fetch_daily(
    stock_code: str,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    adj: str = "qfq",
    adapters: Sequence[DailyAdapter] | None = None,
    now=None,
) -> dict:
    """抓取个股日线并落库：只补头尾缺口，按业务键 upsert（幂等）。

    盘中时段（交易日 09:30–15:00）当日行不落库（盘中禁写防线，issue #78），
    响应带 note；now 为测试注入的时钟。
    """
    ctx, err = _run(stock_code, days, start, end, adj, adapters, "fetch_daily", now)
    if err:
        return err
    out = {
        "status": "ok",
        "tool": "fetch_daily",
        "params": ctx.params,
        "range": {"start": ctx.start, "end": ctx.end},
        "data_range": _data_range(ctx.rows),
        "segments": ctx.segments,
        "rows_upserted": sum(s["rows"] for s in ctx.segments),
    }
    if ctx.deferred_rows:
        out["note"] = _INTRADAY_NOTE
        out["deferred_rows"] = len(ctx.deferred_rows)
    if any(s.get("adjust_fallback") for s in ctx.segments):
        out["note"] = (
            f"{out['note']}；{_ADJUST_FALLBACK_NOTE}"
            if out.get("note")
            else _ADJUST_FALLBACK_NOTE
        )
    return out


def query_daily(
    stock_code: str,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    adj: str = "qfq",
    adapters: Sequence[DailyAdapter] | None = None,
    now=None,
) -> dict:
    """查询个股日线：库里缺数据自动补抓（自愈）后返回完整区间。

    盘中时段当日行不落库但随响应返回（note 标注盘中数据，issue #78）。
    """
    ctx, err = _run(stock_code, days, start, end, adj, adapters, "query_daily", now)
    if err:
        return err
    rows = ctx.rows
    if ctx.deferred_rows:
        seen = {r["trade_date"] for r in rows}
        rows = sorted(
            rows + [r for r in ctx.deferred_rows if r["trade_date"] not in seen],
            key=lambda r: r["trade_date"],
        )
    rows = rows[-days:] if days is not None else rows
    rows = [_mark_suspension(dict(r)) for r in rows]
    out = {
        "status": "ok",
        "tool": "query_daily",
        "params": ctx.params,
        "range": {"start": ctx.start, "end": ctx.end},
        "data_range": _data_range(rows),
        "count": len(rows),
        "healed": ctx.segments,
        "rows": rows,
    }
    if ctx.deferred_rows:
        out["note"] = _INTRADAY_NOTE + "；响应中当日行为盘中未收盘数据"
    return out
