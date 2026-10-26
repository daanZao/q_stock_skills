"""stock_daily / market_snapshot 落库与读取：按业务键 upsert。

stock_daily 业务键 (stock_code, trade_date, adj)；market_snapshot 业务键
(trade_date, stock_code)。每行记录实际来源 source；重复 upsert 幂等
（不产生重复行，更新为最新值）。init 轻量初始化（issue #8）新增
stock_list（主键 stock_code）与 index_daily（业务键 index_code + trade_date）。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg2.extras import Json, execute_values

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
from .dates import add_days

log = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO stock_daily (stock_code, trade_date, {cols}, adj, source, tradestatus, updated_at)
VALUES %s
ON CONFLICT (stock_code, trade_date, adj) DO UPDATE SET
    {updates},
    source = EXCLUDED.source,
    tradestatus = EXCLUDED.tradestatus,
    updated_at = CURRENT_TIMESTAMP
""".format(
    cols=", ".join(BAR_FIELDS[1:]),
    updates=", ".join(f"{c} = EXCLUDED.{c}" for c in BAR_FIELDS[1:]),
)

_SELECT_SQL = """
SELECT {cols}, source, tradestatus FROM stock_daily
WHERE stock_code = %s AND adj = %s AND trade_date BETWEEN %s AND %s
ORDER BY trade_date
""".format(cols=", ".join(BAR_FIELDS))


def upsert_daily(conn, stock_code: str, adj: str, rows: list[dict], source: str) -> int:
    """按业务键 upsert，返回写入行数。

    tradestatus（权威停牌状态，issue #80）：仅 baostock 路径带值，其余源为 None。
    """
    values = [
        [stock_code, row["trade_date"]]
        + [row.get(f) for f in BAR_FIELDS[1:]]
        + [adj, source, row.get("tradestatus")]
        for row in rows
    ]
    if not values:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            _UPSERT_SQL,
            values,
            # 占位符数 = stock_code + trade_date + 数据列 + adj + source + tradestatus
            template="(" + ", ".join(["%s"] * (len(BAR_FIELDS) + 4)) + ", CURRENT_TIMESTAMP)",
        )
    conn.commit()
    return len(values)


def upsert_daily_rows(conn, rows: list[dict], source: str) -> int:
    """全市场批量 upsert（次晨批，issue #83）：行自带 stock_code 与 adj。

    与 upsert_daily 同业务键（stock_code, trade_date, adj），重复执行幂等覆盖。
    """
    values = [
        [row["stock_code"], row["trade_date"]]
        + [row.get(f) for f in BAR_FIELDS[1:]]
        + [row["adj"], source, row.get("tradestatus")]
        for row in rows
    ]
    if not values:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            _UPSERT_SQL,
            values,
            template="(" + ", ".join(["%s"] * (len(BAR_FIELDS) + 4)) + ", CURRENT_TIMESTAMP)",
        )
    conn.commit()
    return len(values)


def select_daily(
    conn, stock_code: str, adj: str, start: str, end: str
) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL, (stock_code, adj, start, end))
        return [
            dict(zip(BAR_FIELDS + ("source", "tradestatus"), row)) for row in cur.fetchall()
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
{{order}}
""".format(cols=", ".join(SNAPSHOT_FIELDS))

# 排序取前 N（issue #48）的 order_by 白名单：列名只来自内部常量，不接外部输入
SNAPSHOT_ORDER_COLUMNS = ("trade_date",) + SNAPSHOT_FIELDS

# 分市场口径（沪/深/北）按代码前缀判定，与 is_bse_code 同例：4/8/9 开头为北交所，
# 6 开头为沪市，其余（0/3）归深市；%% 为 psycopg2 占位符转义
_SNAPSHOT_MARKET_CASE = (
    "CASE WHEN stock_code LIKE '6%%' THEN 'sh'"
    " WHEN stock_code LIKE '4%%' OR stock_code LIKE '8%%' OR stock_code LIKE '9%%'"
    " THEN 'bj' ELSE 'sz' END"
)


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
    conn,
    trade_date: str | None = None,
    stock_code: str | None = None,
    order_by: str | None = None,
    order: str = "desc",
    limit: int | None = None,
) -> list[dict]:
    """按日期/代码过滤查询快照；两者都为 None 时返回全表（慎用）。

    order_by（白名单列）+ limit 为排序取前 N（issue #48），排序与限量下推
    到数据库；order_by 缺省时按 stock_code 升序（原行为），此时 limit 单独
    生效（按代码序截断）。NULL 值恒排末尾。order_by 不配 limit 会退化为
    排序后全表（大结果通道），属调用方契约错误，直接 ValueError。
    """
    where: list[str] = []
    args: list = []
    if trade_date is not None:
        where.append("trade_date = %s")
        args.append(trade_date)
    if stock_code is not None:
        where.append("stock_code = %s")
        args.append(stock_code)
    if order_by is not None:
        if order_by not in SNAPSHOT_ORDER_COLUMNS:
            raise ValueError(f"未知快照排序列: {order_by}")
        if limit is None:
            raise ValueError(f"order_by 须配合 limit（排序取前 N）: {order_by}")
        direction = "ASC" if order == "asc" else "DESC"
        order_sql = f"ORDER BY {order_by} {direction} NULLS LAST, stock_code"
    else:
        order_sql = "ORDER BY stock_code"
    sql = _SELECT_SNAPSHOT_SQL.format(
        where="WHERE " + " AND ".join(where) if where else "",
        order=order_sql,
    )
    if limit is not None:
        sql += " LIMIT %s"
        args.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return [
            dict(zip(("trade_date",) + SNAPSHOT_FIELDS + ("source",), row))
            for row in cur.fetchall()
        ]


# 涨跌判定（issue #48；issue #116 zdf 回退）：有昨收按 最新价 vs 昨收；
# 昨收缺失（腾讯 rank 榜单行不带 pre_close）改由 change_percent（上游 zdf）
# 判定——>0 涨、<0 跌、=0 平；最新价缺失、或昨收与涨跌幅双双缺失（无法判定）
# 计入 no_price 桶。up + down + flat + no_price = 过滤后总行数不变。
_SNAPSHOT_AGGREGATE_SQL = """
SELECT {market} AS market,
    count(*) FILTER (WHERE latest_price > pre_close
        OR (pre_close IS NULL AND latest_price IS NOT NULL AND change_percent > 0)) AS up,
    count(*) FILTER (WHERE latest_price < pre_close
        OR (pre_close IS NULL AND latest_price IS NOT NULL AND change_percent < 0)) AS down,
    count(*) FILTER (WHERE latest_price = pre_close
        OR (pre_close IS NULL AND latest_price IS NOT NULL AND change_percent = 0)) AS flat,
    count(*) FILTER (WHERE latest_price IS NULL
        OR (pre_close IS NULL AND change_percent IS NULL)) AS no_price,
    sum(amount) AS total_amount
FROM market_snapshot
{{where}}
GROUP BY {market}
""".format(market=_SNAPSHOT_MARKET_CASE)

_MARKETS = ("sh", "sz", "bj")


def select_snapshot_aggregate(
    conn,
    trade_date: str | None = None,
    stock_code: str | None = None,
) -> dict:
    """快照聚合（issue #48）下推到数据库：涨跌家数与成交额合计，全市场 + 分市场。

    上涨/下跌/平盘按最新价 vs 昨收判定；昨收缺失（腾讯 rank 榜单行）改由
    change_percent（上游 zdf）判定（issue #116）；最新价缺失、或昨收与涨跌幅
    双双缺失的行（停牌等）计入 no_price 桶，
    up + down + flat + no_price = 过滤后总行数（total 与 by_market 同口径），
    调用方无需假设三桶之和即全部家数。成交额合计为库内万元口径
    （ADR 0006）。无行时各项为 0 / 0.0。
    返回 {"breadth": {"total", "by_market"}, "amount": {"total_amount", "by_market"}}，
    by_market 恒含 sh/sz/bj 三键。
    """
    where, args = [], []
    if trade_date is not None:
        where.append("trade_date = %s")
        args.append(trade_date)
    if stock_code is not None:
        where.append("stock_code = %s")
        args.append(stock_code)
    sql = _SNAPSHOT_AGGREGATE_SQL.format(
        where="WHERE " + " AND ".join(where) if where else ""
    )
    with conn.cursor() as cur:
        cur.execute(sql, args)
        grouped = {
            row[0]: {
                "up": row[1], "down": row[2], "flat": row[3],
                "no_price": row[4], "amount": row[5],
            }
            for row in cur.fetchall()
        }
    zero = {"up": 0, "down": 0, "flat": 0, "no_price": 0}
    breadth_by_market = {
        m: {k: grouped.get(m, {}).get(k, 0) for k in zero} for m in _MARKETS
    }
    amount_by_market = {
        m: float(grouped[m]["amount"]) if m in grouped else 0.0 for m in _MARKETS
    }
    return {
        "breadth": {
            "total": {k: sum(v[k] for v in breadth_by_market.values()) for k in zero},
            "by_market": breadth_by_market,
        },
        "amount": {
            "total_amount": sum(amount_by_market.values()),
            "by_market": amount_by_market,
        },
    }


def select_latest_snapshot_date(conn) -> str | None:
    """库内最新快照交易日；无数据返回 None。"""
    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM market_snapshot")
        return cur.fetchone()[0]


# ---------------------------------------------------------------- init 轻量初始化（issue #8）

_UPSERT_STOCK_LIST_SQL = """
INSERT INTO stock_list (stock_code, stock_name, source, updated_at)
VALUES %s
ON CONFLICT (stock_code) DO UPDATE SET
    stock_name = EXCLUDED.stock_name,
    source = EXCLUDED.source,
    updated_at = CURRENT_TIMESTAMP
"""


def upsert_stock_list(conn, rows: list[dict], source: str) -> int:
    """按 stock_code upsert 股票清单（幂等），返回写入行数。

    缺代码/名称的行跳过（stock_name 为 NOT NULL 列，一行脏数据不拖垮整批）。
    """
    values = [
        [row["stock_code"], row["stock_name"], source]
        for row in rows
        if row.get("stock_code") and row.get("stock_name")
    ]
    if not values:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur, _UPSERT_STOCK_LIST_SQL, values,
            template="(%s, %s, %s, CURRENT_TIMESTAMP)",
        )
    conn.commit()
    return len(values)


def select_stock_codes(conn) -> list[str]:
    """股票清单全部代码（按代码排序），全量回溯的标的来源。"""
    with conn.cursor() as cur:
        cur.execute("SELECT stock_code FROM stock_list ORDER BY stock_code")
        return [row[0] for row in cur.fetchall()]


_UPSERT_INDEX_DAILY_SQL = """
INSERT INTO index_daily (index_code, trade_date, {cols}, source, updated_at)
VALUES %s
ON CONFLICT (index_code, trade_date) DO UPDATE SET
    {updates},
    source = EXCLUDED.source,
    updated_at = CURRENT_TIMESTAMP
""".format(
    cols=", ".join(BAR_FIELDS[1:]),
    updates=", ".join(f"{c} = EXCLUDED.{c}" for c in BAR_FIELDS[1:]),
)


def upsert_index_daily(conn, index_code: str, rows: list[dict], source: str) -> int:
    """按 (index_code, trade_date) 业务键 upsert 指数日线（幂等），返回写入行数。"""
    values = [
        [index_code, row["trade_date"]]
        + [row.get(f) for f in BAR_FIELDS[1:]]
        + [source]
        for row in rows
    ]
    if not values:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            _UPSERT_INDEX_DAILY_SQL,
            values,
            # 占位符数 = index_code + trade_date + 数据列 + source
            template="(" + ", ".join(["%s"] * (len(BAR_FIELDS) + 2)) + ", CURRENT_TIMESTAMP)",
        )
    conn.commit()
    return len(values)


_INDEX_DAILY_QUERY_COLS = ("index_code",) + BAR_FIELDS + ("source",)


def select_index_daily(
    conn,
    index_code: str,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """按指数代码 + 日期范围（yyyymmdd 闭区间）查询 index_daily，升序返回。

    limit 取区间内最近 N 行（先倒序限量再翻转回升序），None 不限；
    start/end 缺省不对该端限界。内部列（id/created_at/updated_at）不返回。
    """
    where, args = ["index_code = %s"], [index_code]
    if start is not None:
        where.append("trade_date >= %s")
        args.append(start)
    if end is not None:
        where.append("trade_date <= %s")
        args.append(end)
    sql = (
        f"SELECT {', '.join(_INDEX_DAILY_QUERY_COLS)} FROM index_daily"
        f" WHERE {' AND '.join(where)} ORDER BY trade_date DESC"
    )
    if limit is not None:
        sql += " LIMIT %s"
        args.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = [dict(zip(_INDEX_DAILY_QUERY_COLS, row)) for row in cur.fetchall()]
    rows.reverse()
    return rows


# ---------------------------------------------------------------- 盘面快照（issue #5）

# section → [(表名, 日期列, 业务键(除日期列), 数据列)]；表结构见 sql/003-007。
# 盘面表基本无 source 列（盘面 section 为东财系单源，来源在工具结果中报告）；
# 例外：market_boards 有 source 列（issue #84，区分实时采集 NULL 与回补
# 'push2his'，sql/015）。
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
        ("lhb_basic", "trade_date", ("stock_code", "lhb_reason"), LHB_BASIC_FIELDS),
        # lhb_stock_statistic 不在适配器承载（issue #114 定稿）：库内
        # lhb_basic 滚动自算，见 recompute_lhb_statistic（tools_board 落库后调用）
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
    同业务键重复行 last-wins 去重（上游脏数据防御），丢弃数记 log。
    """
    cols = (date_col,) + tuple(fields)
    key_idx = [cols.index(k) for k in key_cols]
    deduped: dict[tuple, list] = {}
    for row in rows:
        value = [row.get(date_col) or default_date] + [row.get(f) for f in fields]
        deduped[(value[0], *(value[i] for i in key_idx))] = value
    dropped = len(rows) - len(deduped)
    if dropped:
        log.warning(
            "upsert_board_rows: %s 丢弃 %d 行业务键重复行（last-wins）", table, dropped
        )
    values = list(deduped.values())
    if not values:
        return 0
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


LHB_STATISTIC_WINDOW_DAYS = 60

_LHB_STATISTIC_INSERT_TEMPLATE = """
INSERT INTO lhb_stock_statistic (
    trade_date, stock_code, stock_name,
    appear_count_3m, buy_amount_3m, sell_amount_3m, net_buy_3m,
    buy_seat_count, sell_seat_count, updated_at
)
SELECT %s, stock_code, MAX(stock_name), COUNT(*),
       SUM(buy_amount), SUM(sell_amount), SUM(net_buy_amount),
       NULL, NULL, CURRENT_TIMESTAMP
FROM {source}
WHERE trade_date >= %s AND trade_date <= %s
GROUP BY stock_code
"""

_LHB_STATISTIC_INSERT_SQL = _LHB_STATISTIC_INSERT_TEMPLATE.format(source="lhb_basic")
_LHB_STATISTIC_STAGING_INSERT_SQL = _LHB_STATISTIC_INSERT_TEMPLATE.format(
    source="""
    (
        SELECT trade_date, stock_code, stock_name, lhb_reason,
               buy_amount, sell_amount, net_buy_amount
        FROM lhb_basic
        UNION ALL
        SELECT s.trade_date, s.stock_code, s.stock_name, s.lhb_reason,
               s.buy_amount, s.sell_amount, s.net_buy_amount
        FROM lhb_stat_staging AS s
        WHERE NOT EXISTS (
            SELECT 1 FROM lhb_basic AS b
            WHERE b.trade_date = s.trade_date
              AND b.stock_code = s.stock_code
              AND b.lhb_reason = s.lhb_reason
        )
    ) AS statistic_input
    """
)


def replace_lhb_basic_day(
    conn, trade_date: str, rows: list[dict], staging_rows: list[dict] | None = None
) -> tuple[int, int]:
    """原子替换当日 basic 并重算 statistic；前置行只进连接级临时表。"""
    cols = ("trade_date",) + LHB_BASIC_FIELDS
    values = [[trade_date] + [row.get(f) for f in LHB_BASIC_FIELDS] for row in rows]
    staging_values = [
        [row.get("trade_date"), row.get("stock_code"), row.get("stock_name"),
         row.get("lhb_reason") or "", row.get("buy_amount"),
         row.get("sell_amount"), row.get("net_buy_amount")]
        for row in (staging_rows or [])
    ]
    window_start = add_days(trade_date, -LHB_STATISTIC_WINDOW_DAYS)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE IF NOT EXISTS lhb_stat_staging ("
                "trade_date text, stock_code text, stock_name text, lhb_reason text, "
                "buy_amount real, sell_amount real, net_buy_amount real"
                ") ON COMMIT DELETE ROWS"
            )
            cur.execute("DELETE FROM lhb_stat_staging")
            if staging_values:
                execute_values(
                    cur,
                    "INSERT INTO lhb_stat_staging VALUES %s",
                    staging_values,
                )
            cur.execute("DELETE FROM lhb_stock_statistic WHERE trade_date = %s",
                        (trade_date,))
            cur.execute("DELETE FROM lhb_basic WHERE trade_date = %s", (trade_date,))
            if values:
                execute_values(
                    cur,
                    f"INSERT INTO lhb_basic ({', '.join(cols)}) VALUES %s",
                    values,
                    template="(" + ", ".join(["%s"] * len(cols)) + ")",
                )
            cur.execute(
                _LHB_STATISTIC_STAGING_INSERT_SQL,
                (trade_date, window_start, trade_date),
            )
            statistic_rows = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(values), statistic_rows


# ---------------------------------------------------------------- lhb 统计子表（库内自算）

def recompute_lhb_statistic(conn, trade_date: str) -> int:
    """lhb_stock_statistic 库内滚动自算（issue #114 定稿口径）：以 lhb_basic
    近 60 日历日窗口（含当日）按股聚合——appear=行数（业务键含 lhb_reason，
    同股当日多榜单各计一次）、buy/sell/net=窗口合计（万元，ADR 0006）。
    先删当日快照再插入：幂等可重跑，窗口滑出/上游修订自愈（旧快照股票不再
    入窗即删除，不残留）。buy/sell_seat_count 恒 NULL：官方汇总无席位计数
    来源、不伪造（lhb_stock_detail 接线后才可派生）。返回写入行数。"""
    window_start = add_days(trade_date, -LHB_STATISTIC_WINDOW_DAYS)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM lhb_stock_statistic WHERE trade_date = %s",
                    (trade_date,))
        cur.execute(
            _LHB_STATISTIC_INSERT_SQL,
            (trade_date, window_start, trade_date),
        )
        n = cur.rowcount
    conn.commit()
    return n


def select_lhb_statistic(conn, trade_date: str) -> list[dict]:
    """读某日 statistic 自算快照（`lhb --check` 对拍的库内侧；纯读）。"""
    cols = ("trade_date",) + LHB_STATISTIC_FIELDS
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(cols)} FROM lhb_stock_statistic"
            " WHERE trade_date = %s ORDER BY stock_code",
            (trade_date,),
        )
        return [dict(zip(cols, row)) for row in cur.fetchall()]


_BOARD_HISTORY_COLS = ("trade_date",) + BOARD_FIELDS

_BOARD_HISTORY_UPSERT_SQL = """
INSERT INTO market_boards ({cols}, source)
VALUES %s
ON CONFLICT (trade_date, board_type, board_name) DO UPDATE SET
    {updates},
    source = EXCLUDED.source
""".format(
    cols=", ".join(_BOARD_HISTORY_COLS),
    updates=", ".join(
        f"{c} = EXCLUDED.{c}"
        for c in BOARD_FIELDS
        if c not in ("board_type", "board_name")
    ),
)


def upsert_board_history(conn, rows: list[dict], source: str) -> int:
    """板块历史回补落库（issue #84，issue #121 起仅申万 sw 源使用）：行自带
    trade_date/board_type/board_name，按业务键幂等 upsert 并写 source 标注
    （'swsresearch'，与实时采集行的 NULL 区分；存量 'push2his' 行为东财回补
    退役前产物，保留只读）。"""
    values = [[row.get(c) for c in _BOARD_HISTORY_COLS] + [source] for row in rows]
    if not values:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            _BOARD_HISTORY_UPSERT_SQL,
            values,
            template="(" + ", ".join(["%s"] * (len(_BOARD_HISTORY_COLS) + 1)) + ")",
        )
    conn.commit()
    return len(values)


# 可查询的盘面表 → (日期列, 代码列)；表名/列名为内部常量。lhb_stock_detail 无业务
# 唯一键（忠于 appdb），仅随父表存在，供查询。
BOARD_QUERY_TABLES: dict[str, tuple[str, str]] = {
    "market_indices": ("trade_date", "index_code"),
    "market_boards": ("trade_date", "board_name"),
    "sw_board_valuation": ("trade_date", "board_code"),
    "zt_pool": ("trade_date", "stock_code"),
    "strong_stocks": ("trade_date", "stock_code"),
    "lhb_basic": ("trade_date", "stock_code"),
    "lhb_stock_detail": ("trade_date", "stock_code"),
    "lhb_stock_statistic": ("trade_date", "stock_code"),
    "lhb_yyb_capital": ("fetch_date", "seat_name"),
    "lhb_yyb_most": ("fetch_date", "seat_name"),
}


_SW_BOARD_VALUATION_COLS = (
    "trade_date",
    "board_code",
    "board_name",
    "level",
    "close_index",
    "turnover_rate",
    "pe_ratio",
    "pb_ratio",
    "float_cap",
    "dividend_yield",
)


def upsert_sw_board_valuation(conn, rows: list[dict]) -> int:
    """申万板块分析报表（估值维度）落库（issue #118）：业务键
    (trade_date, board_code) 幂等 upsert，返回写入行数。"""
    values = [[row.get(c) for c in _SW_BOARD_VALUATION_COLS] for row in rows]
    if not values:
        return 0
    sql = (
        f"INSERT INTO sw_board_valuation ({', '.join(_SW_BOARD_VALUATION_COLS)})"
        " VALUES %s ON CONFLICT (trade_date, board_code) DO UPDATE SET "
        + ", ".join(
            f"{c} = EXCLUDED.{c}"
            for c in _SW_BOARD_VALUATION_COLS
            if c != "board_code"
        )
    )
    with conn.cursor() as cur:
        execute_values(
            cur,
            sql,
            values,
            template="(" + ", ".join(["%s"] * len(_SW_BOARD_VALUATION_COLS)) + ")",
        )
    conn.commit()
    return len(values)

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


# ---------------------------------------------------------------- 结论存储（issue #7）

# 业务唯一键 (subject_type, subject_code, trade_date, conclusion_type) 在 schema 层
# 钉死（sql/008_conclusions.sql，docs/adr/0003）；payload 结构 server 不校验。
# inserted/updated 判定用 CTE 预查业务键（语句级快照），不用 xmax 内部列。
_UPSERT_CONCLUSION_SQL = """
WITH prior AS (
    SELECT 1 FROM conclusions
    WHERE subject_type = %s AND subject_code = %s
      AND trade_date = %s AND conclusion_type = %s
), write AS (
    INSERT INTO conclusions (subject_type, subject_code, trade_date, conclusion_type, payload)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (subject_type, subject_code, trade_date, conclusion_type) DO UPDATE SET
        payload = EXCLUDED.payload,
        updated_at = CURRENT_TIMESTAMP
)
SELECT EXISTS (SELECT 1 FROM prior)
"""

_CONCLUSION_COLS = ("subject_type", "subject_code", "trade_date", "conclusion_type", "payload")


def upsert_conclusion(
    conn,
    subject_type: str,
    subject_code: str,
    trade_date: str,
    conclusion_type: str,
    payload: Any,
) -> str:
    """按业务键 upsert 一条结论；返回 "inserted" 或 "updated"。"""
    key = (subject_type, subject_code, trade_date, conclusion_type)
    with conn.cursor() as cur:
        cur.execute(_UPSERT_CONCLUSION_SQL, key + key + (Json(payload),))
        existed = cur.fetchone()[0]
    conn.commit()
    return "updated" if existed else "inserted"


def select_conclusions(
    conn,
    subject_type: str | None = None,
    subject_code: str | None = None,
    trade_date: str | None = None,
    conclusion_type: str | None = None,
) -> list[dict]:
    """按主体/日期/结论类型过滤查询；全为 None 时返回全表（慎用）。内部列不返回。"""
    filters = {
        "subject_type": subject_type,
        "subject_code": subject_code,
        "trade_date": trade_date,
        "conclusion_type": conclusion_type,
    }
    where = [f"{col} = %s" for col, val in filters.items() if val is not None]
    args = [val for val in filters.values() if val is not None]
    sql = (
        f"SELECT {', '.join(_CONCLUSION_COLS)} FROM conclusions"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY trade_date, subject_code, conclusion_type"
    )
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return [dict(zip(_CONCLUSION_COLS, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------- 资讯条目（issue #24/T2）

# 业务唯一键 (news_code, subject_type, subject_code) 在 schema 层钉死
# （sql/011_news_items.sql）。inserted/updated 判定用写入前预查业务键，
# 不用 xmax 内部列（conclusions 先例）。
_NEWS_COLS = (
    "news_code", "subject_type", "subject_code", "information_type",
    "title", "content", "publish_time", "source", "url", "author",
    "ins_name", "rating", "raw",
)

_UPSERT_NEWS_SQL = """
INSERT INTO news_items ({cols}, fetched_at)
VALUES %s
ON CONFLICT (news_code, subject_type, subject_code) DO UPDATE SET
    information_type = EXCLUDED.information_type,
    title = EXCLUDED.title,
    content = EXCLUDED.content,
    publish_time = EXCLUDED.publish_time,
    source = EXCLUDED.source,
    url = EXCLUDED.url,
    author = EXCLUDED.author,
    ins_name = EXCLUDED.ins_name,
    rating = EXCLUDED.rating,
    raw = EXCLUDED.raw,
    fetched_at = now()
""".format(cols=", ".join(_NEWS_COLS))

# 上游 date 字段实测为 "2026-08-28 19:22:00"；容忍常见变体，解析失败跳过该条
_NEWS_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")
# 上游时间为东八区（A 股资讯语境，无夏令时）；naive 写 timestamptz 会按会话时区解释
_NEWS_TZ = timezone(timedelta(hours=8))


def _parse_publish_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    for fmt in _NEWS_DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=_NEWS_TZ)
        except ValueError:
            continue
    return None


def _fallback_news_code(title: Any, date: Any) -> str | None:
    """上游缺条目 id 时的业务键兜底：标题 + 发布时间复合（ticket #24）。"""
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(date, str) or not date.strip():
        return None
    return f"fb:{title.strip()}|{date.strip()}"


def upsert_news_items(
    conn, subject_type: str, subject_code: str, items: list[dict]
) -> dict:
    """按业务键 upsert 一批资讯条目，返回 {"inserted", "updated", "skipped"} 计数。

    items 为上游 news-search 条目 dict（字段名为上游 camelCase）。可选字段
    缺失映射为 None；业务键取上游条目 id，缺失时用标题+发布时间复合兜底；
    date 解析失败、业务键兜底也凑不出、批内键重复的条目跳过并计入
    skipped（一行脏数据不拖垮整批）。
    """
    values = []
    seen: set = set()
    skipped = 0
    for item in items:
        publish_time = _parse_publish_time(item.get("date"))
        code = item.get("code") or _fallback_news_code(
            item.get("title"), item.get("date")
        )
        if not code or publish_time is None or code in seen:
            skipped += 1
            continue
        seen.add(code)
        values.append(
            [
                code,
                subject_type,
                subject_code,
                item.get("informationType"),
                item.get("title"),
                item.get("content"),
                publish_time,
                item.get("source"),
                item.get("jumpUrl"),
                item.get("author"),
                item.get("insName"),
                item.get("rating"),
                Json(item),
            ]
        )
    if not values:
        return {"inserted": 0, "updated": 0, "skipped": skipped}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT news_code FROM news_items"
            " WHERE subject_type = %s AND subject_code = %s AND news_code = ANY(%s)",
            (subject_type, subject_code, [v[0] for v in values]),
        )
        existing = {row[0] for row in cur.fetchall()}
        execute_values(
            cur,
            _UPSERT_NEWS_SQL,
            values,
            template="(" + ", ".join(["%s"] * len(_NEWS_COLS)) + ", now())",
        )
    conn.commit()
    inserted = sum(1 for v in values if v[0] not in existing)
    return {"inserted": inserted, "updated": len(values) - inserted, "skipped": skipped}


# 查询不返回内部列（id/fetched_at）与 raw 原文（select_conclusions 不返回
# 内部列的同先例）；publish_time 序列化为 ISO 字符串，保证输出 JSON 安全
_NEWS_QUERY_COLS = tuple(c for c in _NEWS_COLS if c != "raw")


def _day_start_plus8(yyyymmdd: str) -> str:
    """yyyymmdd → 东八区当日 00:00 的 timestamptz 字面量（与落库时区一致）。"""
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]} 00:00:00+08"


def select_news_items(
    conn,
    subject_type: str | None = None,
    subject_code: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """按 subject + 发布时间范围查资讯：发布时间倒序（news_code 决胜），可限量。

    start/end 为 yyyymmdd 闭区间（按东八区整日计：start 含当日 00:00 起，
    end 含当日 23:59 止）；subject 与时间范围全缺省返回全表（建议带 limit）。
    """
    where: list[str] = []
    args: list = []
    if subject_type is not None:
        where.append("subject_type = %s")
        args.append(subject_type)
    if subject_code is not None:
        where.append("subject_code = %s")
        args.append(subject_code)
    if start is not None:
        where.append("publish_time >= %s::timestamptz")
        args.append(_day_start_plus8(start))
    if end is not None:
        where.append("publish_time < %s::timestamptz")
        args.append(_day_start_plus8(add_days(end, 1)))
    sql = f"SELECT {', '.join(_NEWS_QUERY_COLS)} FROM news_items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY publish_time DESC, news_code"
    if limit is not None:
        sql += " LIMIT %s"
        args.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = [dict(zip(_NEWS_QUERY_COLS, row)) for row in cur.fetchall()]
    for row in rows:
        row["publish_time"] = row["publish_time"].isoformat()
    return rows


# ---------------------------------------------------------------- 成分字典（issue #73）

# sw_industry_class 多体系同构形态（sql/012）：业务键
# (source_system, stock_code, board_code, effective_date)。
def replace_board_classification(conn, source_system: str, rows: list[dict]) -> dict:
    """以文件为准清空重载某 source_system 的成分行，返回 {"deleted", "inserted"}。

    事务由调用方管理（删除与重载须原子，本函数不 commit）；其他
    source_system 的行不受影响。写入前按业务键去重（last-wins，
    upsert_board_rows 同例）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM sw_industry_class WHERE source_system = %s",
            (source_system,),
        )
        deleted = cur.rowcount
        deduped = {
            (r["stock_code"], r["board_code"], r["effective_date"]): r for r in rows
        }
        dropped = len(rows) - len(deduped)
        if dropped:
            log.warning(
                "replace_board_classification: %s 丢弃 %d 行业务键重复行（last-wins）",
                source_system,
                dropped,
            )
        if deduped:
            execute_values(
                cur,
                "INSERT INTO sw_industry_class"
                " (source_system, stock_code, board_code, effective_date, updated_at)"
                " VALUES %s",
                [
                    [
                        source_system,
                        r["stock_code"],
                        r["board_code"],
                        r["effective_date"],
                        r["updated_at"],
                    ]
                    for r in deduped.values()
                ],
            )
    return {"deleted": deleted, "inserted": len(deduped)}


def upsert_sw_industry_meta(conn, rows: list[dict]) -> int:
    """按 industry_code 幂等 upsert 行业名称表（issue #74），返回写入行数。

    只动 sw_industry_meta，不碰成分变更史；名称修正即改后重导。
    """
    values = [[r["industry_code"], r["name"], r["level"]] for r in rows]
    if not values:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO sw_industry_meta (industry_code, name, level) VALUES %s"
            " ON CONFLICT (industry_code) DO UPDATE SET"
            " name = EXCLUDED.name, level = EXCLUDED.level",
            values,
        )
    conn.commit()
    return len(values)


# ---------------------------------------------------------------- 申万板块自算行情（issue #75）


def select_latest_daily_date(conn) -> str | None:
    """stock_daily 库内最新交易日（不限 adj，issue #92：none-only 的交易日不跳过）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM stock_daily")
        return cur.fetchone()[0]


def select_dominant_adj(conn, trade_date: str) -> str | None:
    """当日横截面口径 adj：各 adj 行数多者胜、平局取 qfq（issue #92）。

    单一 adj 防一股双行重复计入；截面指标 change_pct/amount 与复权方式无关。
    当日无任何日线行返回 None。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT adj FROM stock_daily WHERE trade_date = %s"
            " GROUP BY adj"
            " ORDER BY COUNT(*) DESC,"
            " CASE WHEN adj = 'qfq' THEN 0 ELSE 1 END, adj"
            " LIMIT 1",
            (trade_date,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def select_sw_dict_as_of(conn) -> str | None:
    """sw 成分字典 max effective_date（yyyymmdd）；无行返回 None。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(effective_date) FROM sw_industry_class"
            " WHERE source_system = 'sw'"
        )
        d = cur.fetchone()[0]
        return d.strftime("%Y%m%d") if d is not None else None


_SW_BOARD_AGG_SQL = """
WITH pit AS (
    SELECT DISTINCT ON (stock_code) stock_code, board_code
    FROM sw_industry_class
    WHERE source_system = 'sw' AND effective_date <= to_date(%s, 'YYYYMMDD')
    ORDER BY stock_code, effective_date DESC
),
agg AS (
    SELECT CASE WHEN %s = 1 THEN substr(p.board_code, 1, 2) || '0000'
                ELSE substr(p.board_code, 1, 4) || '00' END AS industry_code,
           SUM(d.change_percent * d.amount)
               FILTER (WHERE d.change_percent IS NOT NULL AND d.amount IS NOT NULL)
           / NULLIF(SUM(d.amount)
               FILTER (WHERE d.change_percent IS NOT NULL AND d.amount IS NOT NULL), 0)
               AS change_pct,
           COUNT(*) FILTER (WHERE d.change_percent > 0 AND d.amount IS NOT NULL)
               AS up_count,
           COUNT(*) FILTER (WHERE d.change_percent < 0 AND d.amount IS NOT NULL)
               AS down_count,
           COUNT(*) FILTER (WHERE d.change_percent = 0 AND d.amount IS NOT NULL)
               AS flat_count,
           SUM(d.amount) AS amount,
           COUNT(*) AS stock_count,
           COUNT(*) FILTER (WHERE d.change_percent IS NOT NULL AND d.amount IS NOT NULL)
               AS counted_count
    FROM pit p
    LEFT JOIN stock_daily d
      ON d.stock_code = p.stock_code AND d.trade_date = %s AND d.adj = %s
    {where}
    GROUP BY 1
    HAVING COUNT(d.stock_code) > 0
)
SELECT a.industry_code, m.name AS industry_name, a.change_pct,
       a.up_count, a.down_count, a.flat_count, a.amount,
       a.stock_count, a.counted_count
FROM agg a
LEFT JOIN sw_industry_meta m ON m.industry_code = a.industry_code
ORDER BY a.industry_code
"""


def select_sw_board_daily(
    conn, trade_date: str, level: int, prefix: str | None = None, adj: str = "qfq"
) -> list[dict]:
    """单日截面：point-in-time 成分 × stock_daily(adj 由调用方选定) 按行业聚合
    （下推 SQL）。

    adj 为当日横截面口径（select_dominant_adj 选定，issue #92），每行回带。
    聚合码形态与 sw_industry_meta 一致（一级前2位补 0000、二级前4位补 00），
    名称 join 缺失时 industry_name 为 None。change_pct 为成交额加权
    （change_percent 或 amount 为 null 的停牌占位行从权重剔除并归一，spec #70）；
    涨/跌/平家数与加权同一口径（change_percent 与 amount 双非 null 才计家数）；
    stock_count 为 point-in-time 归属该行业的成分股总数（LEFT JOIN：当日无
    日线行的成分也计入），counted_count 为参与加权数，差即停牌/无行情数；
    全行业当日无任何日线行时该行业不出现（HAVING），空日期 count:0。
    amount 单位万元（库内已是万元口径，ADR 0006）。每行携带 basis 口径标注。
    """
    where, args = "", [trade_date, level, trade_date, adj]
    if prefix is not None:
        where = "WHERE p.board_code LIKE %s"
        args.append(prefix + "%")
    with conn.cursor() as cur:
        cur.execute(_SW_BOARD_AGG_SQL.format(where=where), args)
        return [
            {
                "industry_code": code,
                "industry_name": name,
                "change_pct": round(float(pct), 4) if pct is not None else None,
                "up_count": up,
                "down_count": down,
                "flat_count": flat,
                "amount": round(float(amt), 2) if amt is not None else None,
                "stock_count": stocks,
                "counted_count": counted,
                "adj": adj,
                "basis": "sw_amount_weighted",
            }
            for code, name, pct, up, down, flat, amt, stocks, counted
            in cur.fetchall()
        ]
