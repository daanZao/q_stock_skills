"""stock_daily 落库与读取：按 (stock_code, trade_date, adj) 业务键 upsert。

每行记录实际来源 source；重复 upsert 幂等（不产生重复行，更新为最新值）。
"""

from psycopg2.extras import execute_values

from .adapters.base import BAR_FIELDS

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
