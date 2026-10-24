"""指数日线固定入口核心层（issue #117，spec #111）：抓取落库与覆盖核验。

fetch_index_backfill：逐指数经指数日线 fallback 链（腾讯 → baostock，
issue #117）抓取 [start, end] 并 upsert index_daily（业务键
(index_code, trade_date) 幂等），逐指数独立成败、逐指数落采集台账
（section=index_daily，detail 记区间）。CLI 专用路径：不经 fetch 工具的
历史日期防线（指数日线本就有历史语义），用于断档回填（如科创50 自
2026-10-13 起缺口）与收盘后当晚 T+0 当日行补采。盘中禁写（issue #78）：
交易时段内当日行不落库（deferred 计数进报告，未收盘数据非最终值）。

verify_index_coverage：覆盖核验（纯读不写）——逐指数读库内区间行数/
首末日期/来源分布，并对照 A 股交易日历（dates.is_trading_day）列示窗口内
缺失交易日，供复盘主要指数清单逐一对照。缺口定位即回填命令的 --start。
"""

import logging
from datetime import datetime, timezone
from typing import Sequence

from .adapters import DataAdapter, default_index_daily_adapters
from .dates import add_days, is_trading_day, is_trading_time, normalize_date, today_str
from .db import connect
from .fetch_chain import (
    AllSourcesFailed,
    aggregate_error_kind,
    fetch_index_daily_with_fallback,
)
from .ledger import record_collection, summarize_attempted
from .repository import select_index_daily, upsert_index_daily
from .tools_init import MAJOR_INDICES

log = logging.getLogger(__name__)

# 覆盖核验缺省窗口：近 31 自然日（约 20 个交易日），足够发现复盘级断档
VERIFY_WINDOW_DAYS = 31


def _codes_or_default(codes: Sequence[str] | str | None) -> list[str]:
    """None → MAJOR_INDICES 7 代码（单一事实来源）；CSV 字符串/列表 → 去空去重。"""
    if codes is None:
        return [c for c, _ in MAJOR_INDICES]
    if isinstance(codes, str):
        raw = codes.split(",")
    else:
        raw = list(codes)
    seen: set[str] = set()
    out: list[str] = []
    for c in raw:
        c = str(c).strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def fetch_index_backfill(
    codes: Sequence[str] | str | None = None,
    start: str | None = None,
    end: str | None = None,
    adapters: Sequence[DataAdapter] | None = None,
    now=None,
) -> dict:
    """指数日线区间抓取落库（固定入口核心）：断档回填与 T+0 当日行补采。

    start/end 缺省均为当日；逐指数独立成败（单指数失败不拖垮其他），
    结果带逐指数 source/rows/deferred；盘中（now 注入时钟，issue #78）
    当日行剔除并计 deferred。返回结构与退出码约定见 scheduler index。
    """
    tool = "fetch_index_backfill"
    params = {"codes": codes, "start": start, "end": end}
    today = today_str(now)
    try:
        start_n = normalize_date(start) if start else today
        end_n = normalize_date(end) if end else today
    except ValueError as e:
        return {"status": "error", "tool": tool, "params": params, "error": str(e)}
    if start_n > end_n:
        return {
            "status": "error",
            "tool": tool,
            "params": params,
            "error": f"start({start_n}) 晚于 end({end_n})",
        }
    selected = _codes_or_default(codes)
    if not selected:
        return {"status": "error", "tool": tool, "params": params, "error": "无指数代码"}
    if adapters is None:
        adapters = default_index_daily_adapters()
    defer_today = today if is_trading_time(now) and start_n <= today <= end_n else None
    conn, err = connect()
    if err:
        return {"status": "error", "tool": tool, "params": params, "error": err}
    indices: list[dict] = []
    try:
        for code in selected:
            started = datetime.now(timezone.utc)
            try:
                r = fetch_index_daily_with_fallback(adapters, code, start_n, end_n)
            except AllSourcesFailed as e:
                record_collection(
                    conn,
                    trade_date=end_n,
                    section="index_daily",
                    status="error",
                    error_kind=aggregate_error_kind(e.attempted),
                    detail=(
                        f"index_backfill {code} {start_n}~{end_n}: "
                        f"{summarize_attempted(e.attempted)}"
                    ),
                    started_at=started,
                )
                indices.append(
                    {
                        "index_code": code,
                        "status": "error",
                        "error": str(e),
                        "error_kind": aggregate_error_kind(e.attempted),
                        "attempted_sources": e.attempted,
                    }
                )
                continue
            rows = r["rows"]
            deferred = 0
            if defer_today is not None:
                keep = [row for row in rows if row["trade_date"] != defer_today]
                deferred = len(rows) - len(keep)
                rows = keep
            n = upsert_index_daily(conn, code, rows, r["source"])
            record_collection(
                conn,
                trade_date=end_n,
                section="index_daily",
                status="ok" if n > 0 else "empty",
                source=r["source"],
                rows=n,
                error_kind=None if n > 0 else "no_data_for_date",
                detail=f"index_backfill {code} {start_n}~{end_n}"
                + (f" deferred={deferred}" if deferred else ""),
                started_at=started,
            )
            entry = {
                "index_code": code,
                "status": "ok" if n > 0 else "empty",
                "rows": n,
                "source": r["source"],
            }
            if deferred:
                entry["deferred"] = deferred
            if n == 0:
                entry["empty_kind"] = "no_data_for_date"
            indices.append(entry)
    finally:
        conn.close()
    failed = [i for i in indices if i["status"] == "error"]
    if not failed:
        status = "ok"
    elif len(failed) == len(indices):
        status = "error"
    else:
        status = "partial_error"
    return {
        "status": status,
        "tool": tool,
        "params": params,
        "range": {"start": start_n, "end": end_n},
        "indices": indices,
    }


def verify_index_coverage(
    codes: Sequence[str] | str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """主要指数覆盖核验（纯读）：区间行数/首末日期/来源/交易日历缺口。

    窗口缺省 = 近 VERIFY_WINDOW_DAYS 自然日；missing_days 对照
    dates.is_trading_day 日历列示窗口内库内缺席的交易日（空表全窗口缺）。
    """
    tool = "verify_index_coverage"
    params = {"codes": codes, "start": start, "end": end}
    today = today_str()
    try:
        start_n = normalize_date(start) if start else add_days(today, -VERIFY_WINDOW_DAYS)
        end_n = normalize_date(end) if end else today
    except ValueError as e:
        return {"status": "error", "tool": tool, "params": params, "error": str(e)}
    if start_n > end_n:
        return {
            "status": "error",
            "tool": tool,
            "params": params,
            "error": f"start({start_n}) 晚于 end({end_n})",
        }
    selected = _codes_or_default(codes)
    names = dict(MAJOR_INDICES)
    # 交易日历窗口（含端点），上限防御：窗口超过 366 自然日按 error 拒
    if (int(end_n) - int(start_n)) > 10000:
        return {
            "status": "error",
            "tool": tool,
            "params": params,
            "error": "核验窗口过大（>10000 日），请缩小范围",
        }
    calendar: list[str] = []
    d = start_n
    while d <= end_n:
        if is_trading_day(d):
            calendar.append(d)
        d = add_days(d, 1)
    conn, err = connect()
    if err:
        return {"status": "error", "tool": tool, "params": params, "error": err}
    indices: list[dict] = []
    try:
        for code in selected:
            rows = select_index_daily(conn, code, start_n, end_n)
            present = {r["trade_date"] for r in rows}
            missing = [d for d in calendar if d not in present]
            indices.append(
                {
                    "index_code": code,
                    "index_name": names.get(code),
                    "rows": len(rows),
                    "first_date": rows[0]["trade_date"] if rows else None,
                    "last_date": rows[-1]["trade_date"] if rows else None,
                    "sources": sorted({r["source"] for r in rows if r.get("source")}),
                    "missing_days": missing,
                    "status": "ok" if not missing else "gap",
                }
            )
    finally:
        conn.close()
    gapped = [i for i in indices if i["status"] == "gap"]
    if not gapped:
        status = "ok"
    elif len(gapped) == len(indices):
        status = "error"
    else:
        status = "partial_error"
    return {
        "status": status,
        "tool": tool,
        "params": params,
        "range": {"start": start_n, "end": end_n},
        "calendar_days": len(calendar),
        "indices": indices,
    }
