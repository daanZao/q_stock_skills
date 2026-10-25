"""次晨批核心层（issue #83，spec #71/W5）：开盘前采集前一交易日数据。

两个部分独立成败、独立落采集台账：
1. stock_daily：baostock `query_daily_history_k_AStock` 一把抓全市场日线
   （实测 5,225 行/约 20s），按业务键 (stock_code, trade_date, adj) 幂等
   落库——与既有日线头尾自愈共用业务键，重复执行幂等覆盖，不冲突。
   单源调用不走 fallback 链（一把抓是 baostock 独有接口）；失败按三态
   契约分类 error_kind 落台账。
2. index_daily：7 大指数白名单尾行缺失即补（spec 补充输入 07a/07b）——
   尾行早于目标日的指数按尾行次日起经 fetch_index_daily fallback 链补抓；
   尾行已在目标日的指数零请求。
3. sw_board_valuation：申万板块每日分析报表（issue #118，spec #111）——
   换手率/PE/PB/流通市值/股息率，T+1 发布（前一交易日报表次晨可查），
   申万官网单源（SwBoardsAdapter 协议，注入缝 sw_adapters），按业务键
   (trade_date, board_code) 幂等落 sw_board_valuation 表。

盘中禁写防线（issue #78）延伸：显式指定目标日为当日且正在交易时段时报错
零落库（次晨批语义是抓"已收盘"的前一交易日）。调度入口见 scheduler.py
morning 子命令；本层不注册 MCP 工具。
"""

import logging
from datetime import datetime, timezone
from typing import Sequence

from .adapters import AStockDailyAdapter, IndexDailyAdapter, SwBoardsAdapter, default_adapters
from .adapters.base import FetchError, fetch_failure_kind
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
from .tools_init import MAJOR_INDICES

log = logging.getLogger(__name__)


def _stock_daily_part(conn, adapter, target: str) -> dict:
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
        return {"status": "error", "error": str(e), "error_kind": kind}
    n = upsert_daily_rows(conn, rows, adapter.name)
    record_collection(
        conn, trade_date=target, section="stock_daily",
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
        adapters = default_adapters()
    if sw_adapters is None:
        from .adapters.sw_adapter import SwSResearchAdapter

        sw_adapters = [SwSResearchAdapter()]
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        parts = {
            "stock_daily": _stock_daily_part(conn, adapter, target),
            "index_daily": _index_daily_part(conn, adapters, target),
            "sw_board_valuation": _sw_board_valuation_part(conn, sw_adapters, target),
        }
    finally:
        conn.close()
    failed = [k for k, v in parts.items() if v["status"] == "error"]
    status = aggregate_batch_status(len(parts), len(failed))
    out = {"status": status, "tool": tool, "params": params,
           "trade_date": target, "parts": parts}
    if failed:
        out["failed_parts"] = failed
    return out
