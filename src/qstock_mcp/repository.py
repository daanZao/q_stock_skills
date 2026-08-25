"""stock_daily / market_snapshot 落库与读取：按业务键 upsert。

stock_daily 业务键 (stock_code, trade_date, adj)；market_snapshot 业务键
(trade_date, stock_code)。每行记录实际来源 source；重复 upsert 幂等
（不产生重复行，更新为最新值）。
"""

from psycopg2.extras import execute_values

from .adapters.base import BAR_FIELDS, SNAPSHOT_FIELDS

_UPSERT_SQL = """
INSERT INTO stock_daily (stock_code, trade_date, {cols}, adj, source, updated_at)
VALUES %s
ON CONFLICT (stock_code, trade_date, adj) DO UPDATE SET
    {updates},
    source = EXCLUDED.source,
    updated_at = CURRENT_TIMESTAMP
""".format(
    cols=", ".join(BAR_FIELDS[1:]),
    updates=", ".join(f"{c} = EXCLUDED.{c}" for c in BAR_FIELDS[1:]),
)

_SELECT_SQL = """
SELECT {cols}, source FROM stock_daily
WHERE stock_code = %s AND adj = %s AND trade_date BETWEEN %s AND %s
ORDER BY trade_date
""".format(cols=", ".join(BAR_FIELDS))


def upsert_daily(conn, stock_code: str, adj: str, rows: list[dict], source: str) -> int:
    """按业务键 upsert，返回写入行数。"""
    values = [
        [stock_code, row["trade_date"]]
        + [row.get(f) for f in BAR_FIELDS[1:]]
        + [adj, source]
        for row in rows
    ]
    if not values:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            _UPSERT_SQL,
            values,
            # 占位符数 = stock_code + trade_date + 数据列 + adj + source
            template="(" + ", ".join(["%s"] * (len(BAR_FIELDS) + 3)) + ", CURRENT_TIMESTAMP)",
        )
    conn.commit()
    return len(values)


def select_daily(
    conn, stock_code: str, adj: str, start: str, end: str
) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL, (stock_code, adj, start, end))
        return [
            dict(zip(BAR_FIELDS + ("source",), row)) for row in cur.fetchall()
        ]


def select_dates(conn, stock_code: str, adj: str, start: str, end: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date FROM stock_daily "
            "WHERE stock_code = %s AND adj = %s AND trade_date BETWEEN %s AND %s",
            (stock_code, adj, start, end),
        )
        return {row[0] for row in cur.fetchall()}


_UPSERT_SNAPSHOT_SQL = """
INSERT INTO market_snapshot (trade_date, {cols}, source, created_at)
VALUES %s
ON CONFLICT (trade_date, stock_code) DO UPDATE SET
    {updates},
    source = EXCLUDED.source
""".format(
    cols=", ".join(SNAPSHOT_FIELDS),
    updates=", ".join(
        f"{c} = EXCLUDED.{c}" for c in SNAPSHOT_FIELDS if c != "stock_code"
    ),
)

_SELECT_SNAPSHOT_SQL = """
SELECT trade_date, {cols}, source FROM market_snapshot
{{where}}
ORDER BY stock_code
""".format(cols=", ".join(SNAPSHOT_FIELDS))


def upsert_snapshot(conn, trade_date: str, rows: list[dict], source: str) -> int:
    """按 (trade_date, stock_code) 业务键 upsert，返回写入行数。"""
    values = [
        [trade_date] + [row.get(f) for f in SNAPSHOT_FIELDS] + [source]
        for row in rows
    ]
    if not values:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            _UPSERT_SNAPSHOT_SQL,
            values,
            # 占位符数 = trade_date + 数据列 + source
            template="(" + ", ".join(["%s"] * (len(SNAPSHOT_FIELDS) + 2)) + ", CURRENT_TIMESTAMP)",
        )
    conn.commit()
    return len(values)


def select_snapshot(
    conn, trade_date: str | None = None, stock_code: str | None = None
) -> list[dict]:
    """按日期/代码过滤查询快照；两者都为 None 时返回全表（慎用）。"""
    where, args = [], []
    if trade_date is not None:
        where.append("trade_date = %s")
        args.append(trade_date)
    if stock_code is not None:
        where.append("stock_code = %s")
        args.append(stock_code)
    sql = _SELECT_SNAPSHOT_SQL.format(
        where="WHERE " + " AND ".join(where) if where else ""
    )
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return [
            dict(zip(("trade_date",) + SNAPSHOT_FIELDS + ("source",), row))
            for row in cur.fetchall()
        ]


def select_latest_snapshot_date(conn) -> str | None:
    """库内最新快照交易日；无数据返回 None。"""
    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM market_snapshot")
        return cur.fetchone()[0]
