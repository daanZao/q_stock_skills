"""stock_daily / market_snapshot 落库与读取：按业务键 upsert。

stock_daily 业务键 (stock_code, trade_date, adj)；market_snapshot 业务键
(trade_date, stock_code)。每行记录实际来源 source；重复 upsert 幂等
（不产生重复行，更新为最新值）。init 轻量初始化（issue #8）新增
stock_list（主键 stock_code）与 index_daily（业务键 index_code + trade_date）。
"""

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


# ---------------------------------------------------------------- 盘面快照（issue #5）

# section → [(表名, 日期列, 业务键(除日期列), 数据列)]；表结构见 sql/003-007。
# 盘面表均无 source 列：盘面 section 基本为 akshare 单源，来源在工具结果中
# 报告即可，不值得占用 schema 列（本项目 DDL 决策，非继承旧库）。
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
