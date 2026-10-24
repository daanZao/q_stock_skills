"""qstock-scheduler：调度 CLI（直调 tools_* 核心层，不经 MCP 协议层）。

子命令：
  close [--date YYYYMMDD]    收盘后批：盘面五 section（boards/zt_pool/
                             strong_stocks/lhb/indices）快照采集，各 section
                             独立成败报告、逐 section 落采集台账（台账由
                             tools_board 内联记录，本层不重复落账）。
                             boards 成功时附带概念成分积累（cons → 成分字典
                             'em' 体系）；cons 独立成败落台账、不拖垮主批
                             （不影响退出码）。在册 planned_absent（第四态，
                             issue #113）的 section 占位通过：响应
                             status=planned_absent、不计失败、不影响退出码。
  morning [--date YYYYMMDD]  次晨批：baostock 一把抓前一交易日全市场日线
                             + 7 大指数尾行补漏（tools_morning），默认目标日
                             为前一交易日。
  backfill [--start D] [--end D]
                             板块历史回补：push2his 板块 K 线回补
                             market_boards（默认窗口 = 前一交易日前推约
                             3 个月），CLI 专用路径，不经历史日期防线。
  daily [--codes A,B] [--all] [--limit N] [--date D | --start D --end D]
        [--adj qfq|hfq|none] [--check] [--throttle SEC]
                             个股日线固定入口（issue #112，spec #111）：
                             默认走 腾讯→新浪→baostock fallback 链抓取落库；
                             --check 三源直取对拍（不落库）。--all 从
                             stock_list 取全市场（建议配 --throttle 1 避风控）。
  fundamentals --codes A,B   基本面透传固定入口（issue #115，spec #111）：
                             逐 code 走 get_fundamentals 链（akshare→baostock，
                             估值走百度股市通、base_info planned_absent）出
                             状态报告；proxy 面不落库，可挂定时任务做链路巡检。
  snapshot [--date D] [--check] [--limit N]
                             全市场快照固定入口（issue #116，spec #111）：
                             默认走 腾讯批量报价→腾讯 rank→新浪 三层链抓取
                             落库；--check 三源直取对拍（不落库）。

退出码：0 = 全部 section/part 成功（含 empty 无数据，非故障）；1 = 任一
section/part 失败或输入非法（失败明细在 stdout JSON）；2 = 参数错误
（argparse）。异常收敛为结构化 JSON 错误输出，不向调用方抛栈。
"""

import argparse
import json
import logging
import sys
import time
from typing import Sequence

from .adapters import BoardAdapter, SnapshotAdapter
from .daily_parity import compare_daily_sources
from .dates import normalize_date, today_str
from .db import connect
from .output import aggregate_batch_status
from .repository import select_stock_codes
from .snapshot_parity import compare_snapshot_sources
from .tools_backfill import accumulate_em_concepts, fetch_boards_backfill
from .tools_board import fetch_board_snapshot
from .tools_daily import fetch_daily
from .tools_morning import fetch_morning_batch
from .tools_snapshot import fetch_market_snapshot

CLOSE_SECTIONS = ("indices", "boards", "zt_pool", "strong_stocks", "lhb")


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def cmd_close(date_arg: str | None,
              adapters: Sequence[BoardAdapter] | None = None,
              cons_adapters: Sequence | None = None) -> int:
    """收盘后批：五 section 盘面快照 + 概念成分积累。任一 section 失败 exit 1。"""
    result = fetch_board_snapshot(
        trade_date=date_arg, sections=list(CLOSE_SECTIONS), adapters=adapters)
    failed = result.get("failed_sections")
    if failed is None:
        # 参数解析等前置失败（fetch_board_snapshot 的 error 契约，无 sections）
        _emit({"status": "error", "job": "close",
               "error": result.get("error", "未知错误")})
        return 1
    out = {
        "status": "ok" if not failed else "error",
        "job": "close",
        "trade_date": result["trade_date"],
        "sections": result["sections"],
        "failed_sections": failed,
    }
    if failed and result.get("error"):
        out["error"] = result["error"]
        if result.get("error_kind"):
            out["error_kind"] = result["error_kind"]
    # 概念成分积累（issue #84）：东财健康（boards 成功且有行）时执行；
    # 独立成败落台账，不拖垮主批（不影响退出码）
    boards = result["sections"].get("boards") or {}
    if boards.get("status") == "ok" and boards.get("rows", 0) > 0:
        cons = accumulate_em_concepts(
            trade_date=result["trade_date"], adapters=cons_adapters)
        out["cons"] = {k: v for k, v in cons.items() if k != "tool"}
    else:
        out["cons"] = {"status": "skipped",
                       "reason": "boards section 未成功（东财不健康），跳过概念成分积累"}
    _emit(out)
    return 0 if not failed else 1


def cmd_morning(date_arg: str | None) -> int:
    """次晨批：全市场日线 + 指数尾行补漏。任一部分 error 时 exit 1
    （partial_error 也算失败——调度侧需要感知）。"""
    result = fetch_morning_batch(trade_date=date_arg)
    out = {"job": "morning", **result}
    _emit(out)
    return 0 if result.get("status") == "ok" else 1


def cmd_backfill(start: str | None, end: str | None) -> int:
    """板块历史回补：非全 ok（partial_error/error）exit 1。"""
    result = fetch_boards_backfill(start=start, end=end)
    out = {"job": "backfill", **result}
    _emit(out)
    return 0 if result.get("status") == "ok" else 1


def _parse_codes(codes_arg: str | None) -> list[str]:
    """逗号分隔代码 → 去空去重（保持输入顺序）。"""
    seen: set[str] = set()
    codes: list[str] = []
    for raw in (codes_arg or "").split(","):
        code = raw.strip()
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _daily_range(
    date_arg: str | None, start_arg: str | None, end_arg: str | None
) -> tuple[tuple[str, str] | None, str | None]:
    """解析日线日期参数：--date 单日 / --start+--end 区间 / 缺省当日。"""
    if date_arg and (start_arg or end_arg):
        return None, "--date 与 --start/--end 互斥"
    try:
        if date_arg:
            day = normalize_date(date_arg)
            return (day, day), None
        if start_arg or end_arg:
            first = start_arg if start_arg is not None else end_arg
            last = end_arg if end_arg is not None else start_arg
            assert first is not None and last is not None
            start = normalize_date(first)
            end = normalize_date(last)
            if start > end:
                return None, f"起始日 {start} 晚于结束日 {end}"
            return (start, end), None
        today = today_str()
        return (today, today), None
    except ValueError as e:
        return None, str(e)


def cmd_daily(
    codes_arg: str | None = None,
    all_codes: bool = False,
    limit: int | None = None,
    date_arg: str | None = None,
    start_arg: str | None = None,
    end_arg: str | None = None,
    adj: str = "qfq",
    check: bool = False,
    throttle: float | None = None,
    daily_adapters: Sequence | None = None,
) -> int:
    """个股日线固定入口（issue #112）：抓取落库 / 三源对拍。

    - 默认（fetch）：逐 code 走 腾讯→新浪→baostock fallback 链落库；
    - --check：逐源直取结束日 bar 做字段级对拍，不落库；
    - --all：从 stock_list 取全市场，缺省按 1 秒/只节流（腾讯连接级限速实测）。
    """
    tool = "daily"
    rng, err = _daily_range(date_arg, start_arg, end_arg)
    if err:
        _emit({"status": "error", "job": tool, "error": err})
        return 1
    assert rng is not None
    start, end = rng
    codes = _parse_codes(codes_arg)
    if codes and all_codes:
        _emit({"status": "error", "job": tool,
               "error": "--codes 与 --all 互斥"})
        return 1
    if not codes and not all_codes:
        _emit({"status": "error", "job": tool,
               "error": "需要 --codes 或 --all 指定标的"})
        return 1
    if all_codes:
        conn, db_err = connect()
        if db_err:
            _emit({"status": "error", "job": tool, "error": db_err})
            return 1
        try:
            codes = select_stock_codes(conn)
        finally:
            conn.close()
        if limit is not None:
            codes = codes[:limit]
        if not codes:
            _emit({"status": "error", "job": tool,
                   "error": "股票清单为空（先运行 qstock-mcp init 或 init_database）"})
            return 1
    if check:
        result = compare_daily_sources(
            codes, end, adj=adj, adapters=daily_adapters
        )
        _emit({"job": tool, "mode": "check", **result})
        return 0 if result.get("status") == "ok" else 1
    pause = throttle if throttle is not None else (1.0 if all_codes else 0.0)
    results: list[dict] = []
    for i, code in enumerate(codes):
        raw = fetch_daily(
            code, start=start, end=end, adj=adj, adapters=daily_adapters
        )
        entry: dict = {"stock_code": code, "status": raw.get("status")}
        if raw.get("status") == "ok":
            segments = raw.get("segments", [])
            entry["source"] = (
                ",".join(sorted({s["source"] for s in segments}))
                if segments
                else None
            )
            entry["rows_upserted"] = raw.get("rows_upserted", 0)
            entry["data_range"] = raw.get("data_range")
            if not segments:
                entry["note"] = "区间内无缺口，零抓取"
            elif raw.get("note"):
                entry["note"] = raw["note"]
        else:
            entry["error"] = raw.get("error")
            if raw.get("attempted_sources"):
                entry["attempted_sources"] = raw["attempted_sources"]
        results.append(entry)
        if pause and i < len(codes) - 1:
            time.sleep(pause)
    succeeded = sum(1 for r in results if r["status"] == "ok")
    failed = len(results) - succeeded
    status = aggregate_batch_status(len(results), failed)
    _emit({
        "status": status,
        "job": tool,
        "mode": "fetch",
        "range": {"start": start, "end": end},
        "adj": adj,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    })
    return 0 if status == "ok" else 1


def cmd_fundamentals(
    codes_arg: str | None = None,
    fundamentals_adapters: Sequence | None = None,
) -> int:
    """基本面透传固定入口（issue #115）：逐 code 走 get_fundamentals 链。

    proxy 面不落库——本命令产出逐 code 状态报告（section 覆盖/单 section
    errors/弃用清单/逐源明细），一次开发可重复运行，可挂定时任务做基本面
    链路健康巡检与代表股真实拉取冒烟（验收入口）。
    """
    tool = "fundamentals"
    codes = _parse_codes(codes_arg)
    if not codes:
        _emit({"status": "error", "job": tool,
               "error": "需要 --codes 指定标的（逗号分隔）"})
        return 1
    from .tools_fundamentals import get_fundamentals

    results: list[dict] = []
    for code in codes:
        raw = get_fundamentals(code, adapters=fundamentals_adapters)
        entry: dict = {"stock_code": code, "status": raw.get("status")}
        if raw.get("status") == "ok":
            data = raw.get("data") or {}
            entry["source"] = raw.get("source")
            entry["sections"] = sorted(
                k for k in data if k != "errors" and isinstance(data[k], (dict, list))
            )
            entry["planned_absent"] = raw.get("planned_absent") or {}
            if data.get("errors"):
                entry["section_errors"] = data["errors"]
        else:
            entry["error"] = raw.get("error")
            entry["planned_absent"] = raw.get("planned_absent") or {}
            if raw.get("attempted_sources"):
                entry["attempted_sources"] = raw["attempted_sources"]
        results.append(entry)
    succeeded = sum(1 for r in results if r["status"] == "ok")
    failed = len(results) - succeeded
    status = aggregate_batch_status(len(results), failed)
    _emit({
        "status": status,
        "job": tool,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    })
    return 0 if status == "ok" else 1


def cmd_snapshot(
    date_arg: str | None = None,
    check: bool = False,
    limit: int | None = None,
    snapshot_adapters: Sequence[SnapshotAdapter] | None = None,
) -> int:
    """全市场快照固定入口（issue #116）：三层链抓取落库 / 三源对拍。

    默认（fetch）经 腾讯批量报价→腾讯 rank→新浪 链抓取落库（台账由
    tools_snapshot 内联记录）；--check 逐源直取字段级对拍，不落库。
    """
    tool = "snapshot"
    if check:
        result = compare_snapshot_sources(
            adapters=snapshot_adapters, codes_limit=limit
        )
        _emit({"job": tool, "mode": "check", **result})
        return 0 if result.get("status") == "ok" else 1
    result = fetch_market_snapshot(trade_date=date_arg, adapters=snapshot_adapters)
    out = {"job": tool, "mode": "fetch", **result}
    _emit(out)
    return 0 if result.get("status") == "ok" else 1


def main(argv: list[str] | None = None,
         adapters: Sequence[BoardAdapter] | None = None,
         cons_adapters: Sequence | None = None,
         daily_adapters: Sequence | None = None,
         fundamentals_adapters: Sequence | None = None,
         snapshot_adapters: Sequence[SnapshotAdapter] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="qstock-scheduler",
        description="调度 CLI：定时批采集（直调 tools_* 核心层，不经 MCP）")
    sub = ap.add_subparsers(dest="command", required=True)
    p_close = sub.add_parser(
        "close", help="收盘后批：盘面五 section 快照采集并落台账（附概念成分积累）")
    p_close.add_argument("--date", default=None,
                         help="交易日 yyyymmdd，缺省当日")
    p_morning = sub.add_parser(
        "morning", help="次晨批：baostock 一把抓前日全市场日线 + 指数尾行补漏")
    p_morning.add_argument("--date", default=None,
                           help="目标交易日 yyyymmdd，缺省前一交易日")
    p_backfill = sub.add_parser(
        "backfill", help="板块历史回补：push2his 板块 K 线回补 market_boards")
    p_backfill.add_argument("--start", default=None,
                            help="回补起点 yyyymmdd，缺省 end 前推约 3 个月")
    p_backfill.add_argument("--end", default=None,
                            help="回补终点 yyyymmdd，缺省前一交易日")
    p_daily = sub.add_parser(
        "daily", help="个股日线：腾讯→新浪→baostock 抓取落库 / 三源对拍")
    p_daily.add_argument("--codes", default=None,
                         help="逗号分隔股票代码（支持北交所旧码 43x/83x/87x）")
    p_daily.add_argument("--all", action="store_true",
                         help="从数据库 stock_list 取全市场标的")
    p_daily.add_argument("--limit", type=int, default=None,
                         help="--all 时只取前 N 只（冒烟用）")
    p_daily.add_argument("--date", default=None,
                         help="单日 yyyymmdd，缺省当日")
    p_daily.add_argument("--start", default=None,
                         help="区间起点 yyyymmdd（与 --end 配对）")
    p_daily.add_argument("--end", default=None,
                         help="区间终点 yyyymmdd（与 --start 配对）")
    p_daily.add_argument("--adj", default="qfq", choices=("qfq", "hfq", "none"),
                         help="复权口径，缺省 qfq")
    p_daily.add_argument("--check", action="store_true",
                         help="对拍模式：逐源直取结束日 bar 比对，不落库")
    p_daily.add_argument("--throttle", type=float, default=None,
                         help="逐 code 间隔秒；--all 缺省 1.0，显式 codes 缺省 0")
    p_fund = sub.add_parser(
        "fundamentals",
        help="基本面透传：逐 code 走 get_fundamentals 链出状态报告（不落库）")
    p_fund.add_argument("--codes", default=None,
                        help="逗号分隔股票代码（必填），如 600519,000001")
    p_snapshot = sub.add_parser(
        "snapshot", help="全市场快照：腾讯批量报价→腾讯 rank→新浪抓取落库 / 三源对拍")
    p_snapshot.add_argument("--date", default=None,
                            help="交易日 yyyymmdd，缺省由 API 报告/当日回退"
                                 "（仅主源含日期时间戳可带参校验）")
    p_snapshot.add_argument("--check", action="store_true",
                            help="对拍模式：逐源直取字段级比较，不落库")
    p_snapshot.add_argument("--limit", type=int, default=None,
                            help="只对拍前 N 只（按代码序，冒烟用；抓取模式忽略）")
    args = ap.parse_args(argv)  # 参数错误由 argparse 以退出码 2 终止
    # stdout 留给最终结构化 JSON；进度/日志走 stderr（与 MCP 侧"日志只走
    # stderr"同契约）
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        if args.command == "close":
            return cmd_close(args.date, adapters=adapters,
                             cons_adapters=cons_adapters)
        if args.command == "morning":
            return cmd_morning(args.date)
        if args.command == "daily":
            return cmd_daily(
                codes_arg=args.codes,
                all_codes=args.all,
                limit=args.limit,
                date_arg=args.date,
                start_arg=args.start,
                end_arg=args.end,
                adj=args.adj,
                check=args.check,
                throttle=args.throttle,
                daily_adapters=daily_adapters,
            )
        if args.command == "snapshot":
            return cmd_snapshot(
                date_arg=args.date,
                check=args.check,
                limit=args.limit,
                snapshot_adapters=snapshot_adapters,
            )
        if args.command == "backfill":
            return cmd_backfill(args.start, args.end)
        if args.command == "fundamentals":
            return cmd_fundamentals(
                codes_arg=args.codes,
                fundamentals_adapters=fundamentals_adapters,
            )
    except Exception as e:  # 零例外：异常收敛为结构化错误输出
        _emit({"status": "error", "job": args.command,
               "error": f"{type(e).__name__}: {e}"})
        return 1
    _emit({"status": "error", "error": f"未知子命令: {args.command!r}"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
