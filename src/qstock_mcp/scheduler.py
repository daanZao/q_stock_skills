"""qstock-scheduler：调度 CLI（直调 tools_* 核心层，不经 MCP 协议层）。

子命令：
  close [--date YYYYMMDD]    收盘后批：盘面五 section（boards/zt_pool/
                             strong_stocks/lhb/indices）快照采集，各 section
                             独立成败报告、逐 section 落采集台账（台账由
                             tools_board 内联记录，本层不重复落账）。
  morning [--date YYYYMMDD]  次晨批：baostock 一把抓前一交易日全市场日线
                             + 7 大指数尾行补漏（tools_morning），默认目标日
                             为前一交易日。

退出码：0 = 全部 section/part 成功（含 empty 无数据，非故障）；1 = 任一
section/part 失败或输入非法（失败明细在 stdout JSON）；2 = 参数错误
（argparse）。异常收敛为结构化 JSON 错误输出，不向调用方抛栈。
"""

import argparse
import json
import sys
from typing import Sequence

from .adapters import BoardAdapter
from .tools_board import fetch_board_snapshot
from .tools_morning import fetch_morning_batch

CLOSE_SECTIONS = ("indices", "boards", "zt_pool", "strong_stocks", "lhb")


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def cmd_close(date_arg: str | None,
              adapters: Sequence[BoardAdapter] | None = None) -> int:
    """收盘后批：五 section 盘面快照。任一 section 失败 exit 1。"""
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
    _emit(out)
    return 0 if not failed else 1


def cmd_morning(date_arg: str | None) -> int:
    """次晨批：全市场日线 + 指数尾行补漏。任一部分 error 时 exit 1
    （partial_error 也算失败——调度侧需要感知）。"""
    result = fetch_morning_batch(trade_date=date_arg)
    out = {"job": "morning", **result}
    _emit(out)
    return 0 if result.get("status") == "ok" else 1


def main(argv: list[str] | None = None,
         adapters: Sequence[BoardAdapter] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="qstock-scheduler",
        description="调度 CLI：定时批采集（直调 tools_* 核心层，不经 MCP）")
    sub = ap.add_subparsers(dest="command", required=True)
    p_close = sub.add_parser(
        "close", help="收盘后批：盘面五 section 快照采集并落台账")
    p_close.add_argument("--date", default=None,
                         help="交易日 yyyymmdd，缺省当日")
    p_morning = sub.add_parser(
        "morning", help="次晨批：baostock 一把抓前日全市场日线 + 指数尾行补漏")
    p_morning.add_argument("--date", default=None,
                           help="目标交易日 yyyymmdd，缺省前一交易日")
    args = ap.parse_args(argv)  # 参数错误由 argparse 以退出码 2 终止

    try:
        if args.command == "close":
            return cmd_close(args.date, adapters=adapters)
        if args.command == "morning":
            return cmd_morning(args.date)
    except Exception as e:  # 零例外：异常收敛为结构化错误输出
        _emit({"status": "error", "job": args.command,
               "error": f"{type(e).__name__}: {e}"})
        return 1
    _emit({"status": "error", "error": f"未知子命令: {args.command!r}"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
