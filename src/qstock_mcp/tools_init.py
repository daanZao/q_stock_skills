"""init 能力面：init_database 工具的核心逻辑（tool 函数层的薄包装之下）。

轻量初始化（issue #8）= 幂等建表 + 股票清单 + 全市场快照 + 主要指数日线，
各部分独立 fallback 重试、独立成败报告（单部分失败不拖垮其他，仅全部数据
部分失败才整体 error）；backfill_history=True 时追加全市场个股历史日线回溯
（重操作，显式开启，复用日线头尾自愈，单股失败不中断，进度/失败在
parts.backfill 中报告）。输出自描述 JSON（dict）：status / tool / params /
schema_files / tables / parts / failed_parts。

query_index_daily（issue #33/T1）：index_daily 已沉淀数据的纯读查询，不触发
补抓；未知指数代码、坏日期、非法 limit 走统一 error 契约，空结果 count:0
非错误。

import_board_classification（issue #73，spec #70）：成分字典显式导入入口，
以文件为准清空重载某 source_system（事务内删除后重载，陈旧行不残留），
本期仅支持申万 .xls（解析在 _sw_classify，纯函数不连库）。

import_sw_industry_meta（issue #74）：申万行业名称表（sw_industry_meta）
种子导入，仓库内置 CSV 种子幂等 upsert，只动 meta 不碰成分变更史。
"""

import logging
from datetime import date
from typing import Sequence

from ._sw_classify import (
    ClassificationFileError,
    load_sw_industry_meta_seed,
    parse_sw_classification,
)
from .adapters import DataAdapter, default_adapters
from .dates import normalize_date
from .db import connect, ensure_schema, list_tables
from .fetch_chain import (
    AllSourcesFailed,
    fetch_index_daily_with_fallback,
    fetch_snapshot_with_fallback,
    fetch_stock_list_with_fallback,
)
from .output import error as _error
from .repository import (
    replace_board_classification,
    select_index_daily,
    select_stock_codes,
    upsert_index_daily,
    upsert_snapshot,
    upsert_stock_list,
    upsert_sw_industry_meta,
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


# ------------------------------------------------- query_index_daily（issue #33/T1）

_INDEX_NAMES = dict(MAJOR_INDICES)

# 缺省返回最近 120 根日线（约半年交易日）；start/end 给定时 limit 仍生效
DEFAULT_INDEX_DAILY_LIMIT = 120


def _normalize_or_error(tool: str, params: dict, value: str) -> tuple[str | None, dict | None]:
    """可选日期参数归一化；非法时返回 (None, error_dict)。"""
    try:
        return normalize_date(value), None
    except ValueError as e:
        return None, _error(tool, params, str(e))


def query_index_daily(
    index_code: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = DEFAULT_INDEX_DAILY_LIMIT,
) -> dict:
    """库内主要指数日线查询（纯读，不补抓）：按日期范围过滤，升序返回最近
    limit 行；空结果 count:0 非错误。"""
    tool = "query_index_daily"
    params = {
        "index_code": index_code,
        "start": start,
        "end": end,
        "limit": limit,
    }
    if index_code not in _INDEX_NAMES:
        options = ", ".join(f"{c}({n})" for c, n in MAJOR_INDICES)
        return _error(tool, params, f"未知指数代码: {index_code}（可选: {options}）")
    if start is not None:
        start, date_err = _normalize_or_error(tool, params, start)
        if date_err:
            return date_err
    if end is not None:
        end, date_err = _normalize_or_error(tool, params, end)
        if date_err:
            return date_err
    if start is not None and end is not None and start > end:
        return _error(tool, params, f"start({start}) 晚于 end({end})")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return _error(tool, params, f"limit 必须为正整数: {limit!r}")
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        rows = select_index_daily(conn, index_code, start, end, limit)
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "index_code": index_code,
        "index_name": _INDEX_NAMES[index_code],
        "range": {"start": start, "end": end},
        "count": len(rows),
        "rows": rows,
    }


# ------------------------------------------------- import_board_classification（issue #73）


def _ingest_ledger_hook(
    conn, source_system: str, file_path: str, counts: dict
) -> None:
    """采集台账挂接点：台账表由 issue #71 落地后在此记录导入批次。"""


def import_board_classification(source_system: str, file_path: str) -> dict:
    """以文件为准清空重载成分字典：事务内删除该 source_system 全部成分行后
    按文件重载（文件含全历史变更，陈旧行不残留），重复执行幂等。
    本期仅支持 'sw'（申万），多体系化为东财概念预留。"""
    tool = "import_board_classification"
    params = {"source_system": source_system, "file_path": file_path}
    if source_system != "sw":
        return _error(
            tool,
            params,
            f"暂不支持的 source_system: {source_system!r}（本期仅 'sw'，东财概念预留）",
        )
    try:
        rows = parse_sw_classification(file_path)
    except ClassificationFileError as e:
        return _error(tool, params, str(e))
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        try:
            ensure_schema(conn)
            counts = replace_board_classification(conn, source_system, rows)
            _ingest_ledger_hook(conn, source_system, file_path, counts)
            conn.commit()
        except Exception as e:  # noqa: BLE001 - 工具层把异常收敛为自描述错误
            conn.rollback()
            log.exception("成分字典导入失败")
            return _error(tool, params, f"成分字典导入失败: {e}")
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "stocks": len({r["stock_code"] for r in rows}),
        **counts,
    }


# ------------------------------------------------- import_sw_industry_meta（issue #74）


def import_sw_industry_meta() -> dict:
    """申万行业名称表（sw_industry_meta）种子导入：仓库内置 CSV 种子按
    industry_code 幂等 upsert，重复执行行数不变。只动 meta，不碰成分
    变更史；名称修正即改种子 CSV 后重导。"""
    tool = "import_sw_industry_meta"
    params: dict = {}
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        try:
            ensure_schema(conn)
            rows = upsert_sw_industry_meta(conn, load_sw_industry_meta_seed())
        except Exception as e:  # noqa: BLE001 - 工具层把异常收敛为自描述错误
            # upsert_sw_industry_meta 内部自 commit；此 rollback 仅清理失败语句的
            # 事务残余，成功路径的已提交行不受影响（空转 rollback 是有意为之）
            conn.rollback()
            log.exception("行业名称表种子导入失败")
            return _error(tool, params, f"行业名称表种子导入失败: {e}")
    finally:
        conn.close()
    return {"status": "ok", "tool": tool, "params": params, "rows": rows}
