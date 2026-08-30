"""fetch/query 能力面核心：fetch_board_snapshot 盘面快照落库，query_board_data 库内查询。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake 适配器 +
真实 PG 测试库（见 tests/）。盘面快照契约（issue #5）：按 section
（indices/boards/zt_pool/strong_stocks/lhb）独立 fallback 重试、独立成败报告，
单 section 失败不拖垮其他，仅全部失败才整体 error；盘中 lhb_basic 为空
返回 rows:0 + note（龙虎榜盘后发布是正常语义），不算失败；各表按业务键
upsert，重复执行幂等。
"""

import logging
from datetime import date
from typing import Sequence

from .adapters import BoardAdapter, default_adapters
from .dates import normalize_date
from .db import connect
from .fetch_chain import AllSourcesFailed, fetch_section_with_fallback
from .output import error as _error
from .repository import (
    BOARD_QUERY_TABLES,
    BOARD_SECTION_TABLES,
    select_board_rows,
    select_latest_board_date,
    upsert_board_rows,
)

log = logging.getLogger(__name__)

ALL_SECTIONS = ("indices", "boards", "zt_pool", "strong_stocks", "lhb")

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
                results[name] = {"rows": 0, "status": "error", "error": str(e)}
                continue
            res = {
                "rows": _upsert_section(conn, name, r["data"], effective),
                "source": r["source"],
                "status": "ok",
            }
            if name == "lhb":
                if r["data"].get("errors"):
                    # 子项部分失败：拿到什么落什么，但失败原因必须报告
                    res["partial_error"] = "; ".join(r["data"]["errors"])
                if not r["data"]["lhb_basic"]:
                    res["note"] = _LHB_EMPTY_NOTE
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
    if len(failed) == len(results):
        return {
            "status": "error",
            "error": f"全部 section 抓取失败: {', '.join(failed)}",
            **payload,
        }
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
