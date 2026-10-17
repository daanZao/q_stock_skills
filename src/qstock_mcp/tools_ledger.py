"""统一查询能力面核心：query_collection_log 采集台账查询（issue #77，spec #71/W4）。

工具函数层（server.py）只做薄包装；本层是测试接缝（真实 PG 测试库，见
tests/test_collection_log.py）。台账为 append-only 审计表（sql/013），
查询按日期/section/状态过滤，空结果 count:0 非错误，行数上限约束输出体量。
"""

from .dates import normalize_date
from .db import connect
from .ledger import (
    DEFAULT_LOG_LIMIT,
    LEDGER_STATUSES,
    MAX_LOG_LIMIT,
    select_collection_log,
)
from .output import error as _error


def query_collection_log(
    trade_date: str | None = None,
    section: str | None = None,
    status: str | None = None,
    limit: int = DEFAULT_LOG_LIMIT,
) -> dict:
    """采集台账查询：按日期/section/状态过滤，按完成时间倒序返回。

    trade_date: yyyymmdd 或 yyyy-mm-dd；status: ok/empty/error（ADR 0007
    三态同词汇）；limit 缺省 100、上限 500（超出截断而非报错，约束输出体量）。
    空结果 count:0 非错误。
    """
    tool = "query_collection_log"
    params = {
        "trade_date": trade_date,
        "section": section,
        "status": status,
        "limit": limit,
    }
    if trade_date is not None:
        try:
            trade_date = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    if status is not None and status not in LEDGER_STATUSES:
        return _error(
            tool, params, f"未知 status: {status!r}（可选: {', '.join(LEDGER_STATUSES)}）"
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return _error(tool, params, f"limit 必须为正整数: {limit!r}")
    limit = min(limit, MAX_LOG_LIMIT)
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        rows = select_collection_log(conn, trade_date, section, status, limit)
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "count": len(rows),
        "rows": rows,
    }
