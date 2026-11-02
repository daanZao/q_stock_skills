"""历史分析窗口覆盖核验：纯读库内数据与采集台账，不触发抓取。"""

from ._sw_classify import load_sw_industry_meta_seed
from .dates import normalize_date, trading_days_between
from .db import connect
from .output import error as _error
from .repository import select_stock_codes
from .tools_init import MAJOR_INDICES


def _dates(conn, table: str, code_column: str, code: str, start: str, end: str,
           adj: str | None = None) -> set[str]:
    sql = (
        f"SELECT trade_date FROM {table} WHERE {code_column} = %s"
        " AND trade_date BETWEEN %s AND %s"
    )
    args: list = [code, start, end]
    if adj is not None:
        sql += " AND adj = %s"
        args.append(adj)
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return {row[0] for row in cur.fetchall()}


def _entry(code: str, name: str | None, present: set[str], calendar: list[str]) -> dict:
    missing = [day for day in calendar if day not in present]
    return {
        "code": code,
        "name": name,
        "rows": len(present),
        "first_date": min(present) if present else None,
        "last_date": max(present) if present else None,
        "missing_days": missing,
        "status": "gap" if missing else "ok",
    }


def verify_history_coverage(start: str, end: str, adj: str = "qfq") -> dict:
    """按交易日历核验个股、指数、申万板块与龙虎榜窗口覆盖。"""
    tool = "verify_history_coverage"
    params = {"start": start, "end": end, "adj": adj}
    try:
        start_d, end_d = normalize_date(start), normalize_date(end)
    except ValueError as e:
        return _error(tool, params, str(e))
    if start_d > end_d:
        return _error(tool, params, f"start({start_d}) 晚于 end({end_d})")
    calendar = trading_days_between(start_d, end_d)
    if not calendar:
        return _error(tool, params, "窗口内无交易日")
    conn, db_error = connect()
    if db_error:
        return _error(tool, params, db_error)
    try:
        stocks = []
        for code in select_stock_codes(conn):
            present = _dates(conn, "stock_daily", "stock_code", code, start_d, end_d, adj)
            stocks.append(_entry(code, None, present, calendar))
        if not stocks:
            stocks = [{
                "code": "stock_list", "name": None, "rows": 0,
                "first_date": None, "last_date": None,
                "missing_days": calendar, "status": "gap",
            }]

        indices = [
            _entry(
                code, name,
                _dates(conn, "index_daily", "index_code", code, start_d, end_d),
                calendar,
            )
            for code, name in MAJOR_INDICES
        ]

        boards = [
            _entry(
                board["industry_code"], board["name"],
                _dates(
                    conn, "market_boards", "board_code",
                    board["industry_code"], start_d, end_d,
                ),
                calendar,
            )
            for board in load_sw_industry_meta_seed()
            if board["level"] in (1, 2)
        ]

        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, COUNT(*) FROM lhb_basic"
                " WHERE trade_date BETWEEN %s AND %s GROUP BY trade_date",
                (start_d, end_d),
            )
            basic_counts = dict(cur.fetchall())
            cur.execute(
                "SELECT trade_date, COUNT(*) FROM lhb_stock_statistic"
                " WHERE trade_date BETWEEN %s AND %s GROUP BY trade_date",
                (start_d, end_d),
            )
            statistic_counts = dict(cur.fetchall())
            cur.execute(
                "SELECT DISTINCT ON (trade_date) trade_date, status"
                " FROM collection_log WHERE section = 'lhb'"
                " AND trade_date BETWEEN %s AND %s ORDER BY trade_date, id DESC",
                (start_d, end_d),
            )
            ledger_status = dict(cur.fetchall())
        lhb = []
        for day in calendar:
            basic = basic_counts.get(day, 0)
            statistic = statistic_counts.get(day, 0)
            ledger = ledger_status.get(day)
            ready = (
                (basic == 0 and ledger in ("ok", "empty"))
                or (basic > 0 and statistic > 0)
            )
            lhb.append({
                "trade_date": day,
                "basic_rows": basic,
                "statistic_rows": statistic,
                "ledger_status": ledger,
                "status": "ok" if ready else "gap",
            })
    finally:
        conn.close()

    sections = {
        "stock_daily": stocks,
        "index_daily": indices,
        "market_boards": boards,
        "lhb": lhb,
    }
    gaps = sum(
        1
        for entries in sections.values()
        for entry in entries
        if entry["status"] != "ok"
    )
    return {
        "status": "ok" if gaps == 0 else "error",
        "tool": tool,
        "params": params,
        "range": {"start": start_d, "end": end_d},
        "calendar_days": len(calendar),
        "summary": {"gaps": gaps},
        "sections": sections,
    }
