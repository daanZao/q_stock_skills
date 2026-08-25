"""init 能力面：init_database 工具的核心逻辑（tool 函数层的薄包装之下）。

输出自描述 JSON（dict）：status / tool / tables；失败时 status:"error" + 明确原因。
"""

import logging

import psycopg2

from .db import MissingDsnError, ensure_schema, get_dsn, list_tables

log = logging.getLogger(__name__)


def init_database() -> dict:
    """建表初始化。本期只建 schema；轻量初始化数据属后续票。"""
    try:
        dsn = get_dsn()
    except MissingDsnError as e:
        return {"status": "error", "tool": "init_database", "error": str(e)}

    try:
        conn = psycopg2.connect(dsn)
        try:
            applied = ensure_schema(conn)
            tables = list_tables(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - 工具层把异常收敛为自描述错误
        log.exception("init_database 失败")
        return {
            "status": "error",
            "tool": "init_database",
            "error": f"数据库连接或建表失败: {e}",
        }

    return {
        "status": "ok",
        "tool": "init_database",
        "schema_files": applied,
        "tables": tables,
    }
