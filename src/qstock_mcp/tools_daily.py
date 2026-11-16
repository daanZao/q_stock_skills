"""fetch/query 能力面：raw+B 持续采集、派生查询与 legacy 兼容。

#167 的写入口由 `adjustment_pipeline` 承载：baostock 优先取 raw，逐日确认累计 B，
再重建 hfq/state；因子未知与替代源权威字段缺失均不得判 complete。legacy 查询
仍走腾讯 → 新浪 → baostock 与旧表。new 查询在新窗口内盘后自愈，盘中当日禁写。
全失败返回结构化错误与 attempted_sources，不伪造数据。

三源对拍在 `daily_parity.py`，固定 CLI 为 `qstock-scheduler daily --check`。
"""

import logging
from datetime import datetime, timezone
from typing import NamedTuple, Sequence

from .adapters import DailyAdapter, default_adjustment_adapters
from .adjustment import adjustment_window_start
from .adjustment_pipeline import ensure_adjustment_coverage
from .baostock_errors import BaostockError
from .dates import is_trading_time, resolve_range, today_str
from .db import connect
from .fetch_chain import AllSourcesFailed, aggregate_error_kind, fetch_with_fallback
from .gaps import head_tail_gaps
from .ledger import record_collection, summarize_attempted
from .output import error as _error
from .price_reader import NewPriceReader, PriceQueryError, get_price_reader
from .repository import select_daily, select_dates, upsert_daily
from .tools_baostock import baostock_error

log = logging.getLogger(__name__)


def ensure_coverage(
    conn,
    adapters: Sequence[DailyAdapter],
    stock_code: str,
    adj: str,
    start: str,
    end: str,
    defer_dates: frozenset[str] = frozenset(),
    force: bool = False,
) -> dict:
    """补齐 [start, end] 的头尾缺口，返回 {"ok", "segments", "deferred_rows"}。

    仅承载 legacy 查询自愈；#167 新写入走 adjustment_pipeline。
    defer_dates：禁写日期集（盘中禁写防线，issue #78）——命中行不落库，
    收集进 deferred_rows 由调用方决定如何呈现。force=True 时整窗重抓。
    """
    existing = select_dates(conn, stock_code, adj, start, end)
    gaps = [(start, end)] if force else head_tail_gaps(existing, start, end)
    segments: list[dict] = []
    deferred_rows: list[dict] = []
    for seg_start, seg_end in gaps:
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
    conn, start: str, end: str, segments: list[dict], started: datetime,
    section: str = "stock_daily",
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
        section=section,
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
    params: dict
    start: str
    end: str
    segments: list[dict]
    rows: list[dict]
    deferred_rows: list[dict]
    snapshot: dict | None
    factor_errors: list[dict]
    coverage: str | None


_INTRADAY_NOTE = (
    "盘中时段：当日未收盘数据不落库（盘中禁写防线，issue #78），收盘后可正常补采"
)

_ADJUST_FALLBACK_NOTE = (
    "上游缺复权键，已按不复权（day）数据落库——响应无法区分"
    "“无复权事件”与“上游不提供调整序列”（ADR 0010）"
)


def _run(
    stock_code: str, days, start, end, adj, adapters, tool: str, now=None,
    force: bool = False, adjustment: bool = True,
):
    params = {
        "stock_code": stock_code,
        "adj": adj,
        "days": days,
        "start": start,
        "end": end,
    }
    if force:
        params["force"] = True
    try:
        start_d, end_d = resolve_range(days, start, end)
    except ValueError as e:
        return None, _error(tool, params, str(e))
    if adjustment and start_d < adjustment_window_start():
        return None, _error(
            tool,
            params,
            "raw+B 写入口仅支持 QSTOCK_ADJ_WINDOW_START 起的窗口，不从 legacy qfq 反推",
            error_kind="invalid_range",
        )
    if adapters is None:
        if adjustment:
            adapters = default_adjustment_adapters()
        else:
            from .adapters import default_daily_adapters

            adapters = default_daily_adapters()
    conn, err = connect()
    if err:
        return None, _error(tool, params, err)
    try:
        started = datetime.now(timezone.utc)
        defer = frozenset({today_str(now)}) if is_trading_time(now) else frozenset()
        if adjustment:
            cover = ensure_adjustment_coverage(
                conn, adapters, stock_code, start_d, end_d,
                defer_dates=defer, force=force,
            )
        else:
            cover = ensure_coverage(
                conn, adapters, stock_code, adj, start_d, end_d,
                defer_dates=defer, force=force,
            )
        if cover["ok"] and cover["segments"]:
            # 采集台账（issue #77）：真实发生抓取/写库才落账；无缺口零动作不落账
            _ledger_daily(
                conn, start_d, end_d, cover["segments"], started,
                section="stock_daily_raw" if adjustment else "stock_daily",
            )
        if not cover["ok"]:
            record_collection(
                conn,
                trade_date=end_d,
                section="stock_daily_raw" if adjustment else "stock_daily",
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
        coverage = None
        if tool == "fetch_daily":
            read_result = NewPriceReader().query_daily(
                conn, stock_code, "none", start_d, end_d
            )
            rows = read_result["rows"]
            coverage = read_result["adjust"]["coverage"]
        else:
            rows = select_daily(conn, stock_code, adj, start_d, end_d)
    except BaostockError as exc:
        return None, baostock_error(tool, params, exc)
    finally:
        conn.close()
    return _Ctx(
        params, start_d, end_d, cover["segments"], rows, cover["deferred_rows"],
        cover.get("snapshot"), cover.get("factor_errors", []), coverage,
    ), None


def fetch_daily(
    stock_code: str,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    adj: str = "qfq",
    adapters: Sequence[DailyAdapter] | None = None,
    now=None,
    force: bool = False,
) -> dict:
    """抓取 raw 与累计 B 并重建派生；adj 仅为兼容保留，不选择写入口径。"""
    ctx, err = _run(
        stock_code, days, start, end, adj, adapters, "fetch_daily", now, force=force
    )
    if err:
        return err
    partial = bool(ctx.factor_errors) or (
        ctx.snapshot is not None and ctx.snapshot["coverage"] == "partial"
    )
    out = {
        "status": "partial_error" if partial else "ok",
        "tool": "fetch_daily",
        "params": ctx.params,
        "range": {"start": ctx.start, "end": ctx.end},
        "data_range": _data_range(ctx.rows),
        "segments": ctx.segments,
        "rows_upserted": sum(s["rows"] for s in ctx.segments),
        "adjust": {
            "coverage": ctx.coverage or ("partial" if partial else "unknown")
        },
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


def _query_derived(reader, stock_code, days, start, end, adj, adapters, now) -> dict:
    params = {"stock_code": stock_code, "adj": adj, "days": days, "start": start, "end": end}
    try:
        start_d, end_d = resolve_range(days, start, end)
    except ValueError as e:
        return _error("query_daily", params, str(e))
    conn, err = connect()
    if err:
        return _error("query_daily", params, err)
    healed: list[dict] = []
    deferred_rows: list[dict] = []
    try:
        window_start = adjustment_window_start()
        should_heal = end_d >= window_start and start_d >= window_start
        if should_heal:
            if adapters is None:
                adapters = default_adjustment_adapters()
            started = datetime.now(timezone.utc)
            defer = frozenset({today_str(now)}) if is_trading_time(now) else frozenset()
            try:
                cover = ensure_adjustment_coverage(
                    conn, adapters, stock_code, start_d, end_d, defer_dates=defer
                )
            except BaostockError as exc:
                return baostock_error("query_daily", params, exc)
            healed = cover["segments"]
            deferred_rows = cover["deferred_rows"]
            if healed:
                _ledger_daily(
                    conn, start_d, end_d, healed, started, section="stock_daily_raw"
                )
            if not cover["ok"]:
                record_collection(
                    conn,
                    trade_date=end_d,
                    section="stock_daily_raw",
                    status="error",
                    error_kind=aggregate_error_kind(cover["attempted_sources"]),
                    detail=summarize_attempted(cover["attempted_sources"]),
                    started_at=started,
                )
                return _error(
                    "query_daily",
                    params,
                    "分段补抓失败（各数据源错误见 attempted_sources）",
                    failed_segment=cover["failed_segment"],
                    attempted_sources=cover["attempted_sources"],
                    healed=healed,
                )
        try:
            result = reader.query_daily(conn, stock_code, adj, start_d, end_d)
        except PriceQueryError as e:
            return _error("query_daily", params, str(e), error_kind=e.error_kind)
    finally:
        conn.close()
    rows = result["rows"]
    rows = rows[-days:] if days is not None else rows
    rows = [_mark_suspension(dict(row)) for row in rows]
    out = {
        "status": "ok",
        "tool": "query_daily",
        "params": params,
        "range": {"start": start_d, "end": end_d},
        "data_range": _data_range(rows),
        "count": len(rows),
        "healed": healed,
        "adjust": result["adjust"],
        "rows": rows,
    }
    if result["adjust"]["coverage"] != "complete":
        out["note"] = result.get("note", "复权数据覆盖不完整")
    if deferred_rows:
        out["note"] = (
            f"{out['note']}；{_INTRADAY_NOTE}" if out.get("note") else _INTRADAY_NOTE
        )
        out["deferred_rows"] = len(deferred_rows)
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
    """查询个股日线：新窗口内盘后自愈写 raw+B；盘中当日行禁写。"""
    reader = get_price_reader()
    if hasattr(reader, "query_daily"):
        return _query_derived(reader, stock_code, days, start, end, adj, adapters, now)
    ctx, err = _run(
        stock_code, days, start, end, adj, adapters, "query_daily", now,
        adjustment=False,
    )
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
    for row in rows:
        row["price_basis"] = "legacy_qfq" if adj == "qfq" else ("raw" if adj == "none" else adj)
    out = {
        "status": "ok",
        "tool": "query_daily",
        "params": ctx.params,
        "range": {"start": ctx.start, "end": ctx.end},
        "data_range": _data_range(rows),
        "count": len(rows),
        "healed": ctx.segments,
        "adjust": {
            "anchor": rows[-1]["trade_date"] if rows else None,
            "events": [],
            "coverage": "partial",
        },
        "rows": rows,
        "note": "legacy 价格口径未接入新复权覆盖核验",
    }
    if ctx.deferred_rows:
        out["note"] += "；" + _INTRADAY_NOTE + "；响应中当日行为盘中未收盘数据"
    return out
