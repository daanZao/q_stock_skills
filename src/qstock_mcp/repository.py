"""stock_daily / market_snapshot 落库与读取：按业务键 upsert。

stock_daily 业务键 (stock_code, trade_date, adj)；market_snapshot 业务键
(trade_date, stock_code)。每行记录实际来源 source；重复 upsert 幂等
（不产生重复行，更新为最新值）。
"""

from psycopg2.extras import execute_values

from .adapters.base import (
    BAR_FIELDS,
    BOARD_FIELDS,
    INDEX_FIELDS,
    LHB_BASIC_FIELDS,
    LHB_STATISTIC_FIELDS,
    LHB_YYB_CAPITAL_FIELDS,
    LHB_YYB_MOST_FIELDS,
    SNAPSHOT_FIELDS,
    STRONG_FIELDS,
    ZT_POOL_FIELDS,
)

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


# ---------------------------------------------------------------- 盘面快照（issue #5）

# section → [(表名, 日期列, 业务键(除日期列), 数据列)]；表结构见 sql/003-007。
# 盘面表均无 source 列（忠于 appdb），来源只在工具结果中报告。
BOARD_SECTION_TABLES: dict[str, list[tuple[str, str, tuple, tuple]]] = {
    "indices": [("market_indices", "trade_date", ("index_code",), INDEX_FIELDS)],
    "boards": [
        ("market_boards", "trade_date", ("board_type", "board_name"), BOARD_FIELDS)
    ],
    "zt_pool": [("zt_pool", "trade_date", ("pool_type", "stock_code"), ZT_POOL_FIELDS)],
    "strong_stocks": [
        ("strong_stocks", "trade_date", ("stock_code",), STRONG_FIELDS)
    ],
    "lhb": [
        ("lhb_basic", "trade_date", ("stock_code",), LHB_BASIC_FIELDS),
        ("lhb_stock_statistic", "trade_date", ("stock_code",), LHB_STATISTIC_FIELDS),
        ("lhb_yyb_capital", "fetch_date", ("seat_name",), LHB_YYB_CAPITAL_FIELDS),
        ("lhb_yyb_most", "fetch_date", ("seat_name",), LHB_YYB_MOST_FIELDS),
    ],
}


def upsert_board_rows(
    conn,
    table: str,
    date_col: str,
    key_cols: tuple,
    fields: tuple,
    default_date: str,
    rows: list[dict],
) -> int:
    """按业务键 (date_col, key_cols) upsert 盘面表，返回写入行数（幂等）。

    行 dict 自带 date_col 键时覆盖 default_date（如 lhb_basic 的"上榜日"）。
    表名/列名仅来自 BOARD_SECTION_TABLES 内部常量，不接受外部输入。
    """
    values = [
        [row.get(date_col) or default_date] + [row.get(f) for f in fields]
        for row in rows
    ]
    if not values:
        return 0
    cols = (date_col,) + tuple(fields)
    updates = [f for f in fields if f not in key_cols]
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT ({', '.join((date_col,) + tuple(key_cols))}) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
    )
    with conn.cursor() as cur:
        execute_values(
            cur,
            sql,
            values,
            template="(" + ", ".join(["%s"] * len(cols)) + ")",
        )
    conn.commit()
    return len(values)


# 可查询的盘面表 → (日期列, 代码列)；表名/列名为内部常量。lhb_stock_detail 无业务
# 唯一键（忠于 appdb），仅随父表存在，供查询。
BOARD_QUERY_TABLES: dict[str, tuple[str, str]] = {
    "market_indices": ("trade_date", "index_code"),
    "market_boards": ("trade_date", "board_name"),
    "zt_pool": ("trade_date", "stock_code"),
    "strong_stocks": ("trade_date", "stock_code"),
    "lhb_basic": ("trade_date", "stock_code"),
    "lhb_stock_detail": ("trade_date", "stock_code"),
    "lhb_stock_statistic": ("trade_date", "stock_code"),
    "lhb_yyb_capital": ("fetch_date", "seat_name"),
    "lhb_yyb_most": ("fetch_date", "seat_name"),
}

_INTERNAL_COLS = ("id", "created_at", "updated_at")


def select_latest_board_date(conn, table: str, date_col: str) -> str | None:
    """库内该表最新日期；无数据返回 None。"""
    with conn.cursor() as cur:
        cur.execute(f"SELECT max({date_col}) FROM {table}")
        return cur.fetchone()[0]


def select_board_rows(
    conn,
    table: str,
    date_col: str,
    code_col: str,
    date: str,
    code: str | None = None,
) -> list[dict]:
    """按日期/代码过滤查询盘面表；内部列（id/created_at/updated_at）不返回。"""
    where, args = [f"{date_col} = %s"], [date]
    if code is not None:
        where.append(f"{code_col} = %s")
        args.append(code)
    sql = f"SELECT * FROM {table} WHERE {' AND '.join(where)} ORDER BY {code_col}"
    with conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d.name for d in cur.description]
        return [
            {k: v for k, v in zip(cols, row) if k not in _INTERNAL_COLS}
            for row in cur.fetchall()
        ]
