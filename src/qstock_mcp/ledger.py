"""采集台账（issue #77，spec #71/W4）：一切写库动作的审计记录。

record_collection 由 tools_* 各写库路径调用：每次采集动作落一行
（sql/013_collection_log.sql，append-only 全历史保留）。台账写入失败只记
log、绝不拖垮主流程（台账是审计设施，不是业务前置）。

status 词表 = 采集结果三态（ok/empty/error，ADR 0007）+ 编排第四态
planned_absent（issue #113，spec #111/#102）：已定方向待接线的 section
占位缺席——非抓取结果、非故障，消费方（复盘就绪归因）据此区分
"计划缺席"与"未采集/采集失败/上游无数据"。

select_collection_log 供 query_collection_log 工具（tools_ledger）与调度 CLI
读取；复盘就绪检查"先读台账而非探测猜测"的依据即本表。
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# status 词表：ok/empty/error（ADR 0007 三态）+ planned_absent（第四态占位，
# issue #113）。取词表外值属编程错误，fail-fast 抛 ValueError（不落账、不吞没）。
LEDGER_STATUSES = ("ok", "empty", "error", "planned_absent")

# 逐源明细摘要的长度上限：台账是可审计摘要而非完整错误转储
_DETAIL_MAX = 500

_INSERT_SQL = """
INSERT INTO collection_log
    (trade_date, section, status, source, rows, error_kind, detail, started_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""


def summarize_attempted(attempted: list[dict] | None) -> str | None:
    """attempted_sources 逐源明细 → 单行摘要（source:failure_kind:error）。"""
    if not attempted:
        return None
    text = "; ".join(
        f"{a.get('source')}:{a.get('failure_kind')}:{a.get('error')}" for a in attempted
    )
    return text[:_DETAIL_MAX]


def already_collected(conn, trade_date: str, section: str) -> bool:
    """当日该 section 已有 ok/empty 台账行（采集已成功完成）。

    低频纪律（issue #120，THS 涨停池"每日每池一次"）守卫依据：采集已成功
    的 section 当日不再打上游；error 不在其列（失败允许当日修复后重跑）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM collection_log WHERE trade_date = %s"
            " AND section = %s AND status IN ('ok', 'empty') LIMIT 1",
            (trade_date, section),
        )
        return cur.fetchone() is not None


def record_collection(
    conn,
    *,
    trade_date: str,
    section: str,
    status: str,
    source: str | None = None,
    rows: int | None = None,
    error_kind: str | None = None,
    detail: str | None = None,
    started_at: datetime | None = None,
) -> None:
    """落一条采集台账。

    status 取词表外值属编程错误，fail-fast 抛 ValueError（不落账、不吞没）；
    台账写入本身的 IO 失败仅 log，绝不抛给调用方（台账不拖垮主流程）。
    """
    if status not in LEDGER_STATUSES:
        raise ValueError(f"未知台账状态: {status!r}（可选: {', '.join(LEDGER_STATUSES)}）")
    try:
        with conn.cursor() as cur:
            cur.execute(
                _INSERT_SQL,
                (
                    trade_date,
                    section,
                    status,
                    source,
                    rows,
                    error_kind,
                    detail[:_DETAIL_MAX] if detail else None,
                    started_at,
                ),
            )
        conn.commit()
    except Exception:  # noqa: BLE001 - 审计设施失败不拖垮主流程
        log.warning("采集台账落账失败（%s/%s/%s），主流程不受影响", trade_date, section, status)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


_LOG_COLS = (
    "trade_date", "section", "status", "source", "rows",
    "error_kind", "detail", "started_at", "finished_at",
)

# 单次返回行数上限：约束输出体量（query_collection_log 契约 ≤50KB）
DEFAULT_LOG_LIMIT = 100
MAX_LOG_LIMIT = 500

# 返回体量硬上限（query_collection_log 契约）：超限从最旧行起截断
MAX_LOG_BYTES = 50 * 1024


def select_collection_log(
    conn,
    trade_date: str | None = None,
    section: str | None = None,
    status: str | None = None,
    limit: int = DEFAULT_LOG_LIMIT,
) -> list[dict]:
    """按日期/section/状态过滤查台账，按完成时间倒序（最新采集在前）。"""
    filters = {"trade_date": trade_date, "section": section, "status": status}
    where = [f"{col} = %s" for col, val in filters.items() if val is not None]
    args: list = [val for val in filters.values() if val is not None]
    sql = f"SELECT {', '.join(_LOG_COLS)} FROM collection_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY finished_at DESC, id DESC LIMIT %s"
    args.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = [dict(zip(_LOG_COLS, row)) for row in cur.fetchall()]
    for row in rows:
        for key in ("started_at", "finished_at"):
            if isinstance(row[key], datetime):
                row[key] = row[key].astimezone(timezone.utc).isoformat()
    return rows
