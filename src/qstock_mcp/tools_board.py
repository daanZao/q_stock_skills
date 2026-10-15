"""fetch/query 能力面核心：fetch_board_snapshot 盘面快照落库，query_board_data 库内查询。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake 适配器 +
真实 PG 测试库（见 tests/）。盘面快照契约（issue #5）：按 section
（indices/boards/zt_pool/strong_stocks/lhb）独立 fallback 重试、独立成败报告，
各表按业务键 upsert，重复执行幂等。核心 section fail-fast（issue #47，
ADR 0007）：CORE_SECTIONS 显式声明核心 section 集，任一核心 section 因
upstream_unreachable（连接级故障）失败 → 工具整体 error，响应携带
per-section 明细（成败/已落库行数/error_kind/逐源 attempted_sources）；
fetch_failed（非连接级）与 no_data_for_date（上游正常响应空结果）不触发。
中途失败不回滚：per-section 独立 commit，已落库 section 保留，
调用方可用 sections 参数只重试失败分段（幂等）。lhb 永不进核心集：
盘中 lhb_basic 为空返回 rows:0 + note + empty_kind=no_data_for_date
（龙虎榜盘后发布是正常语义），lhb 抓取失败按三态结构化返回
（status error + error_kind + 逐源明细），与"未发布"可区分；
lhb 子项部分失败落已得数据并报 partial_error。

indices 段白名单定点补漏（issue #49）：主源 efinance「沪深系列指数」口径系统性
排除科创板系列指数（缺 000688），备选 akshare 含 000688 但主源成功时轮不到。
indices 段抓取后对照 MAJOR_INDICES 白名单（复用 tools_init 清单，单一事实来源）
检查覆盖，缺谁按单 code 补抓补落库（fetch_index_spot_with_fallback）。补漏失败
不静默也不触发核心 section fail-fast——section 维持 ok，失败按三态分类进
patch failed 明细（复用 ADR 0007 命名：upstream_unreachable 带 retry_advice /
fetch_failed / 全源口径不含该指数 no_data_for_date）。
"""

import logging
from datetime import date
from typing import Sequence

from .adapters import BoardAdapter, default_adapters
from .dates import normalize_date
from .db import connect
from .fetch_chain import (
    UPSTREAM_RETRY_ADVICE,
    AllSourcesFailed,
    aggregate_error_kind,
    aggregate_patch_error_kind,
    fetch_index_spot_with_fallback,
    fetch_section_with_fallback,
)
from .output import error as _error
from .repository import (
    BOARD_QUERY_TABLES,
    BOARD_SECTION_TABLES,
    select_board_rows,
    select_latest_board_date,
    upsert_board_rows,
)
from .tools_init import MAJOR_INDICES

log = logging.getLogger(__name__)

ALL_SECTIONS = ("indices", "boards", "zt_pool", "strong_stocks", "lhb")

# 核心 section 集（issue #47）：任一核心 section 因 upstream_unreachable 失败
# 即整体 error。lhb 永不进核心集（盘中"未发布"是常态）；新增 section 默认非核心，
# 入核心集需显式更新本常量。
CORE_SECTIONS = ("indices", "boards", "zt_pool", "strong_stocks")

_LHB_EMPTY_NOTE = "当日无龙虎榜数据（龙虎榜盘后发布）"


def _parse_sections(sections) -> list[str]:
    """None → 全部；CSV 字符串或列表 → 子集。未知 section 抛 ValueError。"""
    if sections is None:
        return list(ALL_SECTIONS)
    if isinstance(sections, str):
        selected = [s.strip() for s in sections.split(",") if s.strip()]
    else:
        selected = list(sections)
    unknown = [s for s in selected if s not in ALL_SECTIONS]
    if unknown:
        raise ValueError(f"未知 section: {unknown}（可选: {', '.join(ALL_SECTIONS)}）")
    if not selected:
        raise ValueError(f"sections 为空（可选: {', '.join(ALL_SECTIONS)}）")
    return selected


def _upsert_section(conn, section: str, data, trade_date: str) -> int:
    """把一个 section 的抓取结果落库，返回总行数。"""
    total = 0
    for table, date_col, key_cols, fields in BOARD_SECTION_TABLES[section]:
        rows = data[table] if section == "lhb" else data
        total += upsert_board_rows(
            conn, table, date_col, key_cols, fields, trade_date, rows
        )
    return total


def _patch_major_indices(
    adapters: Sequence[BoardAdapter], data: list[dict]
) -> tuple[list[dict], dict | None]:
    """indices 段白名单定点补漏（issue #49）：对照 MAJOR_INDICES 检查覆盖，
    缺者按单 code 补抓，返回 (含补抓行的行列表, patch 报告)。

    白名单全命中返回 (原数据, None)——零补抓请求。补抓失败不抛错（不拖垮
    section、不触发 fail-fast），按三态分类记入报告 failed 明细并 log；
    全源口径不含该指数（empty）归 no_data_for_date——正常语义，但覆盖目标
    未达成，同样列入 failed 明细。
    """
    present = {row.get("index_code") for row in data}
    missing = [code for code, _ in MAJOR_INDICES if code not in present]
    if not missing:
        return data, None
    rows = list(data)
    patched = []
    failed = {}
    for code in missing:
        try:
            r = fetch_index_spot_with_fallback(adapters, code)
        except AllSourcesFailed as e:
            kind = aggregate_patch_error_kind(e.attempted)
            entry = {
                "error": str(e),
                "error_kind": kind,
                "attempted_sources": e.attempted,
            }
            if kind == "upstream_unreachable":
                entry["retry_advice"] = UPSTREAM_RETRY_ADVICE
            failed[code] = entry
            log.warning("indices 段白名单补漏失败 %s: %s", code, e)
            continue
        rows += r["rows"]
        patched.append({"index_code": code, "source": r["source"]})
    report: dict = {"patched": patched}
    if failed:
        report["failed"] = failed
    return rows, report


def fetch_board_snapshot(
    trade_date: str | None = None,
    sections: str | Sequence[str] | None = None,
    adapters: Sequence[BoardAdapter] | None = None,
) -> dict:
    """抓取盘面快照并落库：各 section 独立 fallback 重试、独立成败报告。"""
    tool = "fetch_board_snapshot"
    params = {"trade_date": trade_date, "sections": sections}
    if trade_date is None:
        effective = date.today().strftime("%Y%m%d")
    else:
        try:
            effective = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    try:
        selected = _parse_sections(sections)
    except ValueError as e:
        return _error(tool, params, str(e))
    if adapters is None:
        adapters = default_adapters()
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    results = {}
    try:
        for name in selected:
            try:
                r = fetch_section_with_fallback(adapters, name, effective)
            except AllSourcesFailed as e:
                kind = aggregate_error_kind(e.attempted)
                entry = {
                    "rows": 0,
                    "status": "error",
                    "error": str(e),
                    "error_kind": kind,
                    "attempted_sources": e.attempted,
                }
                if kind == "upstream_unreachable":
                    entry["retry_advice"] = UPSTREAM_RETRY_ADVICE
                results[name] = entry
                continue
            data = r["data"]
            patch = None
            if name == "indices":
                data, patch = _patch_major_indices(adapters, data)
            res = {
                "rows": _upsert_section(conn, name, data, effective),
                "source": r["source"],
                "status": "ok",
            }
            if patch:
                res["patch"] = patch
            if r["attempted_sources"]:
                res["attempted_sources"] = r["attempted_sources"]
            if name == "lhb":
                if r["data"].get("errors"):
                    # 子项部分失败：拿到什么落什么，但失败原因必须报告
                    res["partial_error"] = "; ".join(r["data"]["errors"])
                if not r["data"]["lhb_basic"]:
                    res["empty_kind"] = "no_data_for_date"
                    res["note"] = _LHB_EMPTY_NOTE
            elif not data:
                res["empty_kind"] = "no_data_for_date"
            results[name] = res
    finally:
        conn.close()
    failed = [k for k, v in results.items() if v["status"] != "ok"]
    payload = {
        "tool": tool,
        "params": params,
        "trade_date": effective,
        "sections": results,
        "failed_sections": failed,
    }
    unreachable_core = [
        k
        for k in failed
        if k in CORE_SECTIONS
        and results[k].get("error_kind") == "upstream_unreachable"
    ]
    if unreachable_core:
        return {
            "status": "error",
            "error": f"核心 section 上游不可达: {', '.join(unreachable_core)}",
            "error_kind": "upstream_unreachable",
            "retry_advice": UPSTREAM_RETRY_ADVICE,
            **payload,
        }
    if len(failed) == len(results):
        kind = (
            "upstream_unreachable"
            if any(
                results[k].get("error_kind") == "upstream_unreachable" for k in failed
            )
            else "fetch_failed"
        )
        out = {
            "status": "error",
            "error": f"全部 section 抓取失败: {', '.join(failed)}",
            "error_kind": kind,
            **payload,
        }
        if kind == "upstream_unreachable":
            out["retry_advice"] = UPSTREAM_RETRY_ADVICE
        return out
    return {"status": "ok", **payload}


def query_board_data(
    table: str,
    trade_date: str | None = None,
    code: str | None = None,
) -> dict:
    """库内盘面数据查询：按表/日期/代码过滤；日期缺省取该表库内最新日期。

    lhb_yyb_* 两表的日期列为 fetch_date（抓取日语义），参数仍叫 trade_date。
    """
    tool = "query_board_data"
    params = {"table": table, "trade_date": trade_date, "code": code}
    if table not in BOARD_QUERY_TABLES:
        return _error(
            tool,
            params,
            f"未知表: {table}（可选: {', '.join(BOARD_QUERY_TABLES)}）",
        )
    if trade_date is not None:
        try:
            trade_date = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    date_col, code_col = BOARD_QUERY_TABLES[table]
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        if trade_date is None:
            trade_date = select_latest_board_date(conn, table, date_col)
        rows = (
            select_board_rows(conn, table, date_col, code_col, trade_date, code)
            if trade_date is not None
            else []
        )
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "table": table,
        "trade_date": trade_date,
        "count": len(rows),
        "rows": rows,
    }
