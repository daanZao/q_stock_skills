"""复权回填 A（issue #163）：逐日全市场事件集 + 只对有事件票重抓 raw。

范围与边界（spec #158 §6.1/§6.2）：
- 窗口内每个历史交易日调用一次 baostock `query_daily_adjust_factor(date)`；
  成功空结果记 empty，失败重试后仍失败记 error，绝不当作“当日无事件”。
- 事件集写入 `stock_adjust_factor`（B 只取 backAdjustFactor）；成功重查整日
  替换该日事件集以支持修订/撤销，受影响票重建，非事件票不写占位因子。
- 只对窗口内有事件的股票逐只重抓 `adjustflag=3` raw（含 tradestatus/turn/
  pctChg），amount 元→万元由适配层完成；失败重试并落台账。
- 首个事件用 `B0=B1×f1` 反推窗口基线；窗口首日即事件时额外取窗口外最后
  一个交易日作 D-1 计算输入，但不写入 hfq。B0/raw/pctChg 不可验证时
  `rebuild_stock` 置 NULL/NULL+pending，本模块再落待修复台账。
- 断点续跑读 `collection_log`：因子按日、raw 按票校验台账与库内事实仍有效
  才跳过；`--force` 忽略台账整窗重抓。全程串行，不接替代源。

数据源纪律：2026-11-04/05 已验证单会话串行、相邻请求 ≥1.5s、单请求 120s
外部硬超时；长期 TPS/并发上限与全历史完备率仍待验证。本模块固定串行并
默认 1.5s 间隔，不放量。
"""

import csv
import json
import logging
import math
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from time import monotonic

from psycopg2.extras import execute_values  # type: ignore[import-untyped]

from .adapters.base import FetchError, fetch_failure_kind
from .adjustment import rebuild_stock
from .dates import (
    is_trading_time,
    normalize_date,
    prev_trading_day,
    today_str,
    trading_day_window,
    trading_days_between,
)
from .db import connect, ensure_schema
from .ledger import record_collection
from .output import error as _error

log = logging.getLogger(__name__)

ADJ_BACKFILL_DEFAULT_START = "20230101"
# 2026-11-04/05 实测：baostock 串行、相邻请求 ≥1.5s 可承载；并发/长期 TPS
# 未验证，回填固定串行并使用该间隔（不据此推断全历史容量）。
ADJ_BACKFILL_THROTTLE_S = 1.5
ADJ_BACKFILL_RETRIES = 3
ADJ_BACKFILL_BACKOFF_S = (0.5, 1.0, 2.0)
PROGRESS_LOG_EVERY = 20
PROGRESS_LOG_INTERVAL_S = 60
RAW_DETAIL_PREFIX = "adj-backfill raw"
EVENT_SECTION = "stock_adjust_factor"
RAW_SECTION = "stock_daily_raw"
REPAIR_SECTION = "stock_factor_repair"
RETRACTED_SECTION = "stock_factor_retracted"
REPAIR_REASON_LABELS = {
    "baseline_unverified": "首个事件窗口基线不可验证",
    "factor_mismatch": "B 路径与权威 pctChg 价格路径相对误差 >0.1%",
    "price_path_unverified": "事件日缺少 D-1 raw 或权威 pctChg，价格路径不可验证",
    "raw_gap": "窗口内 raw 缺少交易日",
    "invalid_close": "close 为 NULL 或 ≤0，已隔离不派生",
    "raw_unavailable": "事件票 raw 抓取失败或上游无数据",
    "raw_refresh_failed": "raw 重抓失败，沿用库内已有 raw 重建",
}

sleep = time.sleep


def _pause(seconds: float) -> None:
    if seconds > 0:
        sleep(seconds)


class _Pacer:
    """只在真正发上游请求前按最小间隔等待；跳过项不占节流时间。"""

    def __init__(self, interval: float):
        self.interval = max(0.0, float(interval))
        self._last: float | None = None

    def before_request(self) -> None:
        if self.interval <= 0:
            return
        now = monotonic()
        if self._last is not None:
            wait = self.interval - (now - self._last)
            if wait > 0:
                _pause(wait)
        self._last = monotonic()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _valid_factor(value) -> bool:
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _event_detail_prefix(start: str, end: str) -> str:
    return f"adj-backfill event {start}~{end}"


def _raw_detail_prefix(stock_code: str, start: str, end: str) -> str:
    return f"{RAW_DETAIL_PREFIX} {stock_code} {start}~{end}"


def _repair_detail(stock_code: str, reason: str, detail: str) -> str:
    return (
        f"adj-backfill repair {stock_code} {reason}: {detail}"
    )[:500]


# ---------------------------------------------------------------- 台账断点


def _day_checkpoints(conn, start: str, end: str) -> dict[str, dict]:
    """每个交易日最近一次因子采集台账（含 morning 写入的权威行）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (trade_date) trade_date, status, rows, error_kind
            FROM collection_log
            WHERE section = %s AND trade_date BETWEEN %s AND %s
            ORDER BY trade_date, id DESC
            """,
            (EVENT_SECTION, start, end),
        )
        return {
            row[0]: {"status": row[1], "rows": row[2] or 0, "error_kind": row[3]}
            for row in cur.fetchall()
        }


def _load_retracted_codes(conn, start: str, end: str) -> set[str]:
    """读窗口内已落库的事件撤销标记（崩溃后续跑仍能重建被撤销票）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM collection_log"
            " WHERE section=%s AND trade_date BETWEEN %s AND %s ORDER BY id",
            (RETRACTED_SECTION, start, end),
        )
        rows = cur.fetchall()
    codes: set[str] = set()
    for (detail,) in rows:
        tokens = (detail or "").split(" ")
        if len(tokens) >= 3 and tokens[1] == "retracted":
            codes.add(tokens[2])
    return codes


def _event_counts(conn, start: str, end: str) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ex_date, count(*) FROM stock_adjust_factor"
            " WHERE ex_date BETWEEN %s AND %s GROUP BY ex_date",
            (start, end),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _raw_checkpoints(conn, start: str, end: str) -> dict[str, dict]:
    """raw 逐票最近一次回填台账（只认本模块 detail 前缀且窗口一致的行）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail, status FROM collection_log"
            " WHERE section = %s AND detail LIKE %s ORDER BY id DESC",
            (RAW_SECTION, RAW_DETAIL_PREFIX + " %"),
        )
        rows = cur.fetchall()
    result: dict[str, dict] = {}
    for detail, status in rows:
        tokens = (detail or "").split(" ")
        if len(tokens) < 4 or tokens[3] != f"{start}~{end}":
            continue
        stock_code = tokens[2]
        if stock_code not in result:
            result[stock_code] = {"status": status, "detail": detail}
    return result


def _raw_window_stats(conn, start: str, end: str) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, count(*), min(trade_date), max(trade_date)"
            " FROM stock_daily_raw WHERE trade_date BETWEEN %s AND %s"
            " GROUP BY stock_code",
            (start, end),
        )
        return {
            row[0]: {"rows": row[1], "first": row[2], "last": row[3]}
            for row in cur.fetchall()
        }


def _raw_dates(conn, stock_code: str, start: str, end: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date FROM stock_daily_raw"
            " WHERE stock_code=%s AND trade_date BETWEEN %s AND %s"
            " ORDER BY trade_date",
            (stock_code, start, end),
        )
        return [row[0] for row in cur.fetchall()]


def _has_row(conn, stock_code: str, trade_date: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM stock_daily_raw WHERE stock_code=%s AND trade_date=%s",
            (stock_code, trade_date),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------- 事件集


def _replace_day_events(conn, trade_date: str, events: list[dict]) -> list[str]:
    """事务内整日替换该日事件集，返回被撤销的股票代码（旧有新无）。"""
    by_code = {event["stock_code"]: event for event in events}
    values = [
        [code, trade_date, event["factor_cum"], event.get("kind"),
         event.get("detail"), event.get("reg_date"),
         event.get("source", "baostock-adjust-factor")]
        for code, event in sorted(by_code.items())
    ]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code FROM stock_adjust_factor WHERE ex_date=%s",
            (trade_date,),
        )
        old_codes = {row[0] for row in cur.fetchall()}
        cur.execute("DELETE FROM stock_adjust_factor WHERE ex_date=%s", (trade_date,))
        if values:
            execute_values(
                cur,
                """
                INSERT INTO stock_adjust_factor (stock_code, ex_date, factor_cum,
                    kind, detail, reg_date, source)
                VALUES %s
                """,
                values,
            )
        retracted = sorted(old_codes - set(by_code))
        if retracted:
            # 与事件替换同事务落撤销标记：崩溃后断点续跑仍能知道哪些票需要
            # 按“无事件”重建，不会留下旧 hfq/state。
            execute_values(
                cur,
                "INSERT INTO collection_log (trade_date, section, status, source,"
                " rows, detail) VALUES %s",
                [
                    [trade_date, RETRACTED_SECTION, "ok", "adj-backfill", None,
                     f"adj-backfill retracted {code} {trade_date}"[:500]]
                    for code in retracted
                ],
            )
        conn.commit()
    return retracted


def _fetch_with_retries(operation: Callable[[], list[dict]], retries: int,
                        pacer: _Pacer) -> tuple[list[dict] | None, int, FetchError | None]:
    """串行调一次上游操作，按退避重试；返回 (rows|None, attempts, error)。"""
    last_error = None
    for attempt in range(1, retries + 1):
        pacer.before_request()
        try:
            return list(operation()), attempt, None
        except FetchError as e:
            last_error = e
            if attempt < retries:
                _pause(
                    ADJ_BACKFILL_BACKOFF_S[
                        min(attempt - 1, len(ADJ_BACKFILL_BACKOFF_S) - 1)
                    ]
                )
    return None, retries, last_error


def _fetch_factor_day(adapter, trade_date: str, retries: int,
                      pacer: _Pacer) -> dict:
    """调一次逐日因子接口，失败按退避重试；返回 attempts/events/error。"""
    events, attempts, error = _fetch_with_retries(
        lambda: adapter.fetch_daily_adjust_factor(trade_date), retries, pacer
    )
    return {"events": events, "attempts": attempts, "error": error}


def _ingest_event_day(conn, adapter, trade_date: str, start: str, end: str,
                      retries: int, pacer: _Pacer) -> dict:
    """抓取并落一个交易日的事件集；失败保留旧事件、落 error 台账。"""
    started = _utcnow()
    result = _fetch_factor_day(adapter, trade_date, retries, pacer)
    if result["events"] is None:
        error = result["error"]
        kind = fetch_failure_kind(error) if error is not None else "fetch_failed"
        if kind == "connection":
            kind = "upstream_unreachable"
        elif kind != "unsupported":
            kind = "fetch_failed"
        record_collection(
            conn, trade_date=trade_date, section=EVENT_SECTION, status="error",
            error_kind=kind,
            detail=(
                f"{_event_detail_prefix(start, end)} attempts={result['attempts']}: {error}"
            ),
            started_at=started,
        )
        return {
            "trade_date": trade_date, "status": "error", "rows": 0,
            "attempts": result["attempts"], "error_kind": kind,
            "detail": str(error), "retracted_codes": [],
        }

    events = result["events"]
    by_code: dict[str, dict] = {}
    invalid: list[str] = []
    for event in events:
        code = event.get("stock_code")
        if not code or not _valid_factor(event.get("factor_cum")):
            invalid.append(str(code))
            continue
        by_code[code] = event
    if invalid:
        record_collection(
            conn, trade_date=trade_date, section=EVENT_SECTION, status="error",
            error_kind="invalid_factor",
            detail=(
                f"{_event_detail_prefix(start, end)} attempts={result['attempts']}"
                f"; invalid={invalid[:20]}"
            ),
            started_at=started,
        )
        return {
            "trade_date": trade_date, "status": "error", "rows": 0,
            "attempts": result["attempts"], "error_kind": "invalid_factor",
            "detail": f"invalid factor rows: {invalid[:20]}", "retracted_codes": [],
        }

    retracted = _replace_day_events(conn, trade_date, list(by_code.values()))
    status = "ok" if by_code else "empty"
    record_collection(
        conn, trade_date=trade_date, section=EVENT_SECTION, status=status,
        source=adapter.name, rows=len(by_code),
        detail=(
            f"{_event_detail_prefix(start, end)} attempts={result['attempts']}"
            f"; stocks={len(by_code)}"
        ),
        started_at=started,
    )
    return {
        "trade_date": trade_date, "status": status, "rows": len(by_code),
        "attempts": result["attempts"], "error_kind": None,
        "detail": None, "retracted_codes": retracted,
    }


def _event_stock_rows(conn, start: str, end: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT stock_code, count(*), min(ex_date), max(ex_date)
            FROM stock_adjust_factor
            WHERE ex_date BETWEEN %s AND %s
            GROUP BY stock_code ORDER BY stock_code
            """,
            (start, end),
        )
        return [
            {"stock_code": row[0], "events": row[1], "first": row[2], "last": row[3]}
            for row in cur.fetchall()
        ]


# ---------------------------------------------------------------- raw + 派生


def _fetch_raw_stock(adapter, stock_code: str, start: str, end: str,
                     retries: int, pacer: _Pacer) -> dict:
    rows, attempts, error = _fetch_with_retries(
        lambda: adapter.fetch_daily(stock_code, start, end, adj="none"),
        retries, pacer,
    )
    return {"rows": rows, "attempts": attempts, "error": error}


def _has_valid_close(row: dict) -> bool:
    close = row.get("close")
    return close is not None and close > 0


def _window_rows(rows: list[dict], start: str, end: str) -> list[dict]:
    return [
        row for row in rows
        if start <= str(row.get("trade_date") or "") <= end
    ]


def _raw_gap_dates(raw_rows: list[dict], start: str, end: str) -> list[str]:
    dates = sorted(
        row["trade_date"] for row in raw_rows
        if start <= row["trade_date"] <= end
    )
    if len(dates) < 2:
        return []
    expected = trading_days_between(dates[0], dates[-1])
    actual = set(dates)
    return [day for day in expected if day not in actual]


def _reasons_for_snapshot(snapshot: dict, gap_dates: list[str],
                          refresh_failed: bool) -> dict[str, list[str]]:
    reasons: dict[str, list[str]] = {}
    state = snapshot.get("state")
    audit = snapshot.get("factor_audit") or []
    if state is None:
        if not snapshot.get("quarantined"):
            reasons.setdefault("raw_unavailable", []).append("无有效 raw")
    elif state.get("baseline_factor") is None:
        reasons.setdefault("baseline_unverified", []).append(
            f"latest_trade_date={state.get('latest_trade_date')}"
        )
    else:
        for item in audit:
            if item["status"] == "mismatch":
                reasons.setdefault("factor_mismatch", []).append(
                    f"{item['ex_date']} relative_error={item['relative_error']}"
                )
            elif item["status"] == "unverified":
                reasons.setdefault("price_path_unverified", []).append(
                    item["ex_date"]
                )
    if gap_dates:
        shown = ",".join(gap_dates[:20])
        suffix = "..." if len(gap_dates) > 20 else ""
        reasons.setdefault("raw_gap", []).append(f"{shown}{suffix}")
    if snapshot.get("quarantined"):
        reasons.setdefault("invalid_close", []).append(
            ",".join(row["trade_date"] for row in snapshot["quarantined"][:20])
        )
    if refresh_failed:
        reasons.setdefault("raw_refresh_failed", []).append("沿用库内 raw")
    return reasons


def _record_repairs(conn, stock_code: str, trade_date: str,
                    reasons: dict[str, list[str]]) -> list[dict]:
    repairs = []
    for reason, details in reasons.items():
        detail = "; ".join(str(item) for item in details)
        record_collection(
            conn, trade_date=trade_date, section=REPAIR_SECTION, status="error",
            source="adj-backfill", error_kind=reason,
            detail=_repair_detail(stock_code, reason, detail),
        )
        repairs.append({
            "stock_code": stock_code,
            "trade_date": trade_date,
            "reason": reason,
            "label": REPAIR_REASON_LABELS.get(reason, reason),
            "detail": detail,
        })
    return repairs


def _record_raw_ledger(conn, stock_code: str, start: str, end: str, *,
                       status: str, rows: int, attempts: int,
                       source: str = "baostock",
                       error_kind: str | None = None,
                       detail: str | None = None, started=None) -> None:
    record_collection(
        conn, trade_date=end, section=RAW_SECTION, status=status,
        source=source, rows=rows, error_kind=error_kind,
        detail=(
            f"{_raw_detail_prefix(stock_code, start, end)} attempts={attempts}"
            + (f": {detail}" if detail else "")
        ),
        started_at=started,
    )


def _process_event_stock(conn, adapter, stock: dict, start: str, end: str,
                         window_days: list[str], *, force: bool,
                         retries: int, checkpoints: dict[str, dict],
                         raw_stats: dict[str, dict], pacer: _Pacer) -> dict:
    """抓 raw（或复用断点）并重建单票；返回结果与待修复原因。"""
    stock_code = stock["stock_code"]
    first_event = stock["first"]
    needs_d1 = first_event == window_days[0]
    fetch_start = prev_trading_day(window_days[0]) if needs_d1 else window_days[0]
    checkpoint = checkpoints.get(stock_code)
    stats = raw_stats.get(stock_code) or {}
    raw_dates = (
        _raw_dates(conn, stock_code, window_days[0], window_days[-1])
        if checkpoint is not None else []
    )
    d1 = prev_trading_day(first_event)
    d1_present = (
        _has_row(conn, stock_code, d1)
        if d1 < window_days[0] else d1 in set(raw_dates)
    )
    # 上市日晚于 D-1 时没有 D-1 是事实边界（新股/次新），重抓也补不出；
    # 其余缺 D-1 或窗口内断档都算“仍无效”，必须重抓。
    d1_unavailable_by_listing = bool(raw_dates) and raw_dates[0] > d1
    gaps = (
        set(trading_days_between(raw_dates[0], raw_dates[-1])) - set(raw_dates)
        if len(raw_dates) >= 2 else set()
    )
    reusable = (
        not force
        and checkpoint is not None
        and checkpoint["status"] == "ok"
        and bool(raw_dates)
        and first_event in set(raw_dates)
        and raw_dates[-1] >= stock["last"]
        and (d1_present or d1_unavailable_by_listing)
        and not gaps
    )

    started = _utcnow()
    attempts = 0
    fetch_failed = False
    skipped = False
    rows: list[dict] = []
    if reusable:
        skipped = True
    else:
        fetched = _fetch_raw_stock(
            adapter, stock_code, fetch_start, end, retries, pacer
        )
        attempts = fetched["attempts"]
        if fetched["rows"] is None:
            fetch_failed = True
            error = fetched["error"]
            kind = fetch_failure_kind(error) if error is not None else "fetch_failed"
            if kind == "connection":
                kind = "upstream_unreachable"
            elif kind != "unsupported":
                kind = "fetch_failed"
            _record_raw_ledger(
                conn, stock_code, start, end, status="error", rows=0,
                attempts=attempts, source=adapter.name, error_kind=kind,
                detail=str(error), started=started,
            )
            if stats.get("rows", 0) == 0:
                reasons = {"raw_unavailable": [str(error)]}
                repairs = _record_repairs(conn, stock_code, first_event, reasons)
                return {
                    "stock_code": stock_code, "status": "error", "rows": 0,
                    "attempts": attempts, "error_kind": kind,
                    "repairs": repairs,
                }
            rows = []
        else:
            rows = fetched["rows"]
            in_window = [
                row for row in _window_rows(rows, window_days[0], window_days[-1])
                if _has_valid_close(row)
            ]
            if not in_window and stats.get("rows", 0) == 0:
                _record_raw_ledger(
                    conn, stock_code, start, end, status="empty", rows=0,
                    attempts=attempts, source=adapter.name,
                    error_kind="no_data_for_date", detail="上游返回空",
                    started=started,
                )
                reasons = {"raw_unavailable": ["上游返回空"]}
                repairs = _record_repairs(conn, stock_code, first_event, reasons)
                return {
                    "stock_code": stock_code, "status": "empty", "rows": 0,
                    "attempts": attempts, "error_kind": "no_data_for_date",
                    "repairs": repairs,
                }

    try:
        snapshot = rebuild_stock(
            conn, stock_code, rows, [],
            window_start=window_days[0], window_end=window_days[-1],
        )
    except Exception as e:  # noqa: BLE001 - 单票失败不终止回填
        log.warning("adj-backfill 重建失败 %s: %s", stock_code, e)
        _record_raw_ledger(
            conn, stock_code, start, end, status="error", rows=len(rows),
            attempts=attempts, source=adapter.name,
            error_kind="rebuild_failed", detail=str(e), started=started,
        )
        return {
            "stock_code": stock_code, "status": "error", "rows": len(rows),
            "attempts": attempts, "error_kind": "rebuild_failed",
            "repairs": [],
        }

    if not skipped and not fetch_failed:
        window_count = len(_window_rows(rows, window_days[0], window_days[-1]))
        _record_raw_ledger(
            conn, stock_code, start, end,
            status="ok" if window_count else "empty", rows=window_count,
            attempts=attempts, source=adapter.name, started=started,
            error_kind=None if window_count else "no_data_for_date",
        )

    gap_dates = _raw_gap_dates(
        snapshot.get("raw_rows") or [], window_days[0], window_days[-1]
    )
    reasons = _reasons_for_snapshot(snapshot, gap_dates, refresh_failed=fetch_failed)
    if reasons or fetch_failed:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE stock_factor_state SET rebuild_pending=true"
                " WHERE stock_code=%s",
                (stock_code,),
            )
        conn.commit()
    repairs = _record_repairs(conn, stock_code, first_event, reasons) if reasons else []
    state = snapshot.get("state") or {}
    return {
        "stock_code": stock_code,
        "status": "skipped" if skipped else ("error" if fetch_failed else "ok"),
        "rows": len([
            row for row in (snapshot.get("raw_rows") or [])
            if window_days[0] <= row["trade_date"] <= window_days[-1]
        ]),
        "attempts": attempts,
        "skipped": skipped,
        "fetched": not skipped,
        "pending": bool(state.get("rebuild_pending")),
        "repairs": repairs,
    }


def _rebuild_retracted(conn, stock_code: str, start: str, end: str) -> dict:
    """事件被撤销的票：复用库内 raw 重建为无事件基线（不重抓）。"""
    try:
        snapshot = rebuild_stock(
            conn, stock_code, [], [], window_start=start, window_end=end,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("adj-backfill 撤销重建失败 %s: %s", stock_code, e)
        return {"stock_code": stock_code, "status": "error", "error": str(e),
                "repairs": []}
    gap_dates = _raw_gap_dates(snapshot.get("raw_rows") or [], start, end)
    reasons: dict[str, list[str]] = {}
    if gap_dates:
        reasons["raw_gap"] = [",".join(gap_dates[:20])]
    if snapshot.get("quarantined"):
        reasons["invalid_close"] = [
            ",".join(row["trade_date"] for row in snapshot["quarantined"][:20])
        ]
    repairs = []
    if reasons:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE stock_factor_state SET rebuild_pending=true"
                " WHERE stock_code=%s",
                (stock_code,),
            )
        conn.commit()
        repairs = _record_repairs(conn, stock_code, end, reasons)
    state = snapshot.get("state")
    if state is not None:
        status = "ok"
    elif snapshot.get("quarantined"):
        status = "error"
    else:
        status = "empty"
    return {
        "stock_code": stock_code,
        "status": status,
        "state": state,
        "repairs": repairs,
    }


# ---------------------------------------------------------------- 导出


def _write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_artifacts(report_dir: str, *, days: list[dict], events: list[dict],
                     event_stocks: list[dict], raw_results: list[dict],
                     repairs: list[dict], summary: dict) -> dict:
    os.makedirs(report_dir, exist_ok=True)
    paths = {
        "factor_days": os.path.join(report_dir, "factor_days.csv"),
        "events": os.path.join(report_dir, "events.csv"),
        "event_stocks": os.path.join(report_dir, "event_stocks.csv"),
        "raw_results": os.path.join(report_dir, "raw_results.csv"),
        "repairs": os.path.join(report_dir, "repairs.csv"),
        "summary": os.path.join(report_dir, "summary.json"),
    }
    _write_csv(
        paths["factor_days"],
        ["trade_date", "status", "rows", "attempts", "error_kind", "detail"],
        days,
    )
    _write_csv(
        paths["events"],
        ["stock_code", "ex_date", "factor_cum", "source"],
        events,
    )
    _write_csv(
        paths["event_stocks"],
        ["stock_code", "events", "first", "last"],
        event_stocks,
    )
    _write_csv(
        paths["raw_results"],
        ["stock_code", "status", "rows", "attempts", "skipped", "pending",
         "error_kind", "repairs"],
        raw_results,
    )
    _write_csv(
        paths["repairs"],
        ["stock_code", "trade_date", "reason", "label", "detail"],
        repairs,
    )
    with open(paths["summary"], "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)
    return paths


# ---------------------------------------------------------------- 主入口


def _default_start() -> str:
    return os.environ.get("QSTOCK_ADJ_WINDOW_START", ADJ_BACKFILL_DEFAULT_START).replace(
        "-", ""
    )


def _default_end(now) -> str:
    return trading_day_window(1, now=now)[1]


def _load_export_rows(conn, start: str, end: str) -> tuple[list[dict], list[dict]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, ex_date, factor_cum, source"
            " FROM stock_adjust_factor WHERE ex_date BETWEEN %s AND %s"
            " ORDER BY ex_date, stock_code",
            (start, end),
        )
        events = [
            {"stock_code": row[0], "ex_date": row[1], "factor_cum": float(row[2]),
             "source": row[3]}
            for row in cur.fetchall()
        ]
    stocks = [
        {"stock_code": row["stock_code"], "events": row["events"],
         "first": row["first"], "last": row["last"]}
        for row in _event_stock_rows(conn, start, end)
    ]
    return events, stocks


def run_adjustment_backfill(
    start: str | None = None,
    end: str | None = None,
    *,
    adapter=None,
    limit: int | None = None,
    force: bool = False,
    report_dir: str | None = None,
    throttle: float | None = None,
    now=None,
) -> dict:
    """回填 A：逐日事件集 + 有事件票 raw/派生重建，可断点续跑。

    `start` 缺省 `QSTOCK_ADJ_WINDOW_START`（20230101），`end` 缺省最近已收盘
    交易日；`limit` 只用于小样本冒烟（显式标记 limited，不宣称完整）。
    """
    tool = "adj-backfill"
    params: dict = {
        "start": start, "end": end, "limit": limit, "force": force,
        "report_dir": report_dir,
    }
    try:
        end_d = normalize_date(end) if end else _default_end(now)
        start_d = normalize_date(start) if start else normalize_date(
            _default_start()
        )
    except ValueError as e:
        return _error(tool, params, str(e))
    if start_d > end_d:
        return _error(tool, params, f"start({start_d}) 晚于 end({end_d})")
    if limit is not None and limit <= 0:
        return _error(tool, params, "limit 必须为正整数")
    if end_d == today_str(now) and is_trading_time(now):
        return _error(
            tool, params,
            f"交易时段内不回填当日（{end_d}）：请在收盘后执行或将 end 提前",
        )
    window_days = trading_days_between(start_d, end_d)
    if not window_days:
        return _error(tool, params, f"窗口内无交易日：{start_d}~{end_d}")

    if adapter is None:
        from .adapters.baostock_adapter import BaostockAdapter

        adapter = BaostockAdapter()
    pause = ADJ_BACKFILL_THROTTLE_S if throttle is None else throttle
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    began = monotonic()
    try:
        try:
            ensure_schema(conn)
        except Exception as e:  # noqa: BLE001 - 建表失败收敛为结构化错误
            return _error(tool, params, f"建表失败: {e}")

        # ---- 阶段 1：逐日全市场事件集
        checkpoints = _day_checkpoints(conn, start_d, end_d)
        counts = _event_counts(conn, start_d, end_d)
        day_results: list[dict] = []
        failed_days: list[dict] = []
        retracted_codes = _load_retracted_codes(conn, start_d, end_d)
        pacer = _Pacer(pause)
        factor_attempts = 0
        last_logged = monotonic()
        for index, day in enumerate(window_days, 1):
            checkpoint = checkpoints.get(day)
            reusable = (
                not force
                and checkpoint is not None
                and checkpoint["status"] in ("ok", "empty")
                and counts.get(day, 0) == checkpoint.get("rows", 0)
            )
            if reusable:
                day_results.append({
                    "trade_date": day, "status": "skipped",
                    "rows": counts.get(day, 0), "attempts": 0,
                    "error_kind": None, "detail": "台账已成功且库内事件数一致",
                })
            else:
                result = _ingest_event_day(
                    conn, adapter, day, start_d, end_d,
                    ADJ_BACKFILL_RETRIES, pacer,
                )
                factor_attempts += result.get("attempts", 0)
                retracted_codes.update(result.pop("retracted_codes", []))
                day_results.append(result)
                if result["status"] == "error":
                    failed_days.append(result)
            now_m = monotonic()
            if (
                index % PROGRESS_LOG_EVERY == 0
                or index == len(window_days)
                or now_m - last_logged >= PROGRESS_LOG_INTERVAL_S
            ):
                last_logged = now_m
                log.info(
                    "adj-backfill 事件集 %d/%d：ok=%d empty=%d error=%d",
                    index, len(window_days),
                    sum(1 for r in day_results if r["status"] == "ok"),
                    sum(1 for r in day_results if r["status"] == "empty"),
                    len(failed_days),
                )

        event_set_complete = not failed_days
        events_export, event_stocks = _load_export_rows(conn, start_d, end_d)
        event_codes = {stock["stock_code"] for stock in event_stocks}
        retracted_with_raw = sorted(
            code for code in retracted_codes if code not in event_codes
            and _raw_dates(conn, code, start_d, end_d)
        )

        # ---- 阶段 2：只对有事件票重抓 raw + 重建
        raw_checkpoints = _raw_checkpoints(conn, start_d, end_d)
        raw_stats = _raw_window_stats(conn, start_d, end_d)
        selected = event_stocks[:limit] if limit is not None else event_stocks
        raw_results: list[dict] = []
        repairs: list[dict] = []
        failed_stocks: list[dict] = []
        staging_d1 = 0
        raw_attempts = 0
        last_logged = monotonic()
        total_stocks = len(selected)
        for index, stock in enumerate(selected, 1):
            result = _process_event_stock(
                conn, adapter, stock, start_d, end_d, window_days,
                force=force, retries=ADJ_BACKFILL_RETRIES,
                checkpoints=raw_checkpoints, raw_stats=raw_stats, pacer=pacer,
            )
            raw_results.append(result)
            raw_attempts += result.get("attempts", 0)
            repairs.extend(result.get("repairs", []))
            if result["status"] in ("error", "empty"):
                failed_stocks.append(result)
            if (
                stock["first"] == window_days[0]
                and _has_row(conn, stock["stock_code"], prev_trading_day(window_days[0]))
            ):
                staging_d1 += 1
            now_m = monotonic()
            if (
                index % PROGRESS_LOG_EVERY == 0
                or index == total_stocks
                or now_m - last_logged >= PROGRESS_LOG_INTERVAL_S
            ):
                last_logged = now_m
                log.info(
                    "adj-backfill raw %d/%d：ok=%d skipped=%d error=%d pending=%d",
                    index, total_stocks,
                    sum(1 for r in raw_results if r["status"] == "ok"),
                    sum(1 for r in raw_results if r["status"] == "skipped"),
                    len(failed_stocks),
                    sum(1 for r in raw_results if r.get("pending")),
                )

        retracted_results = []
        for code in retracted_with_raw:
            result = _rebuild_retracted(conn, code, window_days[0], window_days[-1])
            retracted_results.append(result)
            repairs.extend(result.get("repairs", []))
        retracted_failures = [
            result for result in retracted_results if result["status"] == "error"
        ]

        limited = limit is not None and len(event_stocks) > len(selected)
        if failed_days and not event_codes:
            status = "error"
        elif failed_days or failed_stocks or repairs or limited or retracted_failures:
            status = "partial_error"
        else:
            status = "ok"
        complete = status == "ok" and event_set_complete and not limited
        stage = "verified" if complete else ("rebuilt" if selected or retracted_results else "ingested")

        repair_counts: dict[str, int] = {}
        for repair in repairs:
            repair_counts[repair["reason"]] = repair_counts.get(repair["reason"], 0) + 1
        raw_rows = sum(result.get("rows", 0) for result in raw_results)
        summary = {
            "status": status,
            "stage": stage,
            "window": {"start": start_d, "end": end_d},
            "source": adapter.name,
            "trading_days": len(window_days),
            "days": {
                "ok": sum(1 for r in day_results if r["status"] == "ok"),
                "empty": sum(1 for r in day_results if r["status"] == "empty"),
                "error": len(failed_days),
                "skipped": sum(1 for r in day_results if r["status"] == "skipped"),
            },
            "event_set_complete": event_set_complete,
            "failed_days": failed_days,
            "events": len(events_export),
            "event_stocks": len(event_stocks),
            "event_stock_processing": {
                "selected": len(selected),
                "ok": sum(1 for r in raw_results if r["status"] == "ok"),
                "skipped": sum(1 for r in raw_results if r["status"] == "skipped"),
                "error": len(failed_stocks),
                "pending": sum(1 for r in raw_results if r.get("pending")),
                "limited": limited,
            },
            "raw": {
                "rows": raw_rows,
                "staging_d1_stocks": staging_d1,
                "failed_stocks": len(failed_stocks),
            },
            "upstream_requests": {
                "factor_attempts": factor_attempts,
                "raw_attempts": raw_attempts,
                "total": factor_attempts + raw_attempts,
            },
            "repairs": len(repairs),
            "repair_counts": repair_counts,
            "retracted_rebuilt": len(retracted_results),
            "retracted_failed": len(retracted_failures),
            "duration_s": round(monotonic() - began, 3),
        }
        out_report = report_dir or os.path.join(
            "files", f"adj-backfill_{start_d}_{end_d}"
        )
        artifacts = _write_artifacts(
            out_report, days=day_results, events=events_export,
            event_stocks=event_stocks, raw_results=raw_results,
            repairs=repairs, summary=summary,
        )
        messages = ["数据处理完成"] + (["数据可以使用"] if complete else [])
        return {
            "status": status,
            "tool": tool,
            "params": params,
            "window": {"start": start_d, "end": end_d},
            "source": adapter.name,
            "stage": stage,
            "messages": messages,
            "summary": summary,
            "repairs": repairs[:200],
            "repairs_truncated": len(repairs) > 200,
            "artifacts": artifacts,
        }
    finally:
        conn.close()
