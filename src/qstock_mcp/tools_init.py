"""init 能力面：init_database 工具的核心逻辑（tool 函数层的薄包装之下）。

轻量初始化（issue #8）= 幂等建表 + 股票清单 + 全市场快照 + 主要指数日线，
各部分独立 fallback 重试、独立成败报告（单部分失败不拖垮其他，仅全部数据
部分失败才整体 error）；backfill_history=True 时追加全市场个股历史日线回溯
（重操作，显式开启，复用日线头尾自愈，单股失败不中断，进度/失败在
parts.backfill 中报告）。输出自描述 JSON（dict）：status / tool / params /
schema_files / tables / parts / failed_parts。
"""

import logging
from datetime import date
from typing import Sequence

from .adapters import DataAdapter, default_adapters
from .db import connect, ensure_schema, list_tables
from .fetch_chain import (
    AllSourcesFailed,
    fetch_index_daily_with_fallback,
    fetch_snapshot_with_fallback,
    fetch_stock_list_with_fallback,
)
from .repository import (
    select_stock_codes,
    upsert_index_daily,
    upsert_snapshot,
    upsert_stock_list,
)
from .tools_daily import ensure_coverage

log = logging.getLogger(__name__)

# 主要指数（init 轻量初始化第三部分）
MAJOR_INDICES = (
    ("000001", "上证指数"),
    ("399001", "深证成指"),
    ("399006", "创业板指"),
    ("000300", "沪深300"),
    ("000016", "上证50"),
    ("000905", "中证500"),
    ("000688", "科创50"),
)

FULL_HISTORY_START = "19900101"  # 全量回溯/指数日线起点，各源自行截断到上市日


def _init_stock_list(conn, adapters: Sequence[DataAdapter]) -> dict:
    try:
        r = fetch_stock_list_with_fallback(adapters)
    except AllSourcesFailed as e:
        return {"status": "error", "error": str(e), "attempted_sources": e.attempted}
    return {
        "status": "ok",
        "rows": upsert_stock_list(conn, r["rows"], r["source"]),
        "source": r["source"],
    }


def _init_market_snapshot(conn, adapters: Sequence[DataAdapter]) -> dict:
    try:
        r = fetch_snapshot_with_fallback(adapters)
    except AllSourcesFailed as e:
        return {"status": "error", "error": str(e), "attempted_sources": e.attempted}
    # API 报告的交易日优先，未报告回退当天（同 fetch_market_snapshot 工具契约）
    effective = r["trade_date"] or date.today().strftime("%Y%m%d")
    return {
        "status": "ok",
        "rows": upsert_snapshot(conn, effective, r["rows"], r["source"]),
        "source": r["source"],
        "trade_date": effective,
    }


def _init_index_daily(conn, adapters: Sequence[DataAdapter]) -> dict:
    """主要指数全历史日线：按指数独立 fallback，仅全部失败才 part error。"""
    today = date.today().strftime("%Y%m%d")
    indices: list[dict] = []
    for code, name in MAJOR_INDICES:
        try:
            r = fetch_index_daily_with_fallback(
                adapters, code, FULL_HISTORY_START, today
            )
        except AllSourcesFailed as e:
            indices.append(
                {
                    "index_code": code,
                    "index_name": name,
                    "status": "error",
                    "error": str(e),
                }
            )
            continue
        indices.append(
            {
                "index_code": code,
                "index_name": name,
                "status": "ok",
                "rows": upsert_index_daily(conn, code, r["rows"], r["source"]),
                "source": r["source"],
            }
        )
    failed = [i for i in indices if i["status"] != "ok"]
    return {
        "status": "error" if len(failed) == len(indices) else "ok",
        "rows": sum(i.get("rows", 0) for i in indices),
        "indices": indices,
    }


def _init_backfill(conn, adapters: Sequence[DataAdapter]) -> dict:
    """全市场个股历史日线回溯：复用日线头尾自愈，单股失败不中断。"""
    today = date.today().strftime("%Y%m%d")
    codes = select_stock_codes(conn)
    if not codes:
        return {
            "status": "error",
            "error": "股票清单为空，无可回溯标的",
            "total": 0,
            "succeeded": 0,
            "rows_upserted": 0,
            "failed": [],
        }
    succeeded, rows_upserted, failed = 0, 0, []
    for code in codes:
        try:
            cover = ensure_coverage(
                conn, adapters, code, "qfq", FULL_HISTORY_START, today
            )
        except Exception as e:  # noqa: BLE001 - 单股失败不中断全市场回溯
            log.warning("全量回溯失败 %s: %s", code, e)
            failed.append({"stock_code": code, "error": str(e)})
            continue
        if not cover["ok"]:
            seg = cover["failed_segment"]
            log.warning("全量回溯分段失败 %s %s~%s", code, seg["start"], seg["end"])
            failed.append(
                {
                    "stock_code": code,
                    "error": f"分段补抓失败（{seg['start']}~{seg['end']}）",
                    "attempted_sources": cover["attempted_sources"],
                }
            )
            continue
        succeeded += 1
        rows_upserted += sum(s["rows"] for s in cover["segments"])
    if not failed:
        status = "ok"
    elif succeeded == 0:
        status = "error"
    else:
        status = "partial_error"
    return {
        "status": status,
        "total": len(codes),
        "succeeded": succeeded,
        "rows_upserted": rows_upserted,
        "failed": failed,
    }


def init_database(
    backfill_history: bool = False,
    adapters: Sequence[DataAdapter] | None = None,
) -> dict:
    """建表 + 轻量初始化数据；backfill_history=True 追加全市场历史日线回溯。"""
    tool = "init_database"
    params = {"backfill_history": backfill_history}
    if adapters is None:
        adapters = default_adapters()
    conn, err = connect()
    if err:
        return {"status": "error", "tool": tool, "params": params, "error": err}
    try:
        try:
            applied = ensure_schema(conn)
            tables = list_tables(conn)
        except Exception as e:  # noqa: BLE001 - 建表是硬前置，失败收敛为自描述错误
            log.exception("init_database 建表失败")
            return {
                "status": "error",
                "tool": tool,
                "params": params,
                "error": f"数据库建表失败: {e}",
            }
        parts = {
            "stock_list": _init_stock_list(conn, adapters),
            "market_snapshot": _init_market_snapshot(conn, adapters),
            "index_daily": _init_index_daily(conn, adapters),
        }
        if backfill_history:
            parts["backfill"] = _init_backfill(conn, adapters)
    finally:
        conn.close()

    failed_parts = [k for k, v in parts.items() if v["status"] != "ok"]
    payload = {
        "tool": tool,
        "params": params,
        "schema_files": applied,
        "tables": tables,
        "parts": parts,
    }
    if failed_parts:
        payload["failed_parts"] = failed_parts
    if len(failed_parts) == len(parts):
        return {
            "status": "error",
            "error": f"全部数据部分初始化失败: {', '.join(failed_parts)}",
            **payload,
        }
    status = "partial_error" if failed_parts else "ok"
    return {"status": status, **payload}
