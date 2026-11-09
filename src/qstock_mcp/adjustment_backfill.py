"""复权回填（issue #163/#164）：逐日全市场事件集 + raw + 本地重建与迁移审计。

范围与边界（spec #158 §6.1/§6.2）：
- 窗口内每个历史交易日调用一次 baostock `query_daily_adjust_factor(date)`；
  成功空结果记 empty，失败重试后仍失败记 error，绝不当作“当日无事件”。
- 事件集写入 `stock_adjust_factor`（B 只取 backAdjustFactor）；成功重查整日
  替换该日事件集以支持修订/撤销，受影响票重建，非事件票不写占位因子。
- D-1 与窗口内每个交易日调用一次 `query_daily_history_k_AStock(date)`，整日
  替换全市场 `adjustflag=3` raw；amount 元→万元由适配层完成，失败重试并落台账。
- 本地按股票读取库内 raw：事件票以 `B0=B1×f1` 反推基线，无事件票 B0=1；
  D-1 不写入 hfq。B0/raw/pctChg 不可验证时置 pending 并落待修复台账。
- 断点续跑读 `collection_log`：因子日/raw 日校验台账与库内行数一致才跳过；
  `--force` 忽略台账整窗重抓。全程串行，不接替代源。

数据源纪律：固定单会话串行、相邻请求 ≥1.5s、单请求 60s 外部硬超时。
2026-11-08 全市场 raw 只读样本（20230103–05、20261103）单日 4906/5224 行、
约 6–10 秒，全部 adj=none 且含停牌行；长期容量与全历史完备率仍待全窗口验证。
"""

import csv
import json
import logging
import math
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from time import monotonic

from psycopg2.extras import execute_values  # type: ignore[import-untyped]

from .adapters.base import FetchError, fetch_failure_kind
from .adjustment import rebuild_stock
from .adjustment_audit import audit_adjustment_migration
from .baostock_errors import BaostockError
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
from .tools_baostock import baostock_error

log = logging.getLogger(__name__)

ADJ_BACKFILL_DEFAULT_START = "20230101"
# 2026-11-04/05 实测：baostock 串行、相邻请求 ≥1.5s 可承载；并发/长期 TPS
# 未验证，回填固定串行并使用该间隔（不据此推断全历史容量）。
ADJ_BACKFILL_THROTTLE_S = 1.5
ADJ_BACKFILL_RETRIES = 3
ADJ_BACKFILL_BACKOFF_S = (0.5, 1.0)
PROGRESS_LOG_EVERY = 20
PROGRESS_LOG_INTERVAL_S = 60
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
    "raw_unavailable": "库内无有效 raw（通常由全市场 raw 日失败导致）",
    "raw_refresh_failed": "历史逐票 raw 重抓失败，沿用库内已有 raw 重建",
    "rebuild_failed": "本地复权重建失败",
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


def _decimal(value) -> Decimal:
    return Decimal(str(value))


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


def _failure_kind(error) -> str:
    kind = fetch_failure_kind(error) if error is not None else "fetch_failed"
    if kind == "connection":
        return "upstream_unreachable"
    if kind == "unsupported":
        return "unsupported"
    return "fetch_failed"


def _repair_detail(stock_code: str, reason: str, detail: str) -> str:
    return (
        f"adj-backfill repair {stock_code} {reason}: {detail}"
    )[:500]


# ---------------------------------------------------------------- 台账断点


def _day_checkpoints(conn, start: str, end: str) -> dict[str, dict]:
    """本回填同窗口最近一次因子采集台账。

    只认 `adj-backfill event <start>~<end>` 前缀：morning 的 pctChg 降级行也
    可能记 ok，但回填仍须亲自调用一次权威逐日因子接口，不能把降级结果当作
    已采过。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (trade_date) trade_date, status, rows, error_kind
            FROM collection_log
            WHERE section = %s AND trade_date BETWEEN %s AND %s
              AND detail LIKE %s
            ORDER BY trade_date, id DESC
            """,
            (EVENT_SECTION, start, end, _event_detail_prefix(start, end) + "%"),
        )
        return {
            row[0]: {"status": row[1], "rows": row[2] or 0, "error_kind": row[3]}
            for row in cur.fetchall()
        }


def _raw_day_checkpoints(conn, start: str, end: str) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (trade_date) trade_date, status, rows
            FROM collection_log
            WHERE section=%s AND detail LIKE %s
            ORDER BY trade_date, id DESC
            """,
            (RAW_SECTION, f"adj-backfill raw-day {start}~{end}%"),
        )
        return {
            row[0]: {"status": row[1], "rows": row[2] or 0}
            for row in cur.fetchall()
        }


def _raw_day_counts(conn, start: str, end: str) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, count(*) FROM stock_daily_raw"
            " WHERE trade_date BETWEEN %s AND %s GROUP BY trade_date",
            (start, end),
        )
        return dict(cur.fetchall())


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


def _legacy_none_audit(conn, start: str, end: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), count(DISTINCT l.stock_code), min(l.trade_date),
                   max(l.trade_date),
                   count(*) FILTER (WHERE r.stock_code IS NOT NULL),
                   count(*) FILTER (WHERE l.close IS NULL OR l.close <= 0),
                   count(*) FILTER (
                       WHERE r.stock_code IS NULL AND l.trade_date BETWEEN %s AND %s
                   )
            FROM stock_daily l
            LEFT JOIN stock_daily_raw r
              ON r.stock_code=l.stock_code AND r.trade_date=l.trade_date
            WHERE l.adj='none'
            """,
            (start, end),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT coalesce(source, 'unknown'), count(*) FROM stock_daily"
            " WHERE adj='none' GROUP BY coalesce(source, 'unknown') ORDER BY 1"
        )
        by_source = dict(cur.fetchall())
    return {
        "rows": row[0],
        "stocks": row[1],
        "first_trade_date": row[2],
        "last_trade_date": row[3],
        "covered_by_raw": row[4],
        "invalid_close": row[5],
        "raw_recheck_required": row[6],
        "isolated_rows": row[0],
        "merged_into_raw": 0,
        "by_source": by_source,
    }


def _load_quarantined(conn, start: str, end: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, trade_date FROM stock_daily_raw"
            " WHERE trade_date BETWEEN %s AND %s AND (close IS NULL OR close <= 0)"
            " ORDER BY stock_code, trade_date",
            (start, end),
        )
        return [
            {"stock_code": row[0], "trade_date": row[1], "reason": "invalid_close"}
            for row in cur.fetchall()
        ]


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


def _replace_day_events(
    conn, trade_date: str, events: list[dict]
) -> tuple[list[str], int, int]:
    """事务内整日替换事件集，过滤同 B 非阶跃行并标记受影响 state。

    返回 (被撤销代码, 同 B 跳过数, 实际写入事件数)。同一事务内把受影响
    state 置 rebuild_pending=true，避免崩溃后读者把新事件分母配旧 hfq；
    撤销代码另落 durable 标记供续跑重建。
    """
    by_code = {event["stock_code"]: event for event in events}
    with conn.cursor() as cur:
        previous_b: dict[str, Decimal] = {}
        if by_code:
            cur.execute(
                """
                SELECT DISTINCT ON (stock_code) stock_code, factor_cum
                FROM stock_adjust_factor
                WHERE stock_code = ANY(%s) AND ex_date < %s
                ORDER BY stock_code, ex_date DESC
                """,
                (sorted(by_code), trade_date),
            )
            previous_b = {row[0]: _decimal(row[1]) for row in cur.fetchall()}
        kept: dict[str, dict] = {}
        equal_b_skipped = 0
        for code, event in by_code.items():
            previous = previous_b.get(code)
            if previous is not None and previous == _decimal(event["factor_cum"]):
                equal_b_skipped += 1
                continue
            kept[code] = event
        values = [
            [code, trade_date, event["factor_cum"], event.get("kind"),
             event.get("detail"), event.get("reg_date"),
             event.get("source", "baostock-adjust-factor")]
            for code, event in sorted(kept.items())
        ]
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
        retracted = sorted(old_codes - set(kept))
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
        affected = sorted(old_codes | set(kept))
        if affected:
            cur.execute(
                "UPDATE stock_factor_state SET rebuild_pending=true"
                " WHERE stock_code = ANY(%s)",
                (affected,),
            )
        conn.commit()
    return retracted, equal_b_skipped, len(kept)


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
        kind = _failure_kind(error)
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

    retracted, equal_b_skipped, kept_count = _replace_day_events(
        conn, trade_date, list(by_code.values())
    )
    status = "ok" if (kept_count or equal_b_skipped) else "empty"
    record_collection(
        conn, trade_date=trade_date, section=EVENT_SECTION, status=status,
        source=adapter.name, rows=kept_count,
        detail=(
            f"{_event_detail_prefix(start, end)} attempts={result['attempts']}"
            f"; raw_rows={len(by_code)}; stored={kept_count}"
            f"; equal_b_skipped={equal_b_skipped}"
        ),
        started_at=started,
    )
    return {
        "trade_date": trade_date, "status": status, "rows": kept_count,
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


def _missing_trading_days(dates: list[str]) -> list[str]:
    if len(dates) < 2:
        return []
    expected = trading_days_between(dates[0], dates[-1])
    actual = set(dates)
    return [day for day in expected if day not in actual]


def _raw_gap_dates(raw_rows: list[dict], start: str, end: str,
                   known_missing: list[str] | None = None) -> list[str]:
    dates = sorted(
        row["trade_date"] for row in raw_rows
        if start <= row["trade_date"] <= end
    )
    # 首尾之外的股票生命周期边界不凭空推断；本轮明确失败的 raw 日则始终
    # 是未知缺口，即使它位于窗口首尾，也不能让本地重建误判 complete。
    return sorted(set(_missing_trading_days(dates)) | set(known_missing or []))


def _reasons_for_snapshot(snapshot: dict, gap_details: list[str],
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
    if gap_details:
        shown = ",".join(gap_details[:20])
        suffix = "..." if len(gap_details) > 20 else ""
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


def _ingest_raw_day(conn, adapter, trade_date: str, start: str, end: str,
                    retries: int, pacer: _Pacer) -> dict:
    started = _utcnow()
    rows, attempts, error = _fetch_with_retries(
        lambda: adapter.fetch_astock_daily(trade_date), retries, pacer
    )
    if rows is None:
        kind = _failure_kind(error)
        record_collection(
            conn, trade_date=trade_date, section=RAW_SECTION, status="error",
            error_kind=kind,
            detail=f"adj-backfill raw-day {start}~{end} attempts={attempts}: {error}",
            started_at=started,
        )
        return {
            "trade_date": trade_date, "status": "error", "rows": 0,
            "attempts": attempts, "error_kind": kind, "detail": str(error),
        }

    by_code = {row.get("stock_code"): row for row in rows}
    invalid = [
        row for row in rows
        if not row.get("stock_code")
        or str(row.get("trade_date")) != trade_date
        or row.get("adj") not in (None, "none")
    ]
    if len(by_code) != len(rows) or invalid:
        record_collection(
            conn, trade_date=trade_date, section=RAW_SECTION, status="error",
            error_kind="fetch_failed",
            detail=(
                f"adj-backfill raw-day {start}~{end} attempts={attempts}; "
                f"rows={len(rows)}; unique_codes={len(by_code)}; invalid={len(invalid)}"
            ),
            started_at=started,
        )
        return {
            "trade_date": trade_date, "status": "error", "rows": 0,
            "attempts": attempts, "error_kind": "fetch_failed",
            "detail": "全市场 raw 日响应存在重复代码、错日期或非 raw 口径",
        }

    values = [
        [
            row["stock_code"], trade_date, row.get("open"), row.get("high"),
            row.get("low"), row.get("close"), row.get("preclose"),
            row.get("volume"), row.get("amount"), row.get("amplitude"),
            row.get("change_percent"), row.get("change_amount"),
            row.get("turnover_rate"), row.get("tradestatus"),
            row.get("source", adapter.name),
        ]
        for row in sorted(by_code.values(), key=lambda item: item["stock_code"])
    ]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM stock_daily_raw WHERE trade_date=%s", (trade_date,))
        if values:
            execute_values(
                cur,
                """
                INSERT INTO stock_daily_raw (stock_code, trade_date,
                    open, high, low, close, preclose, volume, amount, amplitude,
                    change_percent, change_amount, turnover_rate, tradestatus, source)
                VALUES %s
                """,
                values,
            )
        conn.commit()
    status = "ok" if values else "empty"
    record_collection(
        conn, trade_date=trade_date, section=RAW_SECTION, status=status,
        source=adapter.name, rows=len(values),
        error_kind=None if values else "no_data_for_date",
        detail=f"adj-backfill raw-day {start}~{end} attempts={attempts}",
        started_at=started,
    )
    return {
        "trade_date": trade_date, "status": status, "rows": len(values),
        "attempts": attempts, "error_kind": None if values else "no_data_for_date",
        "detail": None,
    }


def _rebuild_event_stock(conn, stock: dict, window_days: list[str],
                         known_missing: list[str]) -> dict:
    """复用逐日落库的全市场 raw 重建事件票，不再发起逐票请求。"""
    stock_code = stock["stock_code"]
    first_event = stock["first"]
    try:
        snapshot = rebuild_stock(
            conn, stock_code, [], [],
            window_start=window_days[0], window_end=window_days[-1],
        )
    except Exception as e:  # noqa: BLE001 - 单票失败不终止回填
        log.warning("adj-backfill 重建失败 %s: %s", stock_code, e)
        record_collection(
            conn, trade_date=stock["last"], section=REPAIR_SECTION, status="error",
            source="adj-backfill", error_kind="rebuild_failed",
            detail=f"adj-backfill rebuild {stock_code}: {e}",
        )
        return {
            "stock_code": stock_code, "status": "error", "rows": 0,
            "attempts": 0, "error_kind": "rebuild_failed", "repairs": [],
        }

    snapshot_raw = snapshot.get("raw_rows") or []
    raw_dates = sorted(
        row["trade_date"] for row in snapshot_raw
        if window_days[0] <= row["trade_date"] <= window_days[-1]
    )
    gap_details = _raw_gap_dates(
        snapshot_raw, window_days[0], window_days[-1], known_missing
    )
    if raw_dates and first_event not in set(raw_dates):
        gap_details.append(f"first_event={first_event}")
    if raw_dates and raw_dates[-1] < stock["last"]:
        gap_details.append(f"last_event={stock['last']}")
    reasons = _reasons_for_snapshot(snapshot, gap_details, refresh_failed=False)
    if reasons:
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
        "status": "ok" if state else "error",
        "rows": len(raw_dates),
        "attempts": 0,
        "skipped": False,
        "pending": bool(state.get("rebuild_pending") or reasons),
        "repairs": repairs,
    }


def _rebuild_without_events(conn, stock_code: str, start: str, end: str,
                            known_missing: list[str]) -> dict:
    """复用库内 raw 重建无事件票或事件已撤销票，不再发起上游请求。"""
    try:
        snapshot = rebuild_stock(
            conn, stock_code, [], [], window_start=start, window_end=end,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("adj-backfill 无事件重建失败 %s: %s", stock_code, e)
        record_collection(
            conn, trade_date=end, section=REPAIR_SECTION, status="error",
            source="adj-backfill", error_kind="rebuild_failed",
            detail=f"adj-backfill rebuild {stock_code}: {e}",
        )
        return {
            "stock_code": stock_code, "status": "error", "error": str(e),
            "error_kind": "rebuild_failed", "repairs": [],
        }
    gap_dates = _raw_gap_dates(
        snapshot.get("raw_rows") or [], start, end, known_missing
    )
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


def _write_artifacts(report_dir: str, *, days: list[dict], raw_days: list[dict],
                     events: list[dict], event_stocks: list[dict],
                     raw_results: list[dict], repairs: list[dict],
                     quarantined: list[dict], classification: list[dict],
                     audit: dict, legacy_none: dict, summary: dict) -> dict:
    os.makedirs(report_dir, exist_ok=True)
    paths = {
        "factor_days": os.path.join(report_dir, "factor_days.csv"),
        "raw_days": os.path.join(report_dir, "raw_days.csv"),
        "events": os.path.join(report_dir, "events.csv"),
        "event_stocks": os.path.join(report_dir, "event_stocks.csv"),
        "raw_results": os.path.join(report_dir, "raw_results.csv"),
        "repairs": os.path.join(report_dir, "repairs.csv"),
        "quarantined": os.path.join(report_dir, "quarantined.csv"),
        "classification": os.path.join(report_dir, "stock_classification.csv"),
        "stock_hashes": os.path.join(report_dir, "stock_hashes.csv"),
        "raw_sample": os.path.join(report_dir, "raw_sample.csv"),
        "legacy_none": os.path.join(report_dir, "legacy_none_audit.json"),
        "summary": os.path.join(report_dir, "summary.json"),
    }
    for key, rows in (("factor_days", days), ("raw_days", raw_days)):
        _write_csv(
            paths[key],
            ["trade_date", "status", "rows", "attempts", "error_kind", "detail"],
            rows,
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
    _write_csv(
        paths["quarantined"],
        ["stock_code", "trade_date", "reason"],
        quarantined,
    )
    _write_csv(
        paths["classification"],
        ["stock_code", "classification", "reason"],
        classification,
    )
    _write_csv(
        paths["stock_hashes"],
        ["stock_code", "raw_rows", "raw_hash", "event_rows", "event_hash",
         "hfq_rows", "hfq_hash", "state_rows", "state_hash"],
        audit["stock_hashes"],
    )
    _write_csv(
        paths["raw_sample"],
        ["stock_code", "trade_date", "classification", "close", "amount", "source",
         "exchange", "price_year", "price_bucket"],
        audit["raw_sample"],
    )
    for key, value in (("legacy_none", legacy_none), ("summary", summary)):
        with open(paths[key], "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
    return paths


# ---------------------------------------------------------------- 主入口


def _default_start() -> str:
    return os.environ.get("QSTOCK_ADJ_WINDOW_START", ADJ_BACKFILL_DEFAULT_START).replace(
        "-", ""
    )


def _default_end(now) -> str:
    return trading_day_window(1, now=now)[1]


def _load_event_exports(conn, start: str, end: str) -> tuple[list[dict], list[dict]]:
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
    """逐日全市场事件/raw 回填与本地派生重建，可断点续跑。

    `start` 缺省 `QSTOCK_ADJ_WINDOW_START`（20230101），`end` 缺省最近已收盘
    交易日；`limit` 只限制事件票本地重建，不减少按日 raw 请求。
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

        # 窗口必须覆盖库内最新复权事实：回填会重建 state/hfq，历史 end 不能
        # 回退窗口外派生（CONTEXT「整窗刷新」：窗口外业务数据不动）。
        with conn.cursor() as cur:
            cur.execute("SELECT max(trade_date) FROM stock_daily_raw")
            latest_raw = cur.fetchone()[0]
            cur.execute("SELECT max(ex_date) FROM stock_adjust_factor")
            latest_event = cur.fetchone()[0]
        if (latest_raw and latest_raw > end_d) or (
            latest_event and latest_event > end_d
        ):
            return _error(
                tool, params,
                f"窗口 end({end_d}) 早于库内已有复权事实（raw={latest_raw},"
                f" event={latest_event}）；历史窗口请用隔离库，正式回填必须"
                "覆盖到最新已有数据，避免回退窗口外 hfq/state",
                error_kind="window_behind_data",
            )

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
        events_export, event_stocks = _load_event_exports(conn, start_d, end_d)
        event_codes = {stock["stock_code"] for stock in event_stocks}

        # ---- 阶段 2：逐日全市场 raw + 本地单票重建
        raw_day_results: list[dict] = []
        raw_attempts = 0
        raw_days = [prev_trading_day(window_days[0])] + window_days
        raw_checkpoints = _raw_day_checkpoints(conn, start_d, end_d)
        raw_day_counts = _raw_day_counts(conn, raw_days[0], raw_days[-1])
        for index, day in enumerate(raw_days, 1):
            checkpoint = raw_checkpoints.get(day)
            if (
                not force and checkpoint is not None
                and checkpoint["status"] == "ok"
                and raw_day_counts.get(day, 0) == checkpoint["rows"]
            ):
                result = {
                    "trade_date": day, "status": "skipped",
                    "rows": raw_day_counts[day], "attempts": 0,
                    "error_kind": None, "detail": "台账已成功且库内 raw 行数一致",
                }
            else:
                result = _ingest_raw_day(
                    conn, adapter, day, start_d, end_d,
                    ADJ_BACKFILL_RETRIES, pacer,
                )
            raw_day_results.append(result)
            raw_attempts += result["attempts"]
            log.info(
                "adj-backfill raw 日 %d/%d：%s rows=%d error=%s",
                index, len(raw_days), day, result["rows"], result["error_kind"],
            )

        retracted_with_raw = sorted(
            code for code in retracted_codes if code not in event_codes
            and _raw_dates(conn, code, start_d, end_d)
        )
        known_missing = [
            result["trade_date"] for result in raw_day_results
            if result["status"] in ("error", "empty")
            and window_days[0] <= result["trade_date"] <= window_days[-1]
        ]
        selected = event_stocks[:limit] if limit is not None else event_stocks
        raw_results: list[dict] = []
        repairs: list[dict] = []
        failed_stocks: list[dict] = []
        staging_d1 = 0
        last_logged = monotonic()
        total_stocks = len(selected)
        for index, stock in enumerate(selected, 1):
            result = _rebuild_event_stock(
                conn, stock, window_days, known_missing
            )
            raw_results.append(result)
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

        raw_stats = _raw_window_stats(conn, window_days[0], window_days[-1])
        no_event_results: list[dict] = []
        if event_set_complete and limit is None:
            no_event_codes = sorted(
                set(raw_stats) - event_codes - retracted_codes
            )
            for code in no_event_codes:
                result = _rebuild_without_events(
                    conn, code, window_days[0], window_days[-1], known_missing
                )
                result.update({
                    "rows": raw_stats[code]["rows"], "attempts": 0,
                    "pending": bool(
                        (result.get("state") or {}).get("rebuild_pending")
                        or result.get("repairs")
                    ),
                })
                no_event_results.append(result)
                repairs.extend(result.get("repairs", []))
                if result["status"] in ("error", "empty"):
                    failed_stocks.append(result)

        retracted_results = []
        for code in retracted_with_raw:
            result = _rebuild_without_events(
                conn, code, window_days[0], window_days[-1], known_missing
            )
            retracted_results.append(result)
            repairs.extend(result.get("repairs", []))
        retracted_failures = [
            result for result in retracted_results if result["status"] == "error"
        ]

        selected_event_codes = {stock["stock_code"] for stock in selected}
        classification_rows: list[dict] = []
        skip_reasons: dict[str, int] = {}
        for code in sorted(set(raw_stats) | event_codes | retracted_codes):
            if code in selected_event_codes:
                classification, reason = "event", ""
            elif code in event_codes:
                classification, reason = "skipped", "event_stock_limit"
            elif code in retracted_codes:
                classification, reason = "retracted", ""
            elif event_set_complete and limit is None:
                classification, reason = "no_event", ""
            else:
                classification = "unclassified"
                reason = (
                    "incomplete_event_set" if not event_set_complete
                    else "event_stock_limit"
                )
            if reason:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            classification_rows.append({
                "stock_code": code, "classification": classification, "reason": reason,
            })

        limited = limit is not None and len(event_stocks) > len(selected)
        failed_raw_days = [
            result for result in raw_day_results
            if result["status"] in ("error", "empty")
            and window_days[0] <= result["trade_date"] <= window_days[-1]
        ]
        audit = audit_adjustment_migration(conn, start_d, end_d, event_set_complete)
        audit_failed = any(
            audit["validation"][key] for key in (
                "pending_states", "hfq_without_valid_raw", "valid_raw_without_hfq",
                "state_without_raw", "classification_mismatches",
            )
        )
        if failed_days and not event_codes:
            status = "error"
        elif (
            failed_days or failed_raw_days or failed_stocks or repairs or limited
            or retracted_failures or audit_failed
        ):
            status = "partial_error"
        else:
            status = "ok"
        complete = status == "ok" and event_set_complete and not limited
        stage = "verified" if complete else (
            "rebuilt" if selected or no_event_results or retracted_results else "ingested"
        )

        repair_counts: dict[str, int] = {}
        for repair in repairs:
            repair_counts[repair["reason"]] = repair_counts.get(repair["reason"], 0) + 1
        raw_rows = sum(stock["rows"] for stock in raw_stats.values())
        quarantined = _load_quarantined(conn, start_d, end_d)
        legacy_none = _legacy_none_audit(conn, start_d, end_d)
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
            "raw_day_processing": {
                "selected": len(raw_day_results),
                "ok": sum(1 for r in raw_day_results if r["status"] == "ok"),
                "empty": sum(1 for r in raw_day_results if r["status"] == "empty"),
                "error": sum(1 for r in raw_day_results if r["status"] == "error"),
                "skipped": sum(1 for r in raw_day_results if r["status"] == "skipped"),
                "failed_days": failed_raw_days,
            },
            "events": len(events_export),
            "event_stocks": len(event_stocks),
            "event_stock_processing": {
                "selected": len(selected),
                "ok": sum(1 for r in raw_results if r["status"] == "ok"),
                "skipped": sum(1 for r in raw_results if r["status"] == "skipped"),
                "error": sum(
                    1 for r in raw_results if r["status"] in ("error", "empty")
                ),
                "pending": sum(1 for r in raw_results if r.get("pending")),
                "limited": limited,
            },
            "no_event_stock_processing": {
                "selected": len(no_event_results),
                "ok": sum(1 for r in no_event_results if r["status"] == "ok"),
                "error": sum(
                    1 for r in no_event_results if r["status"] in ("error", "empty")
                ),
                "pending": sum(1 for r in no_event_results if r.get("pending")),
            },
            "classification": {
                "event_set_complete": event_set_complete,
                "factor_days_expected": len(window_days),
                "factor_days_succeeded": len(window_days) - len(failed_days),
                "factor_days_failed": len(failed_days),
                "event_stocks": sum(
                    1 for row in classification_rows if row["classification"] == "event"
                ),
                "no_event_stocks": len(no_event_results),
                "retracted_stocks": len(retracted_results),
                "unclassified_stocks": sum(
                    1 for row in classification_rows
                    if row["classification"] == "unclassified"
                ),
                "skip_reasons": skip_reasons,
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
            "isolation": {
                "rows": len(quarantined),
                "stocks": len({row["stock_code"] for row in quarantined}),
                "reasons": {"invalid_close": len(quarantined)} if quarantined else {},
            },
            "validation": audit["validation"],
            "ordinary_raw_sample": audit["sample_summary"],
            "skip_reasons": {
                "factor_day_checkpoint": sum(
                    1 for row in day_results if row["status"] == "skipped"
                ),
                "raw_day_checkpoint": sum(
                    1 for row in raw_day_results if row["status"] == "skipped"
                ),
            },
            "legacy_none": legacy_none,
            "duration_s": round(monotonic() - began, 3),
        }
        out_report = report_dir or os.path.join(
            "files", f"adj-backfill_{start_d}_{end_d}"
        )
        artifacts = _write_artifacts(
            out_report, days=day_results, raw_days=raw_day_results,
            events=events_export, event_stocks=event_stocks,
            raw_results=raw_results + no_event_results + retracted_results,
            repairs=repairs, quarantined=quarantined,
            classification=classification_rows, audit=audit,
            legacy_none=legacy_none, summary=summary,
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
    except BaostockError as exc:
        return baostock_error(tool, params, exc)
    finally:
        conn.close()
