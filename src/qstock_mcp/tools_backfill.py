"""backfill 核心层（issue #84，spec #71/W5）。

CLI 专用路径（qstock-scheduler backfill），不经 MCP
协议层，也不经 fetch 工具的历史日期防线（spec 明示"与 W1 防线不冲突"）：

- fetch_boards_backfill：push2his 板块 K 线同口径回补行业+概念板块历史入
  market_boards，回补行带 source='push2his' 标注（实时采集行为 NULL），
  业务键 (trade_date, board_type, board_name) 幂等可重跑；默认窗口 =
  前一交易日前推约 3 个月（90 自然日）。板块清单缺省取 market_boards 去重
  板块集；逐板先查台账断点续跑（同板块同窗口已有 ok 台账行则跳过，
  issue #91）；连续 CIRCUIT_BREAKER_CONSECUTIVE_FAILURES 板全源失败即熔断
  中止（issue #90）。盘中禁写延伸：显式 end=当日且交易时段内报错零落库。
- fetch_sw_boards_backfill（issue #118，spec #111）：申万官网 trend 指数全
  历史（1999 起）回补申万双级口径行入 market_boards（source='swsresearch'），
  板块历史深度直达 1999、解除 5/10 日涨跌"自落库日起积累"限制；申万官网
  current 接口取板块清单，逐板断点续跑/熔断/进度与东财回补同机制，台账
  detail 前缀 sw-backfill 互不串扰。调度入口 scheduler backfill --source sw。

两者均逐板块独立成败、逐板块/整体落采集台账，单板块失败不中断。

概念成分积累（accumulate_em_concepts，issue #84）已随概念维度退役摘除
（issue #119：THS 概念成分端点准入闸未过，按 #106 弃用方案实施；em 成分
字典存量行保留只读）。
"""

import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Sequence

from .adapters import BoardHistoryAdapter, default_adapters
from .dates import (
    add_days,
    is_trading_time,
    normalize_date,
    prev_trading_day,
    today_str,
)
from .db import connect
from .fetch_chain import (
    AllSourcesFailed,
    aggregate_error_kind,
    fetch_board_history_with_fallback,
    fetch_sw_board_history_with_fallback,
    fetch_sw_board_list_with_fallback,
)
from .ledger import record_collection, summarize_attempted
from .output import aggregate_batch_status
from .output import error as _error
from .repository import (
    select_distinct_boards,
    upsert_board_history,
)

log = logging.getLogger(__name__)

BACKFILL_SOURCE = "push2his"
SW_BACKFILL_SOURCE = "swsresearch"  # 申万官网 trend 回补行 source 标注（issue #118）
SW_BACKFILL_DEFAULT_START = "19990101"  # 申万官网历史深度（实测 1999-12-30 起）
DEFAULT_BACKFILL_DAYS = 90  # 约 3 个月（自然日）
PROGRESS_LOG_EVERY = 50  # 长循环每处理 N 个板块向 stderr 报一次进度
PROGRESS_LOG_INTERVAL_S = 60  # 距上条进度日志超过 N 秒也报（issue #89：慢路径可观测）
CIRCUIT_BREAKER_CONSECUTIVE_FAILURES = 10  # 连续全源失败熔断阈值（issue #90）


def _backfill_detail_prefix(board_type: str, board_name: str,
                            start: str, end: str) -> str:
    """backfill 台账 detail 前缀的单一事实来源（写入与断点续跑查询共用）。"""
    return f"backfill {board_type}/{board_name} {start}~{end}"


def _sw_backfill_detail_prefix(board_code: str, board_name: str,
                               start: str, end: str) -> str:
    """申万回补台账 detail 前缀（issue #118；与东财回补前缀区分，断点续跑
    按前缀匹配互不串扰）。"""
    return f"sw-backfill {board_code}/{board_name} {start}~{end}"


def _has_ok_backfill_ledger(conn, detail_prefix: str) -> bool:
    """断点续跑跳过判据（issue #91，spec #86）：detail 前缀匹配存在 status=ok
    的 backfill 台账行。empty 不算（上游当时无数据，窗口覆盖未确认）。"""
    pattern = (
        detail_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM collection_log WHERE section = 'market_boards'"
            " AND status = 'ok' AND detail LIKE %s ESCAPE '\\' LIMIT 1",
            (pattern,),
        )
        return cur.fetchone() is not None


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
        # 断点续跑（issue #91，spec #86）：同板块同窗口台账已 ok 的板块跳过，
        # 熔断/中断后的重跑只补缺口，不全量重抓
        skipped = [
            b for b in boards
            if _has_ok_backfill_ledger(
                conn, _backfill_detail_prefix(b["board_type"], b["board_name"], start_d, end_d))
        ]
        if skipped:
            skip_keys = {(b["board_type"], b["board_name"]) for b in skipped}
            boards = [
                b for b in boards
                if (b["board_type"], b["board_name"]) not in skip_keys
            ]
            log.info("backfill 跳过 %d 个板块（台账同窗口已 ok），待抓 %d",
                     len(skipped), len(boards))
        results = []
        log.info("backfill 开始：%d 个板块，窗口 %s~%s",
                 len(boards), start_d, end_d)
        last_logged = monotonic()
        consecutive_failures = 0
        abort_reason = None
        for i, b in enumerate(boards, 1):
            btype, bname = b["board_type"], b["board_name"]
            started = datetime.now(timezone.utc)
            try:
                r = fetch_board_history_with_fallback(
                    adapters, btype, bname, start_d, end_d)
            except AllSourcesFailed as e:
                kind = aggregate_error_kind(e.attempted)
                log.warning("板块历史回补失败 %s/%s: %s", btype, bname, e)
                results.append({
                    "board_type": btype, "board_name": bname,
                    "status": "error", "rows": 0,
                    "error": str(e), "error_kind": kind,
                })
                record_collection(
                    conn, trade_date=end_d, section="market_boards",
                    status="error", error_kind=kind,
                    detail=_backfill_detail_prefix(btype, bname, start_d, end_d)
                    + f": {summarize_attempted(e.attempted)}",
                    started_at=started,
                )
                consecutive_failures += 1
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
                    detail=_backfill_detail_prefix(btype, bname, start_d, end_d)
                    + f" fetch_source={r['source']}",
                    started_at=started,
                )
                consecutive_failures = 0  # ok/empty 都说明上游健康，重置计数
            # 熔断（issue #90，spec #86）：连续全源失败即中止——不区分
            # failure_kind（故障上游常 connection 级与 5xx 混合出现），避免在
            # 故障上游上空磨全部板块；已落库数据与逐板台账保留，幂等可续跑
            if consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILURES:
                abort_reason = (
                    f"连续 {consecutive_failures} 个板块全源失败，"
                    f"熔断于第 {i}/{len(boards)} 板（{bname}）：上游疑似故障"
                )
                log.warning("backfill aborted：%s", abort_reason)
                record_collection(
                    conn, trade_date=end_d, section="market_boards",
                    status="error", error_kind=kind,
                    detail=f"backfill aborted：{abort_reason}",
                    started_at=started,
                )
                break
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
    status = ("error" if abort_reason
              else aggregate_batch_status(len(results), len(failed)))
    out: dict = {
        "status": status,
        "tool": tool,
        "params": params,
        "window": {"start": start_d, "end": end_d},
        "boards": results,
        "failed_boards": failed,
        "skipped_boards": [b["board_name"] for b in skipped],
    }
    if abort_reason:
        out["aborted"] = True
        out["abort_reason"] = abort_reason
    return out


def fetch_sw_boards_backfill(
    start: str | None = None,
    end: str | None = None,
    adapters: Sequence | None = None,
    boards: list[dict] | None = None,
    now=None,
) -> dict:
    """申万板块历史回补（issue #118，spec #111）：申万官网 trend 指数全历史
    （1999 起，T+1）回补 market_boards 申万双级口径行——板块历史深度直达
    1999，5/10 日涨跌解除"自落库日起积累"限制。

    回补行 source='swsresearch' 与实时采集行（NULL）区分；业务键
    (trade_date, board_type, board_name) 幂等可重跑。默认窗口
    19990101~前一交易日（trend 一次调用即全历史，客户端过滤窗口）；
    板块清单缺省取申万官网 current 接口（31 一级 + 124 二级），逐板块断点
    续跑（台账同窗口已 ok 跳过，前缀 sw-backfill 与东财回补互不串扰）、
    连续全源失败熔断（复用 issue #90 阈值）、双阈值进度日志（issue #89）。
    CLI 专用路径不经历史日期防线，仅保留盘中禁写（显式 end=当日且交易时段
    内报错零落库）。申万单源无 fallback 链。
    """
    tool = "fetch_sw_boards_backfill"
    params = {"start": start, "end": end}
    try:
        end_d = normalize_date(end) if end else prev_trading_day(today_str(now))
        start_d = normalize_date(start) if start else SW_BACKFILL_DEFAULT_START
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
        from .adapters.sw_adapter import SwSResearchAdapter

        adapters = [SwSResearchAdapter()]
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        if boards is None:
            try:
                board_result = fetch_sw_board_list_with_fallback(adapters)
            except AllSourcesFailed as e:
                kind = aggregate_error_kind(e.attempted)
                record_collection(
                    conn, trade_date=end_d, section="market_boards",
                    status="error", error_kind=kind,
                    detail="sw-backfill board_list: "
                    + (summarize_attempted(e.attempted) or "全源失败"),
                )
                return _error(
                    tool, params, f"申万板块清单抓取失败: {e}"
                ) | {"error_kind": kind}
            boards = board_result["boards"]
        if not boards:
            return _error(tool, params, "申万板块清单为空")
        # 断点续跑（issue #91）：同板块同窗口台账已 ok 的跳过，重跑只补缺口
        skipped = [
            b for b in boards
            if _has_ok_backfill_ledger(
                conn,
                _sw_backfill_detail_prefix(
                    b["board_code"], b["board_name"], start_d, end_d),
            )
        ]
        if skipped:
            skip_keys = {(b["board_code"]) for b in skipped}
            boards = [b for b in boards if b["board_code"] not in skip_keys]
            log.info("sw-backfill 跳过 %d 个板块（台账同窗口已 ok），待抓 %d",
                     len(skipped), len(boards))
        results = []
        log.info("sw-backfill 开始：%d 个板块，窗口 %s~%s",
                 len(boards), start_d, end_d)
        last_logged = monotonic()
        consecutive_failures = 0
        abort_reason = None
        for i, b in enumerate(boards, 1):
            started = datetime.now(timezone.utc)
            try:
                r = fetch_sw_board_history_with_fallback(
                    adapters, b, start_d, end_d)
            except AllSourcesFailed as e:
                kind = aggregate_error_kind(e.attempted)
                log.warning("申万板块历史回补失败 %s/%s: %s",
                            b["board_code"], b["board_name"], e)
                results.append({
                    "board_code": b["board_code"],
                    "board_name": b["board_name"], "status": "error",
                    "rows": 0, "error": str(e), "error_kind": kind,
                })
                record_collection(
                    conn, trade_date=end_d, section="market_boards",
                    status="error", error_kind=kind,
                    detail=_sw_backfill_detail_prefix(
                        b["board_code"], b["board_name"], start_d, end_d)
                    + f": {summarize_attempted(e.attempted)}",
                    started_at=started,
                )
                consecutive_failures += 1
            else:
                n = upsert_board_history(conn, r["rows"], SW_BACKFILL_SOURCE)
                results.append({
                    "board_code": b["board_code"],
                    "board_name": b["board_name"],
                    "status": "ok" if n > 0 else "empty",
                    "rows": n, "source": r["source"],
                })
                record_collection(
                    conn, trade_date=end_d, section="market_boards",
                    status="ok" if n > 0 else "empty",
                    source=SW_BACKFILL_SOURCE, rows=n,
                    detail=_sw_backfill_detail_prefix(
                        b["board_code"], b["board_name"], start_d, end_d)
                    + f" fetch_source={r['source']}",
                    started_at=started,
                )
                consecutive_failures = 0  # ok/empty 都说明上游健康，重置计数
            # 熔断（issue #90，spec #86）：连续全源失败即中止，已落库数据与
            # 逐板台账保留，幂等可续跑
            if consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILURES:
                abort_reason = (
                    f"连续 {consecutive_failures} 个板块全源失败，"
                    f"熔断于第 {i}/{len(boards)} 板（{b['board_name']}）："
                    "上游疑似故障"
                )
                log.warning("sw-backfill aborted：%s", abort_reason)
                record_collection(
                    conn, trade_date=end_d, section="market_boards",
                    status="error", error_kind=kind,
                    detail=f"sw-backfill aborted：{abort_reason}",
                    started_at=started,
                )
                break
            now_m = monotonic()
            if _progress_due(i, len(boards), last_logged, now_m):
                last_logged = now_m
                done_ok = sum(1 for x in results if x["status"] != "error")
                log.info(
                    "sw-backfill 进度 %d/%d：成功 %d、失败 %d、累计落库 %d 行",
                    i, len(boards), done_ok, i - done_ok,
                    sum(x["rows"] for x in results))
    finally:
        conn.close()
    failed = [b["board_name"] for b in results if b["status"] == "error"]
    status = ("error" if abort_reason
              else aggregate_batch_status(len(results), len(failed)))
    out: dict = {
        "status": status,
        "tool": tool,
        "params": params,
        "fetch_source": SW_BACKFILL_SOURCE,
        "window": {"start": start_d, "end": end_d},
        "boards": results,
        "failed_boards": failed,
        "skipped_boards": [b["board_name"] for b in skipped],
    }
    if abort_reason:
        out["aborted"] = True
        out["abort_reason"] = abort_reason
    return out
