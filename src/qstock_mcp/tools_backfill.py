"""backfill 与概念成分积累核心层（issue #84，spec #71/W5）。

CLI 专用路径（qstock-scheduler backfill / close 的 cons 部分），不经 MCP
协议层，也不经 fetch 工具的历史日期防线（spec 明示"与 W1 防线不冲突"）：

- fetch_boards_backfill：push2his 板块 K 线同口径回补行业+概念板块历史入
  market_boards，回补行带 source='push2his' 标注（实时采集行为 NULL），
  业务键 (trade_date, board_type, board_name) 幂等可重跑；默认窗口 =
  前一交易日前推约 3 个月（90 自然日）。板块清单缺省取 market_boards 去重
  板块集。盘中禁写延伸：显式 end=当日且交易时段内报错零落库。
- accumulate_em_concepts：东财健康时（由调用方按 boards section 成败门控）
  概念板块成分（cons 接口）积累入成分字典 sw_industry_class
  （source_system='em'，effective_date=当日，ON CONFLICT DO NOTHING）。

两者均逐板块独立成败、逐板块/整体落采集台账，单板块失败不中断。
"""

import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Sequence

from .adapters import BoardConsAdapter, BoardHistoryAdapter, default_adapters
from .dates import (
    add_days,
    dash_date,
    is_trading_time,
    normalize_date,
    prev_trading_day,
    today_str,
)
from .db import connect
from .fetch_chain import (
    AllSourcesFailed,
    aggregate_error_kind,
    fetch_board_cons_with_fallback,
    fetch_board_history_with_fallback,
)
from .ledger import record_collection, summarize_attempted
from .output import aggregate_batch_status
from .output import error as _error
from .repository import (
    select_distinct_boards,
    upsert_board_history,
    upsert_em_cons,
)

log = logging.getLogger(__name__)

BACKFILL_SOURCE = "push2his"
DEFAULT_BACKFILL_DAYS = 90  # 约 3 个月（自然日）
PROGRESS_LOG_EVERY = 50  # 长循环每处理 N 个板块向 stderr 报一次进度
PROGRESS_LOG_INTERVAL_S = 60  # 距上条进度日志超过 N 秒也报（issue #89：慢路径可观测）


def _progress_due(i: int, total: int, last_logged: float, now_m: float) -> bool:
    """双阈值进度判定（issue #89，spec #86）：计数每 PROGRESS_LOG_EVERY 板或
    距上次报告 ≥PROGRESS_LOG_INTERVAL_S 秒，先到先打；收尾必打。"""
    return (
        i % PROGRESS_LOG_EVERY == 0
        or i == total
        or now_m - last_logged >= PROGRESS_LOG_INTERVAL_S
    )


def fetch_boards_backfill(
    start: str | None = None,
    end: str | None = None,
    adapters: Sequence[BoardHistoryAdapter] | None = None,
    boards: list[dict] | None = None,
    now=None,
) -> dict:
    """板块历史回补：默认窗口 = 前一交易日前推约 3 个月。"""
    tool = "fetch_boards_backfill"
    params = {"start": start, "end": end}
    try:
        end_d = normalize_date(end) if end else prev_trading_day(today_str(now))
        start_d = normalize_date(start) if start else add_days(end_d, -DEFAULT_BACKFILL_DAYS)
    except ValueError as e:
        return _error(tool, params, str(e))
    if start_d > end_d:
        return _error(tool, params, f"start({start_d}) 晚于 end({end_d})")
    if end_d == today_str(now) and is_trading_time(now):
        # 盘中禁写防线延伸（issue #78）：交易时段内当日数据非最终值，零落库
        return _error(
            tool, params,
            f"交易时段内不回补当日（{end_d}）：请在收盘后执行或将 end 提前",
        )
    if adapters is None:
        adapters = default_adapters()
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        if boards is None:
            boards = select_distinct_boards(conn)
        if not boards:
            return _error(
                tool, params,
                "板块清单为空（market_boards 无任何历史行）："
                "请先执行收盘后批采集当日板块快照，再回补历史",
            )
        results = []
        log.info("backfill 开始：%d 个板块，窗口 %s~%s",
                 len(boards), start_d, end_d)
        last_logged = monotonic()
        for i, b in enumerate(boards, 1):
            btype, bname = b["board_type"], b["board_name"]
            started = datetime.now(timezone.utc)
            try:
                r = fetch_board_history_with_fallback(
                    adapters, btype, bname, start_d, end_d)
            except AllSourcesFailed as e:
                kind = aggregate_error_kind(e.attempted)
                results.append({
                    "board_type": btype, "board_name": bname,
                    "status": "error", "rows": 0,
                    "error": str(e), "error_kind": kind,
                })
                record_collection(
                    conn, trade_date=end_d, section="market_boards",
                    status="error", error_kind=kind,
                    detail=f"backfill {btype}/{bname} {start_d}~{end_d}: "
                           f"{summarize_attempted(e.attempted)}",
                    started_at=started,
                )
            else:
                n = upsert_board_history(conn, r["rows"], BACKFILL_SOURCE)
                results.append({
                    "board_type": btype, "board_name": bname,
                    "status": "ok" if n > 0 else "empty",
                    "rows": n, "source": r["source"],
                })
                record_collection(
                    conn, trade_date=end_d, section="market_boards",
                    status="ok" if n > 0 else "empty",
                    source=BACKFILL_SOURCE, rows=n,
                    detail=f"backfill {btype}/{bname} {start_d}~{end_d}"
                           f" fetch_source={r['source']}",
                    started_at=started,
                )
            now_m = monotonic()
            if _progress_due(i, len(boards), last_logged, now_m):
                last_logged = now_m
                done_ok = sum(1 for x in results if x["status"] != "error")
                log.info(
                    "backfill 进度 %d/%d：成功 %d、失败 %d、累计落库 %d 行",
                    i, len(boards), done_ok, i - done_ok,
                    sum(x["rows"] for x in results))
    finally:
        conn.close()
    failed = [b["board_name"] for b in results if b["status"] == "error"]
    status = aggregate_batch_status(len(results), len(failed))
    return {
        "status": status,
        "tool": tool,
        "params": params,
        "window": {"start": start_d, "end": end_d},
        "boards": results,
        "failed_boards": failed,
    }


def accumulate_em_concepts(
    trade_date: str | None = None,
    adapters: Sequence[BoardConsAdapter] | None = None,
    now=None,
) -> dict:
    """概念成分积累：当日 market_boards 概念板块逐板抓成分，入成分字典
    （source_system='em'）。逐板独立成败；整体落一行台账。"""
    tool = "accumulate_em_concepts"
    params = {"trade_date": trade_date}
    try:
        target = normalize_date(trade_date) if trade_date else today_str(now)
    except ValueError as e:
        return _error(tool, params, str(e))
    if adapters is None:
        adapters = default_adapters()
    effective = dash_date(target)  # date 列
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    started = datetime.now(timezone.utc)
    try:
        boards = [
            b for b in select_distinct_boards(conn, trade_date=target)
            if b["board_type"] == "concept" and b["board_code"]
        ]
        inserted = 0
        failed: list[str] = []
        log.info("cons 开始：%d 个概念板块", len(boards))
        last_logged = monotonic()
        for i, b in enumerate(boards, 1):
            try:
                r = fetch_board_cons_with_fallback(adapters, b["board_name"])
            except AllSourcesFailed as e:
                log.warning("概念成分抓取失败 %s: %s", b["board_name"], e)
                failed.append(b["board_name"])
            else:
                inserted += upsert_em_cons(
                    conn,
                    [{"stock_code": m["stock_code"], "board_code": b["board_code"]}
                     for m in r["rows"]],
                    effective,
                )
            now_m = monotonic()
            if _progress_due(i, len(boards), last_logged, now_m):
                last_logged = now_m
                log.info("cons 进度 %d/%d：失败 %d、累计写入 %d 行",
                         i, len(boards), len(failed), inserted)
        status = aggregate_batch_status(len(boards), len(failed))
        record_collection(
            conn, trade_date=target, section="sw_industry_class",
            status="error" if status == "error" else "ok",
            source="em", rows=inserted,
            detail=f"cons boards={len(boards)} failed={len(failed)}",
            started_at=started,
        )
    finally:
        conn.close()
    return {
        "status": status,
        "tool": tool,
        "params": params,
        "trade_date": target,
        "boards": len(boards),
        "rows": inserted,
        **({"failed_boards": failed} if failed else {}),
    }
