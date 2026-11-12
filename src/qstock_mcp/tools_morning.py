"""次晨批核心层：开盘前采集前一交易日数据并重建复权派生。

各部分独立成败、独立落采集台账：
1. stock_daily：baostock `query_daily_history_k_AStock` 一把抓全市场 raw，
   amount 元→万元，单源不走全市场 fallback；legacy 模式保留同键写入，
   `QSTOCK_PRICE_SOURCE=new` 时停止 legacy 双写，raw/因子采集继续。
2. stock_adjust_factor：逐日全市场累计 B 事件与 raw 经 `rebuild_stock` 写入
   影子表。成功空结果整日替换事件；失败时用权威 raw+pctChg 降级，双路径
   按 0.1% 对账。无事件票只写当日 hfq，事件票从 ex_date 重建并推进 anchor。
   raw 缺口、无效 close、基线未知和未决异常均阻止 verified。
3. index_daily：7 大指数白名单尾行缺失即补，尾行已在目标日的指数零请求。
4. sw_board_valuation：申万板块每日分析报表按业务键幂等落库。

盘中禁写防线（issue #78）延伸：显式指定目标日为当日且正在交易时段时报错
零落库。调度入口见 scheduler.py morning 子命令；本层不注册 MCP 工具。
"""

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from .adapters import (
    AStockDailyAdapter,
    IndexDailyAdapter,
    SwBoardsAdapter,
    default_index_daily_adapters,
)
from .adapters.base import FetchError, fetch_failure_kind
from .baostock_errors import BaostockError
from .adjustment import event_factor_from_pctchg, factor_relative_error
from .dates import add_days, is_trading_time, normalize_date, prev_trading_day, today_str
from .db import connect
from .fetch_chain import AllSourcesFailed, aggregate_error_kind, fetch_index_daily_with_fallback
from .ledger import record_collection
from .output import aggregate_batch_status
from .output import error as _error
from .repository import (
    select_index_daily,
    upsert_daily_rows,
    upsert_index_daily,
    upsert_sw_board_valuation,
)
from .tools_baostock import baostock_error
from .tools_init import MAJOR_INDICES

log = logging.getLogger(__name__)


def _stock_daily_part(conn, adapter, target: str) -> tuple[dict, list[dict]]:
    started = datetime.now(timezone.utc)
    try:
        rows = adapter.fetch_astock_daily(target)
    except FetchError as e:
        kind = fetch_failure_kind(e)
        if kind == "connection":
            kind = "upstream_unreachable"
        elif kind != "unsupported":
            kind = "fetch_failed"
        record_collection(
            conn, trade_date=target, section="stock_daily", status="error",
            error_kind=kind, detail=f"morning_batch: {e}", started_at=started,
        )
        return {"status": "error", "error": str(e), "error_kind": kind}, []
    new_mode = os.environ.get("QSTOCK_PRICE_SOURCE", "legacy") == "new"
    legacy_rows = 0 if new_mode else upsert_daily_rows(conn, rows, adapter.name)
    n = len(rows)
    record_collection(
        conn, trade_date=target, section="stock_daily",
        status="ok" if n > 0 else "empty", source=adapter.name, rows=n,
        error_kind=None if n > 0 else "no_data_for_date",
        detail=f"morning_batch;legacy_write={'disabled' if new_mode else 'enabled'}",
        started_at=started,
    )
    return {
        "status": "ok" if n > 0 else "empty",
        "rows": n,
        "legacy_rows": legacy_rows,
        "source": adapter.name,
        **({"empty_kind": "no_data_for_date"} if n == 0 else {}),
    }, rows


def _previous_raw(conn, stock_code: str, target: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, close FROM stock_daily_raw"
            " WHERE stock_code=%s AND trade_date<%s"
            " ORDER BY trade_date DESC LIMIT 1",
            (stock_code, target),
        )
        return cur.fetchone()


def _previous_b(
    conn, stock_code: str, target: str, *, include_baseline: bool = True
) -> Decimal | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT factor_cum FROM stock_adjust_factor"
            " WHERE stock_code=%s AND ex_date<%s ORDER BY ex_date DESC LIMIT 1",
            (stock_code, target),
        )
        previous_factor = cur.fetchone()
        if previous_factor is None and include_baseline:
            cur.execute(
                "SELECT baseline_factor FROM stock_factor_state WHERE stock_code=%s",
                (stock_code,),
            )
            previous_factor = cur.fetchone()
    if previous_factor is None or previous_factor[0] is None:
        return None
    return Decimal(str(previous_factor[0]))


def _derive_pctchg_events(
    conn, target: str, raw_rows: list[dict]
) -> tuple[list[dict], list[str]]:
    events = []
    unresolved = []
    for row in raw_rows:
        stock_code = row["stock_code"]
        previous = _previous_raw(conn, stock_code, target)
        if previous is None:
            continue
        if row.get("preclose") is None or previous[1] is None:
            unresolved.append(stock_code)
            continue
        if Decimal(str(row["preclose"])) == Decimal(str(previous[1])):
            continue
        previous_b = _previous_b(conn, stock_code, target)
        event_factor = event_factor_from_pctchg(
            previous[1], row.get("close"), row.get("change_percent")
        )
        if previous_b is None or event_factor is None:
            unresolved.append(stock_code)
            continue
        events.append({
            "stock_code": stock_code,
            "ex_date": target,
            "factor_cum": previous_b / event_factor,
            "source": "baostock-pctchg-derived",
        })
    return events, unresolved


def _authoritative_factor_mismatches(
    conn, target: str, raw_rows: list[dict], events: list[dict]
) -> list[dict]:
    raw_by_stock = {row["stock_code"]: row for row in raw_rows}
    mismatches = []
    for event in events:
        stock_code = event["stock_code"]
        row = raw_by_stock.get(stock_code)
        if row is None:
            continue
        previous = _previous_raw(conn, stock_code, target)
        previous_b = _previous_b(conn, stock_code, target, include_baseline=False)
        if previous is None or previous_b is None:
            continue
        pct_factor = event_factor_from_pctchg(
            previous[1], row.get("close"), row.get("change_percent")
        )
        if pct_factor is None or event["factor_cum"] <= 0:
            continue
        authoritative_factor = previous_b / Decimal(str(event["factor_cum"]))
        relative_error = factor_relative_error(pct_factor, authoritative_factor)
        if relative_error > Decimal("0.001"):
            mismatches.append({
                "stock_code": stock_code,
                "relative_error": float(relative_error),
            })
    return mismatches


def _raw_gap_codes(conn, target: str, raw_rows: list[dict]) -> list[str]:
    from .dates import trading_days_between

    gaps = []
    for row in raw_rows:
        previous = _previous_raw(conn, row["stock_code"], target)
        if previous is None:
            continue
        calendar = trading_days_between(previous[0], target)
        if len(calendar) < 2 or calendar[-2:] != [previous[0], target]:
            gaps.append(row["stock_code"])
    return gaps


def _stock_adjustment_part(conn, adapter, target: str, raw_rows: list[dict]) -> dict:
    from .adjustment import rebuild_stock

    started = datetime.now(timezone.utc)
    mode = "baostock-adjust-factor"
    unresolved: list[str] = []
    fallback_unavailable = False
    try:
        events = adapter.fetch_daily_adjust_factor(target)
    except FetchError as e:
        kind = fetch_failure_kind(e)
        if kind == "connection":
            kind = "upstream_unreachable"
        elif kind != "unsupported":
            kind = "fetch_failed"
        record_collection(
            conn, trade_date=target, section="stock_adjust_factor", status="error",
            error_kind=kind, detail=f"morning_batch: {e}", started_at=started,
        )
        mode = "baostock-pctchg-derived"
        fallback_unavailable = not raw_rows
        events, unresolved = _derive_pctchg_events(conn, target, raw_rows)

    mismatches = (
        _authoritative_factor_mismatches(conn, target, raw_rows, events)
        if mode == "baostock-adjust-factor" else []
    )
    raw_gaps = _raw_gap_codes(conn, target, raw_rows)
    pending_codes = (
        {item["stock_code"] for item in mismatches} | set(unresolved) | set(raw_gaps)
    )
    unknown_event_codes = (
        set(unresolved) | set(raw_gaps)
        if mode == "baostock-pctchg-derived" else set()
    )
    raw_by_stock: dict[str, list[dict]] = {}
    for row in raw_rows:
        raw_by_stock.setdefault(row["stock_code"], []).append(row)
    events_by_stock: dict[str, list[dict]] = {}
    for event in events:
        events_by_stock.setdefault(event["stock_code"], []).append(event)
    rebuild_pending: list[str] = []
    invalid_close: list[str] = []
    for stock_code in sorted(raw_by_stock.keys() | events_by_stock.keys()):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.baseline_factor, EXISTS("
                " SELECT 1 FROM stock_adjust_factor f"
                " WHERE f.stock_code=s.stock_code AND f.ex_date<%s)"
                " FROM stock_factor_state s WHERE s.stock_code=%s",
                (target, stock_code),
            )
            state = cur.fetchone()
        snapshot = rebuild_stock(
            conn,
            stock_code,
            raw_by_stock.get(stock_code, []),
            events_by_stock.get(stock_code, []),
            authoritative_event_dates=(
                () if stock_code in unknown_event_codes else (target,)
            ),
            baseline_factor=state[0] if state is not None and state[1] else None,
            rebuild_from=min(
                [target] + [event["ex_date"] for event in events_by_stock.get(stock_code, [])]
            ),
        )
        if snapshot.get("state") and snapshot["state"]["rebuild_pending"]:
            rebuild_pending.append(stock_code)
        if snapshot["quarantined"]:
            invalid_close.append(stock_code)
        if stock_code in pending_codes:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE stock_factor_state SET rebuild_pending=true WHERE stock_code=%s",
                    (stock_code,),
                )
            conn.commit()
    error_kind = (
        "factor_mismatch" if mismatches
        else "pctchg_derivation_failed" if unresolved or fallback_unavailable
        else "raw_gap" if raw_gaps
        else "rebuild_pending" if rebuild_pending
        else "invalid_close" if invalid_close else None
    )
    result_status = "error" if error_kind else ("ok" if events else "empty")
    record_collection(
        conn, trade_date=target, section="stock_adjust_factor",
        status=result_status, source=adapter.name, rows=len(events),
        error_kind=error_kind or (None if events else "no_data_for_date"),
        detail=(
            f"morning_batch:{mode};mismatches={mismatches};unresolved={unresolved}"
        ),
        started_at=started,
    )
    return {
        "status": result_status,
        "rows": len(events),
        "rebuilt": len(raw_by_stock.keys() | events_by_stock.keys()),
        "source": adapter.name,
        "mode": mode,
        **({"error_kind": error_kind, "mismatches": mismatches,
            "unresolved": unresolved} if error_kind else {}),
        **({"empty_kind": "no_data_for_date"} if not events and not error_kind else {}),
    }


def _index_daily_part(conn, adapters: Sequence[IndexDailyAdapter], target: str) -> dict:
    """7 大指数尾行缺失即补：缺者按尾行次日~目标日补抓，全量缺者从全历史起点补。"""
    from .tools_init import FULL_HISTORY_START

    started = datetime.now(timezone.utc)
    filled: list[dict] = []
    for code, name in MAJOR_INDICES:
        latest = select_index_daily(conn, code, limit=1)
        if latest and latest[-1]["trade_date"] >= target:
            continue
        # 尾行次日（不一定是交易日，宁多抓不少抓，上游对非交易日返回空行）；
        # 无任何历史行的指数从全历史起点补
        start = add_days(latest[-1]["trade_date"], 1) if latest else FULL_HISTORY_START
        try:
            r = fetch_index_daily_with_fallback(adapters, code, start, target)
        except AllSourcesFailed as e:
            filled.append({
                "index_code": code, "index_name": name, "status": "error",
                "error": str(e), "attempted_sources": e.attempted,
                "error_kind": aggregate_error_kind(e.attempted),
            })
            continue
        n = upsert_index_daily(conn, code, r["rows"], r["source"])
        filled.append({
            "index_code": code, "index_name": name, "status": "ok",
            "rows": n, "source": r["source"],
        })
    failed = [f for f in filled if f["status"] != "ok"]
    total_rows = sum(f.get("rows", 0) for f in filled)
    status = aggregate_batch_status(len(filled), len(failed))
    record_collection(
        conn, trade_date=target, section="index_daily",
        status="ok" if status != "error" else "error",
        rows=total_rows,
        detail=",".join(f["index_code"] for f in filled) or None,
        started_at=started,
    )
    return {"status": status, "filled": filled, "rows": total_rows}


def _sw_board_valuation_part(
    conn, sw_adapters: Sequence[SwBoardsAdapter], target: str
) -> dict:
    """申万板块每日分析报表（issue #118）：T+1 估值维度落 sw_board_valuation。

    申万官网单源（分页接口内自带逐页容错由适配器承担）；空结果（非交易日/
    报表未发布）为正常语义。业务键 (trade_date, board_code) 幂等。
    """
    started = datetime.now(timezone.utc)
    adapter = sw_adapters[0]
    try:
        rows = adapter.fetch_sw_board_analysis(target, target)
    except FetchError as e:
        kind = fetch_failure_kind(e)
        if kind == "connection":
            kind = "upstream_unreachable"
        elif kind != "unsupported":
            kind = "fetch_failed"
        record_collection(
            conn, trade_date=target, section="sw_board_valuation", status="error",
            error_kind=kind, detail=f"morning_batch: {e}", started_at=started,
        )
        return {"status": "error", "error": str(e), "error_kind": kind}
    n = upsert_sw_board_valuation(conn, rows)
    record_collection(
        conn, trade_date=target, section="sw_board_valuation",
        status="ok" if n > 0 else "empty", source=adapter.name, rows=n,
        error_kind=None if n > 0 else "no_data_for_date",
        detail="morning_batch",
        started_at=started,
    )
    return {
        "status": "ok" if n > 0 else "empty",
        "rows": n,
        "source": adapter.name,
        **({"empty_kind": "no_data_for_date"} if n == 0 else {}),
    }


def fetch_morning_batch(
    trade_date: str | None = None,
    adapter: AStockDailyAdapter | None = None,
    adapters: Sequence[IndexDailyAdapter] | None = None,
    sw_adapters: Sequence[SwBoardsAdapter] | None = None,
    now=None,
) -> dict:
    """次晨批：默认目标日 = 前一交易日（now 为测试注入时钟）。"""
    tool = "fetch_morning_batch"
    params = {"trade_date": trade_date}
    if trade_date is None:
        target = prev_trading_day(today_str(now))
    else:
        try:
            target = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    if target == today_str(now) and is_trading_time(now):
        # 盘中禁写防线（issue #78）：交易时段内当日数据非最终值，零落库
        return _error(
            tool, params,
            f"交易时段内不采集当日（{target}）数据：次晨批语义为前一交易日，"
            "请在开盘前执行或显式指定历史交易日",
        )
    if adapter is None:
        from .adapters.baostock_adapter import BaostockAdapter

        adapter = BaostockAdapter()
    if adapters is None:
        adapters = default_index_daily_adapters()
    if sw_adapters is None:
        from .adapters.sw_adapter import SwSResearchAdapter

        sw_adapters = [SwSResearchAdapter()]
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        stock_part, raw_rows = _stock_daily_part(conn, adapter, target)
        parts = {
            "stock_daily": stock_part,
            "stock_adjust_factor": _stock_adjustment_part(
                conn, adapter, target, raw_rows
            ),
            "index_daily": _index_daily_part(conn, adapters, target),
            "sw_board_valuation": _sw_board_valuation_part(conn, sw_adapters, target),
        }
    except BaostockError as exc:
        return baostock_error(tool, params, exc)
    finally:
        conn.close()
    failed = [k for k, v in parts.items() if v["status"] == "error"]
    status = aggregate_batch_status(len(parts), len(failed))
    out = {"status": status, "tool": tool, "params": params,
           "trade_date": target, "parts": parts,
           "stage": "verified" if not failed else "rebuilt",
           "messages": ["数据处理完成"] + ([] if failed else ["数据可以使用"])}
    if failed:
        out["failed_parts"] = failed
    return out
