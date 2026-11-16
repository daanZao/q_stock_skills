"""持续采集管线：raw + 累计 B 事实写入并重建单票派生。"""

import time
import weakref
from typing import Any

from .adapters.base import FetchError, fetch_failure_kind
from .adjustment import rebuild_stock
from .dates import trading_days_between
from .fetch_chain import AllSourcesFailed, fetch_with_fallback
from .gaps import head_tail_gaps
from .ledger import record_collection


_FACTOR_DAYS: weakref.WeakKeyDictionary[Any, dict[str, Any]] = (
    weakref.WeakKeyDictionary()
)
_UPSTREAM_LAST: weakref.WeakKeyDictionary[Any, float] = weakref.WeakKeyDictionary()
# 2026-11-04/05 baostock 串行实测支持相邻请求 ≥1.5s；并发/长期 TPS 未验证。
_MIN_UPSTREAM_INTERVAL_S = 1.5
sleep = time.sleep
monotonic = time.monotonic


def _pace(adapter) -> None:
    if adapter.name != "baostock":
        return
    previous = _UPSTREAM_LAST.get(adapter)
    if previous is not None:
        sleep(max(0.0, _MIN_UPSTREAM_INTERVAL_S - (monotonic() - previous)))
    _UPSTREAM_LAST[adapter] = monotonic()


class _PacedDailyAdapter:
    def __init__(self, delegate):
        self.delegate = delegate
        self.name = delegate.name

    def fetch_daily(self, stock_code, start, end, adj="qfq"):
        _pace(self.delegate)
        return self.delegate.fetch_daily(stock_code, start, end, adj)


def _factor_day(adapter, trade_date: str, force: bool) -> list[dict]:
    state = _FACTOR_DAYS.setdefault(adapter, {"events": {}})
    if trade_date not in state["events"]:
        _pace(adapter)
        state["events"][trade_date] = adapter.fetch_daily_adjust_factor(trade_date)
    return state["events"][trade_date]


def _fetch_raw(adapters, stock_code: str, start: str, end: str) -> dict:
    attempted: list[dict] = []
    last_empty: dict | None = None
    for adapter in adapters:
        try:
            result = fetch_with_fallback(
                [_PacedDailyAdapter(adapter)], stock_code, start, end, "none"
            )
        except AllSourcesFailed as exc:
            attempted.extend(exc.attempted)
            continue
        if result["rows"]:
            result["attempted_sources"] = attempted
            return result
        attempted.append({
            "source": adapter.name,
            "attempts": 1,
            "error": None,
            "failure_kind": "empty",
        })
        last_empty = result
    if last_empty is not None:
        last_empty["attempted_sources"] = attempted
        return last_empty
    raise AllSourcesFailed(attempted)


def ensure_adjustment_coverage(
    conn,
    adapters,
    stock_code: str,
    start: str,
    end: str,
    defer_dates=frozenset(),
    force: bool = False,
) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date FROM stock_daily_raw"
            " WHERE stock_code=%s AND trade_date BETWEEN %s AND %s"
            " AND source='baostock'",
            (stock_code, start, end),
        )
        existing = {row[0] for row in cur.fetchall()}
        cur.execute(
            """
            SELECT DISTINCT ON (trade_date) trade_date, status
            FROM collection_log
            WHERE section='stock_adjust_factor' AND trade_date BETWEEN %s AND %s
              AND (
                  detail LIKE %s
                  OR detail LIKE 'adj-backfill event %%'
                  OR detail LIKE 'morning_batch%%'
              )
            ORDER BY trade_date, finished_at DESC, id DESC
            """,
            (start, end, f"daily:{stock_code}%"),
        )
        factor_status = dict(cur.fetchall())
        cur.execute(
            "SELECT baseline_factor FROM stock_factor_state WHERE stock_code=%s",
            (stock_code,),
        )
        state = cur.fetchone()
        cur.execute(
            "SELECT trade_date FROM stock_daily_raw"
            " WHERE stock_code=%s AND trade_date BETWEEN %s AND %s"
            " AND source<>'baostock'",
            (stock_code, start, end),
        )
        alternative_dates = {row[0] for row in cur.fetchall()}
        cur.execute(
            "SELECT DISTINCT error_kind FROM collection_log"
            " WHERE section='stock_factor_repair'"
            " AND (detail LIKE %s OR detail LIKE %s)"
            " AND error_kind<>'alternative_raw'",
            (f"daily:{stock_code}%", f"adj-backfill repair {stock_code} %"),
        )
        other_pending_reasons = {row[0] for row in cur.fetchall()}
    alternative_raw = bool(alternative_dates)
    baseline = state[0] if state is not None else None
    gaps = [(start, end)] if force else head_tail_gaps(existing, start, end)
    for day in sorted(alternative_dates):
        if not any(seg_start <= day <= seg_end for seg_start, seg_end in gaps):
            gaps.append((day, day))
    gaps.sort()
    segments: list[dict] = []
    deferred_rows: list[dict] = []
    raw_rows: list[dict] = []
    for seg_start, seg_end in gaps:
        try:
            result = _fetch_raw(adapters, stock_code, seg_start, seg_end)
        except AllSourcesFailed as exc:
            if raw_rows:
                rebuild_stock(
                    conn,
                    stock_code,
                    raw_rows,
                    [],
                    baseline_factor=baseline,
                    event_set_complete=False,
                    force_pending=any(
                        row.get("source") != "baostock" for row in raw_rows
                    ),
                    rebuild_from=min(row["trade_date"] for row in raw_rows),
                )
            return {
                "ok": False,
                "failed_segment": {"start": seg_start, "end": seg_end},
                "attempted_sources": exc.attempted,
                "segments": segments,
                "deferred_rows": deferred_rows,
            }
        source = result["source"]
        for row in result["rows"]:
            row = {**row, "source": source}
            if row["trade_date"] in defer_dates:
                deferred_rows.append(row)
            else:
                raw_rows.append(row)
        keep = [row for row in raw_rows if seg_start <= row["trade_date"] <= seg_end]
        segments.append({
            "start": seg_start, "end": seg_end, "source": source, "rows": len(keep),
        })

    factor_adapter = next(
        (adapter for adapter in adapters if hasattr(adapter, "fetch_daily_adjust_factor")),
        None,
    )
    factor_events: list[dict] = []
    authoritative_dates: list[str] = []
    factor_errors: list[dict] = []
    for day in trading_days_between(start, end):
        if day in defer_dates or (
            not force and factor_status.get(day) in ("ok", "empty")
        ):
            continue
        try:
            if factor_adapter is None:
                raise FetchError("baostock 因子源不可用", kind="unsupported")
            events = [
                {**event, "source": "baostock-adjust-factor"}
                for event in _factor_day(factor_adapter, day, force)
                if event["stock_code"] == stock_code
            ]
        except FetchError as exc:
            kind = fetch_failure_kind(exc)
            error_kind = "upstream_unreachable" if kind == "connection" else "fetch_failed"
            factor_errors.append({"trade_date": day, "error": str(exc), "error_kind": error_kind})
            record_collection(
                conn,
                trade_date=day,
                section="stock_adjust_factor",
                status="error",
                source="baostock",
                error_kind=error_kind,
                detail=f"daily:{stock_code}: {exc}",
            )
            continue
        factor_events.extend(events)
        authoritative_dates.append(day)
        record_collection(
            conn,
            trade_date=day,
            section="stock_adjust_factor",
            status="ok" if events else "empty",
            source="baostock",
            rows=len(events),
            error_kind=None if events else "no_data_for_date",
            detail=f"daily:{stock_code}",
        )

    authoritative_upgrade = alternative_raw and all(
        any(
            row.get("source") == "baostock" and row["trade_date"] == day
            for row in raw_rows
        )
        for day in alternative_dates
    )
    snapshot = None
    if raw_rows or authoritative_dates or factor_errors:
        rebuild_from = min(
            [row["trade_date"] for row in raw_rows]
            + authoritative_dates
            + [item["trade_date"] for item in factor_errors]
        )
        snapshot = rebuild_stock(
            conn,
            stock_code,
            raw_rows,
            factor_events,
            authoritative_event_dates=authoritative_dates,
            baseline_factor=baseline,
            event_set_complete=not factor_errors,
            force_pending=(
                any(row.get("source") != "baostock" for row in raw_rows)
                or (alternative_raw and not authoritative_upgrade)
            ),
            preserve_pending=not (authoritative_upgrade and not other_pending_reasons),
            rebuild_from=rebuild_from,
        )
        if not authoritative_upgrade and any(
            row.get("source") != "baostock" for row in raw_rows
        ):
            record_collection(
                conn,
                trade_date=max(
                    row["trade_date"] for row in raw_rows
                    if row.get("source") != "baostock"
                ),
                section="stock_factor_repair",
                status="error",
                source="daily",
                error_kind="alternative_raw",
                detail=f"daily:{stock_code}:alternative_raw",
            )
        if snapshot is not None:
            for audit in snapshot["factor_audit"]:
                if audit["status"] == "ok" or audit["ex_date"] < rebuild_from:
                    continue
                reason = (
                    "factor_mismatch" if audit["status"] == "mismatch"
                    else "price_path_unverified"
                )
                record_collection(
                    conn,
                    trade_date=audit["ex_date"],
                    section="stock_factor_repair",
                    status="error",
                    source="daily",
                    error_kind=reason,
                    detail=f"daily:{stock_code}:{audit['status']}",
                )
    return {
        "ok": True,
        "segments": segments,
        "deferred_rows": deferred_rows,
        "snapshot": snapshot,
        "factor_errors": factor_errors,
    }
