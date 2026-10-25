"""qstock-scheduler：调度 CLI（直调 tools_* 核心层，不经 MCP 协议层）。

子命令：
  close [--date YYYYMMDD]    收盘后批：盘面五 section（boards/zt_pool/
                             strong_stocks/lhb/indices）快照采集，各 section
                             独立成败报告、逐 section 落采集台账（台账由
                             tools_board 内联记录，本层不重复落账）。
                             boards 行业段走申万官网单源链（issue #118，
                             spec #111），申万不发布字段以
                             planned_absent_fields 声明；概念成分积累已摘除
                             （概念维度退役，issue #119 准入闸未过，#106
                             弃用方案：em 成分字典存量行保留只读）。在册
                             planned_absent（第四态，issue #113）的 section
                             占位通过：响应
                             status=planned_absent、不计失败、不影响退出码。
                             lhb section 走专用链（交易所官方单源，issue #114/
                             #121；新浪互补校验走 lhb --check 对拍）。
  morning [--date YYYYMMDD]  次晨批：baostock 一把抓前一交易日全市场日线
                             + 7 大指数尾行补漏 + 申万板块分析报表估值维度
                             （sw_board_valuation part，issue #118，T+1），
                             默认目标日为前一交易日。
  backfill [--start D] [--end D] [--source sw]
                             板块历史回补（issue #121 定稿申万单源）：申万官网
                             trend 指数全历史回补申万双级口径行（issue #118，
                             默认窗口 19990101~前一交易日，历史深至 1999），
                             回补行 source='swsresearch'。东财 push2his 回补
                             已随 issue #121 摘除。CLI 专用路径，不经历史
                             日期防线。
  sw-boards [--date D] [--check] [--level 1|2|all] [--limit N]
                             申万板块固定入口（issue #118）：默认抓申万
                             双级实时快照落 market_boards（收盘后调用即当日
                             收盘截面；与 close 的 boards 段同链，供 boards
                             单段重跑/冒烟）；--check 三方位对拍（申万官网
                             vs 库内落库行 vs 库内自算，不落库），--limit
                             只对拍前 N 个板块（冒烟用）。
  sw-dict [--url U]          申万成分字典在线刷新（issue #118）：官网分类
                             xls 直链下载 → 解析 → 清空重载 'sw' 体系
                             （含北交所归属），复用 import_board_classification
                             事务语义，幂等可重复。
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
  index [--codes A,B] [--date D | --start D --end D] [--verify]
                             指数日线固定入口（issue #117，spec #111）：
                             默认走 腾讯→baostock 链抓取落库（断档回填/T+0
                             当日行补采）；--verify 纯读覆盖核验（对照交易日历
                             列缺口），不写库。缺省代码 = 7 大指数白名单。
  lhb [--date D] [--start D --end D] [--check]
                             龙虎榜固定入口（issue #114，spec #111）：沪深北
                             交易所官方源（深所 JSON/上所文本/北所 JSONP）抓取
                             落库，逐日幂等；--start/--end 历史回溯（单日失败
                             不中断）；--check 新浪互补对拍（不落库，可手动
                             触发）。
  limitpool [--date D] [--probe]
                             涨停池固定入口（issue #120，spec #111）：zt_pool
                             三池经同花顺涨停揭秘低频纪律抓取落库（每日每池
                             一次、长退避、封禁探测；当日已采集即跳过），
                             strong_stocks 库内自算（60日新高 ∪ 近10日多次
                             涨停）。--probe 只做封禁探测（单次页面请求，不打
                             dataapi），不落库。
  parity [--date D] [--faces LIST] [--window N] [--limit N] [--no-probe]
                             迁移总验收对拍（issue #122，spec #111）：五核心面
                             （boards/snapshot/indices/lhb/zt_pool）逐面对拍 +
                             库内历史连续性检查，全程不落库（纯读 + 逐源直取 +
                             THS 页面级探测）。复用各面 parity 工具；zt_pool
                             跨源对拍不可行（ADR 0012）以探测 + 连续性为证据；
                             手册见 docs/parity-cli.md。
  stock-list [--check]       股票清单固定入口（issue #122 长尾验收）：默认经
                             fallback 链（akshare 交易所官网 →
                             baostock，efinance 已随 issue #121 退役）
                             抓取按 code 幂等落 stock_list 并落台账
                             （清单链健康巡检/冒烟）；--check 逐源独立抓取互证
                             （行数/代码集合差集/北交所覆盖），不落库；手册见
                             docs/stock-list-cli.md。

退出码：0 = 全部 section/part 成功（含 empty 无数据，非故障）；1 = 任一
section/part 失败或输入非法（失败明细在 stdout JSON）；2 = 参数错误
（argparse）。异常收敛为结构化 JSON 错误输出，不向调用方抛栈。
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Sequence

from .adapters import BoardAdapter, SnapshotAdapter
from .daily_parity import compare_daily_sources
from .dates import normalize_date, today_str
from .db import connect
from .fetch_chain import (
    AllSourcesFailed,
    aggregate_error_kind,
    fetch_stock_list_with_fallback,
)
from .ledger import record_collection, summarize_attempted
from .output import aggregate_batch_status
from .parity_acceptance import run_parity_acceptance
from .repository import select_stock_codes, upsert_stock_list
from .snapshot_parity import compare_snapshot_sources
from .stock_list_parity import compare_stock_list_sources
from .sw_board_parity import compare_sw_board_sources
from .tools_backfill import fetch_sw_boards_backfill
from .tools_board import fetch_board_snapshot
from .tools_daily import fetch_daily
from .tools_lhb import compare_lhb_sources, fetch_lhb_backfill, fetch_lhb_day
from .tools_morning import fetch_morning_batch
from .tools_snapshot import fetch_market_snapshot

CLOSE_SECTIONS = ("indices", "boards", "zt_pool", "strong_stocks", "lhb")


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def cmd_close(date_arg: str | None,
              adapters: Sequence[BoardAdapter] | None = None,
              lhb_adapters: Sequence[BoardAdapter] | None = None) -> int:
    """收盘后批：五 section 盘面快照。任一 section 失败 exit 1。"""
    result = fetch_board_snapshot(
        trade_date=date_arg, sections=list(CLOSE_SECTIONS), adapters=adapters,
        lhb_adapters=lhb_adapters)
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


def cmd_backfill(start: str | None, end: str | None,
                 source: str = "sw") -> int:
    """板块历史回补（申万官网 trend，issue #118/#121 定稿单源）：
    非全 ok（partial_error/error）exit 1。默认窗口
    19990101~前一交易日，历史深至 1999；东财 push2his 回补已随 issue #121
    摘除（--source 仅 sw）。
    """
    if source != "sw":
        _emit({"status": "error", "job": "backfill",
               "error": f"未知 source: {source}（可选: sw；东财 push2his 回补"
                        "已随 issue #121 摘除）"})
        return 1  # 输入非法（退出码定义见模块 docstring：2 仅 argparse 参数错误）
    result = fetch_sw_boards_backfill(start=start, end=end)
    out = {"job": "backfill", "source": source, **result}
    _emit(out)
    return 0 if result.get("status") == "ok" else 1


def cmd_lhb(
    date_arg: str | None = None,
    start_arg: str | None = None,
    end_arg: str | None = None,
    check: bool = False,
    lhb_adapters: Sequence[BoardAdapter] | None = None,
) -> int:
    """龙虎榜固定入口（issue #114）：单日抓取 / 区间回溯 / 新浪对拍。

    - 默认：单日抓取落库（缺省当日；三所盘后发布，盘中空数据是正常语义）；
    - --start/--end：历史回溯（单日失败不中断，逐日独立成败）；
    - --check：新浪互补对拍（不落库）。--date 与 --start/--end 互斥。
    """
    tool = "lhb"
    if date_arg and (start_arg or end_arg):
        _emit({"status": "error", "job": tool,
               "error": "--date 与 --start/--end 互斥"})
        return 1
    if check:
        result = compare_lhb_sources(trade_date=date_arg)
        _emit({"job": tool, "mode": "check", **result})
        return 0 if result.get("status") == "ok" else 1
    if start_arg or end_arg:
        if not (start_arg and end_arg):
            _emit({"status": "error", "job": tool,
                   "error": "--start 与 --end 需成对提供"})
            return 1
        result = fetch_lhb_backfill(start_arg, end_arg, lhb_adapters=lhb_adapters)
        _emit({"job": tool, "mode": "backfill", **result})
        return 0 if result.get("status") == "ok" else 1
    result = fetch_lhb_day(date_arg, lhb_adapters=lhb_adapters)
    _emit({"job": tool, "mode": "fetch", **result})
    lhb = result.get("lhb") or {}
    return 0 if lhb.get("status") == "ok" else 1


def cmd_sw_boards(
    date_arg: str | None = None,
    check: bool = False,
    level: str = "1",
    limit: int | None = None,
    adapters: Sequence[BoardAdapter] | None = None,
) -> int:
    """申万板块固定入口（issue #118）：快照落库 / 三方位对拍。

    默认（fetch）：抓申万双级实时快照落 market_boards（收盘后调用即当日
    收盘截面，收盘后批 boards 段同链——申万官网单源，东财已退出 boards 链）；
    --check：申万官网 vs 库内落库行 vs 库内自算逐板块字段级对拍，不落库
    （差异列于报告非失败；official 抓取失败才 exit 1）。
    """
    tool = "sw-boards"
    lv: int | str = int(level) if str(level).isdigit() else level
    if check:
        result = compare_sw_board_sources(
            trade_date=date_arg, level=lv, limit=limit, adapters=adapters
        )
        _emit({"job": tool, "mode": "check", **result})
        return 0 if result.get("status") == "ok" else 1
    result = fetch_board_snapshot(
        trade_date=date_arg, sections=["boards"], adapters=adapters
    )
    failed = result.get("failed_sections")
    if failed is None:
        _emit({"status": "error", "job": tool,
               "error": result.get("error", "未知错误")})
        return 1
    out = {
        "status": "ok" if not failed else "error",
        "job": tool,
        "mode": "fetch",
        "trade_date": result["trade_date"],
        "boards": result["sections"].get("boards"),
    }
    if failed and result.get("error"):
        out["error"] = result["error"]
        if result.get("error_kind"):
            out["error_kind"] = result["error_kind"]
    _emit(out)
    return 0 if not failed else 1


def cmd_sw_dict(url: str | None = None) -> int:
    """申万成分字典在线刷新（issue #118）：官网分类 xls → 清空重载 'sw'。"""
    from .tools_init import refresh_sw_dict

    result = refresh_sw_dict(url=url)
    _emit({"job": "sw-dict", **result})
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


def cmd_index(
    codes_arg: str | None = None,
    date_arg: str | None = None,
    start_arg: str | None = None,
    end_arg: str | None = None,
    verify: bool = False,
    index_adapters: Sequence | None = None,
) -> int:
    """指数日线固定入口（issue #117）：抓取落库 / 覆盖核验。

    - 默认（fetch）：逐指数走 腾讯→baostock fallback 链抓取 [start, end]
      落库 index_daily（业务键幂等，可重复执行）；用于断档回填（如科创50
      2026-10-13 起缺口）与收盘后当晚 T+0 当日行补采。缺省代码 = 7 大指数。
    - --verify：纯读覆盖核验（对照交易日历列缺口），不写库。
    """
    tool = "index"
    if verify:
        from .tools_index import verify_index_coverage

        result = verify_index_coverage(codes=codes_arg, start=start_arg, end=end_arg)
        _emit({"job": tool, "mode": "verify", **result})
        return 0 if result.get("status") == "ok" else 1
    rng, err = _daily_range(date_arg, start_arg, end_arg)
    if err:
        _emit({"status": "error", "job": tool, "error": err})
        return 1
    assert rng is not None
    start, end = rng
    from .tools_index import fetch_index_backfill

    result = fetch_index_backfill(
        codes=codes_arg, start=start, end=end, adapters=index_adapters
    )
    _emit({"job": tool, "mode": "fetch", **result})
    return 0 if result.get("status") == "ok" else 1


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


def cmd_limitpool(date_arg: str | None, probe: bool = False) -> int:
    """涨停池固定入口（issue #120）：zt_pool 三池 THS 抓取 + strong_stocks
    库内自算；--probe 只做封禁探测（不落库、不触库）。

    生产路径（不传适配器）走 tools_board 的 THS 低频纪律与自算段；抓取前
    的"每日一次"守卫在 tools_board 内（读采集台账）。
    """
    tool = "limitpool"
    if probe:
        from .adapters.ths_adapter import probe as ths_probe

        result = ths_probe()
        _emit({"job": tool, "mode": "probe", **result})
        return 0 if result["reachable"] and not result["banned"] else 1
    result = fetch_board_snapshot(
        trade_date=date_arg, sections=["zt_pool", "strong_stocks"])
    failed = result.get("failed_sections")
    if failed is None:
        _emit({"status": "error", "job": tool,
               "error": result.get("error", "未知错误")})
        return 1
    out = {
        "status": "ok" if not failed else "error",
        "job": tool,
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


def cmd_parity(
    date_arg: str | None = None,
    faces_arg: str | None = None,
    window_days: int = 120,
    limit: int | None = None,
    no_probe: bool = False,
    sw_adapters: Sequence | None = None,
    snapshot_adapters: Sequence[SnapshotAdapter] | None = None,
    index_adapters: Sequence | None = None,
    lhb_exchange=None,
    lhb_sina=None,
) -> int:
    """迁移总验收对拍（issue #122，spec #111）：五核心面逐面对拍 + 库内
    历史连续性，全程不落库（纯读 + 逐源直取 + THS 页面级探测）。

    逐面复用各面 parity 工具（boards=sw_boards / snapshot / indices /
    lhb），zt_pool 跨源对拍不可行（ADR 0012）以探测 + 连续性为证据；
    --no-probe 跳过 THS 封禁探测（离线/网络受限环境）。任一面对拍源失败
    exit 1（差异与 infeasible 为对拍产出，非失败）。
    """
    tool = "parity"
    probe = None
    if not no_probe:
        from .adapters.ths_adapter import probe as ths_probe

        probe = ths_probe
    result = run_parity_acceptance(
        trade_date=date_arg,
        faces=faces_arg,
        window_days=window_days,
        limit=limit,
        probe=probe,
        sw_adapters=sw_adapters,
        snapshot_adapters=snapshot_adapters,
        index_adapters=index_adapters,
        lhb_exchange=lhb_exchange,
        lhb_sina=lhb_sina,
    )
    _emit({"job": tool, **result})
    return 0 if result.get("status") == "ok" else 1


def cmd_stock_list(
    check: bool = False,
    list_adapters: Sequence | None = None,
) -> int:
    """股票清单固定入口（issue #122 长尾验收）：抓取落库 / 多源互证。

    默认（fetch）经清单 fallback 链（akshare 交易所官网 →
    baostock，efinance 已随 issue #121 退役）抓取并按 stock_code 幂等
    upsert stock_list，落采集台账
    （section=stock_list，与 init 清单 part 同语义）——清单链健康巡检与
    长尾冒烟的固定入口；--check 逐源独立抓取互证（行数 + 代码集合差集 +
    北交所覆盖），不落库不写台账。
    """
    tool = "stock-list"
    if check:
        result = compare_stock_list_sources(adapters=list_adapters)
        _emit({"job": tool, "mode": "check", **result})
        return 0 if result.get("status") in ("ok", "mismatch") else 1
    if list_adapters is None:
        from .adapters import default_adapters

        list_adapters = default_adapters()
    today = today_str()
    started = datetime.now(timezone.utc)
    conn, db_err = connect()
    if db_err:
        _emit({"status": "error", "job": tool, "error": db_err})
        return 1
    try:
        try:
            r = fetch_stock_list_with_fallback(list_adapters)
        except AllSourcesFailed as e:
            kind = aggregate_error_kind(e.attempted)
            record_collection(
                conn, trade_date=today, section="stock_list", status="error",
                error_kind=kind, detail=summarize_attempted(e.attempted),
                started_at=started,
            )
            _emit({"status": "error", "job": tool, "mode": "fetch",
                   "error": str(e), "error_kind": kind,
                   "attempted_sources": e.attempted})
            return 1
        n = upsert_stock_list(conn, r["rows"], r["source"])
        record_collection(
            conn, trade_date=today, section="stock_list",
            status="ok" if n > 0 else "empty", source=r["source"], rows=n,
            error_kind=None if n > 0 else "no_data_for_date",
            detail=summarize_attempted(r["attempted_sources"]), started_at=started,
        )
        bse = sum(
            1 for row in r["rows"]
            if str(row.get("stock_code") or "")[:1] in ("4", "8", "9")
        )
        _emit({
            "status": "ok" if n > 0 else "empty",
            "job": tool,
            "mode": "fetch",
            "source": r["source"],
            "rows_upserted": n,
            "bse_rows": bse,
            "attempted_sources": r["attempted_sources"],
        })
        # empty 为上游确认无数据的正常语义（非故障），退出码 0
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None,
         adapters: Sequence[BoardAdapter] | None = None,
         daily_adapters: Sequence | None = None,
         fundamentals_adapters: Sequence | None = None,
         snapshot_adapters: Sequence[SnapshotAdapter] | None = None,
         index_adapters: Sequence | None = None,
         lhb_adapters: Sequence[BoardAdapter] | None = None,
         list_adapters: Sequence | None = None) -> int:
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
    p_backfill = sub.add_parser(
        "backfill", help="板块历史回补：申万官网 trend（issue #118/#121 定稿单源）")
    p_backfill.add_argument("--start", default=None,
                            help="回补起点 yyyymmdd；缺省 19990101")
    p_backfill.add_argument("--end", default=None,
                            help="回补终点 yyyymmdd，缺省前一交易日")
    p_backfill.add_argument("--source", default="sw", choices=("sw",),
                            help="回补源：sw=申万官网 trend（issue #118）；"
                                 "东财 push2his 回补已随 issue #121 摘除")
    p_sw_boards = sub.add_parser(
        "sw-boards", help="申万板块：双级实时快照落库 / 三方位对拍（--check）")
    p_sw_boards.add_argument("--date", default=None,
                             help="交易日 yyyymmdd，缺省当日（收盘后调用即当日收盘截面）")
    p_sw_boards.add_argument("--check", action="store_true",
                             help="对拍模式：申万官网 vs 库内落库行 vs 库内自算，不落库")
    p_sw_boards.add_argument("--level", default="1", choices=("1", "2", "all"),
                             help="对拍层级（--check 时生效）：1=一级 / 2=二级 / all")
    p_sw_boards.add_argument("--limit", type=int, default=None,
                             help="只对拍前 N 个板块（按官网清单序，冒烟用；抓取模式忽略）")
    p_sw_dict = sub.add_parser(
        "sw-dict", help="申万成分字典在线刷新：官网分类 xls 清空重载 'sw' 体系")
    p_sw_dict.add_argument("--url", default=None,
                           help="分类 xls 直链，缺省申万官网 SwClass2021 文件")
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
    p_index = sub.add_parser(
        "index",
        help="指数日线：腾讯→baostock 抓取落库（回填/T+0）/ --verify 覆盖核验")
    p_index.add_argument("--codes", default=None,
                         help="逗号分隔指数代码，缺省 7 大指数白名单")
    p_index.add_argument("--date", default=None,
                         help="单日 yyyymmdd，缺省当日")
    p_index.add_argument("--start", default=None,
                         help="区间起点 yyyymmdd（与 --end 配对）")
    p_index.add_argument("--end", default=None,
                         help="区间终点 yyyymmdd（与 --start 配对）")
    p_index.add_argument("--verify", action="store_true",
                         help="覆盖核验：对照交易日历列缺口，不写库")
    p_lhb = sub.add_parser(
        "lhb", help="龙虎榜：沪深北交易所官方源抓取落库 / 历史回溯 / 新浪对拍")
    p_lhb.add_argument("--date", default=None,
                       help="单日 yyyymmdd，缺省当日（对拍模式同样适用）")
    p_lhb.add_argument("--start", default=None,
                       help="回溯起点 yyyymmdd（与 --end 配对，历史回溯模式）")
    p_lhb.add_argument("--end", default=None,
                       help="回溯终点 yyyymmdd（与 --start 配对）")
    p_lhb.add_argument("--check", action="store_true",
                       help="对拍模式：交易所官方 vs 新浪互补校验，不落库")
    p_pool = sub.add_parser(
        "limitpool",
        help="涨停池：zt_pool 三池 THS 低频抓取 + strong_stocks 库内自算")
    p_pool.add_argument("--date", default=None,
                        help="交易日 yyyymmdd，缺省当日")
    p_pool.add_argument("--probe", action="store_true",
                        help="只做封禁探测（单次页面请求，不打 dataapi），不落库")
    p_parity = sub.add_parser(
        "parity",
        help="迁移总验收对拍（issue #122）：五核心面逐面对拍 + 库内历史连续性，不落库")
    p_parity.add_argument("--date", default=None,
                          help="对拍交易日 yyyymmdd，缺省当日"
                               "（非交易日上游数据停留最近交易日）")
    p_parity.add_argument("--faces", default=None,
                          help="逗号分隔面集，缺省 all = "
                               "boards,snapshot,indices,lhb,zt_pool")
    p_parity.add_argument("--window", type=int, default=120,
                          help="连续性检查窗口自然日（缺省 120）")
    p_parity.add_argument("--limit", type=int, default=None,
                          help="快照/板块对拍抽样只数（冒烟用）")
    p_parity.add_argument("--no-probe", action="store_true",
                          help="跳过 THS 封禁探测（离线/网络受限环境）")
    p_slist = sub.add_parser(
        "stock-list",
        help="股票清单：fallback 链抓取落库（巡检/冒烟）/ --check 多源互证")
    p_slist.add_argument("--check", action="store_true",
                         help="互证模式：逐源独立抓取比对（行数/代码集/北交所覆盖），不落库")
    args = ap.parse_args(argv)  # 参数错误由 argparse 以退出码 2 终止
    # stdout 留给最终结构化 JSON；进度/日志走 stderr（与 MCP 侧"日志只走
    # stderr"同契约）
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        if args.command == "close":
            return cmd_close(args.date, adapters=adapters,
                             lhb_adapters=lhb_adapters)
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
        if args.command == "lhb":
            return cmd_lhb(
                date_arg=args.date,
                start_arg=args.start,
                end_arg=args.end,
                check=args.check,
                lhb_adapters=lhb_adapters,
            )
        if args.command == "backfill":
            return cmd_backfill(args.start, args.end, source=args.source)
        if args.command == "sw-boards":
            return cmd_sw_boards(
                date_arg=args.date,
                check=args.check,
                level=args.level,
                limit=args.limit,
                adapters=adapters,
            )
        if args.command == "sw-dict":
            return cmd_sw_dict(url=args.url)
        if args.command == "fundamentals":
            return cmd_fundamentals(
                codes_arg=args.codes,
                fundamentals_adapters=fundamentals_adapters,
            )
        if args.command == "index":
            return cmd_index(
                codes_arg=args.codes,
                date_arg=args.date,
                start_arg=args.start,
                end_arg=args.end,
                verify=args.verify,
                index_adapters=index_adapters,
            )
        if args.command == "limitpool":
            return cmd_limitpool(args.date, probe=args.probe)
        if args.command == "parity":
            return cmd_parity(
                date_arg=args.date,
                faces_arg=args.faces,
                window_days=args.window,
                limit=args.limit,
                no_probe=args.no_probe,
                sw_adapters=adapters,
                snapshot_adapters=snapshot_adapters,
                index_adapters=index_adapters,
                lhb_exchange=None,
                lhb_sina=None,
            )
        if args.command == "stock-list":
            return cmd_stock_list(check=args.check, list_adapters=list_adapters)
    except Exception as e:  # 零例外：异常收敛为结构化错误输出
        _emit({"status": "error", "job": args.command,
               "error": f"{type(e).__name__}: {e}"})
        return 1
    _emit({"status": "error", "error": f"未知子命令: {args.command!r}"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
