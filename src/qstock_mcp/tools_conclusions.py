"""结论存储能力面核心：save_conclusion 写入（同键 upsert），query_conclusions 过滤查询。

工具函数层（server.py）只做薄包装；本层是测试接缝：真实 PG 测试库（见 tests/）。
契约（docs/adr/0003，issue #7）：业务唯一键 (subject_type, subject_code,
trade_date, conclusion_type)；payload 为任意 JSON，结构由写入方 skill 约定，
server 不校验。
"""

import json
import logging
from typing import Any

from .db import connect
from .dates import normalize_date
from .output import error as _error
from .repository import select_conclusions, upsert_conclusion

log = logging.getLogger(__name__)


def save_conclusion(
    subject_type: str,
    subject_code: str,
    trade_date: str,
    conclusion_type: str,
    payload: Any,  # 任意 JSON 值，结构由写入方 skill 约定
) -> dict:
    """写入一条分析结论：同业务键重复写入为 upsert 语义，outcome 报告 inserted/updated。"""
    tool = "save_conclusion"
    params = {
        "subject_type": subject_type,
        "subject_code": subject_code,
        "trade_date": trade_date,
        "conclusion_type": conclusion_type,
    }
    try:
        normalized = normalize_date(trade_date)
    except ValueError as e:
        return _error(tool, params, str(e))
    try:
        json.dumps(payload)  # payload 必须是可 JSON 序列化的值
    except (TypeError, ValueError) as e:
        return _error(tool, params, f"payload 不是可 JSON 序列化的值: {e}")
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        outcome = upsert_conclusion(
            conn, subject_type, subject_code, normalized, conclusion_type, payload
        )
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "trade_date": normalized,
        "outcome": outcome,
    }


def query_conclusions(
    subject_type: str | None = None,
    subject_code: str | None = None,
    trade_date: str | None = None,
    conclusion_type: str | None = None,
) -> dict:
    """库内结论查询：按主体/日期/结论类型过滤，全部缺省返回全表。"""
    tool = "query_conclusions"
    params = {
        "subject_type": subject_type,
        "subject_code": subject_code,
        "trade_date": trade_date,
        "conclusion_type": conclusion_type,
    }
    if trade_date is not None:
        try:
            trade_date = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        rows = select_conclusions(conn, subject_type, subject_code, trade_date, conclusion_type)
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "count": len(rows),
        "rows": rows,
    }
