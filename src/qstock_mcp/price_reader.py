"""个股价格读取接缝（issue #159）；默认 legacy，后续实现经注册切换。"""

import os

from .adapters.base import BAR_FIELDS


class LegacyPriceReader:
    def select_daily(self, conn, stock_code: str, adj: str, start: str, end: str) -> list[dict]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, open, high, low, close, volume, amount, amplitude,
                    change_percent, change_amount, turnover_rate, source, tradestatus
                FROM stock_daily
                WHERE stock_code=%s AND adj=%s AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date
                """,
                (stock_code, adj, start, end),
            )
            return [
                dict(zip(BAR_FIELDS + ("source", "tradestatus"), row))
                for row in cur.fetchall()
            ]

    def select_dates(self, conn, stock_code: str, adj: str, start: str, end: str) -> set[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date FROM stock_daily"
                " WHERE stock_code=%s AND adj=%s AND trade_date BETWEEN %s AND %s",
                (stock_code, adj, start, end),
            )
            return {row[0] for row in cur.fetchall()}

    def select_latest_date(self, conn) -> str | None:
        with conn.cursor() as cur:
            cur.execute("SELECT max(trade_date) FROM stock_daily")
            return cur.fetchone()[0]

    def select_dominant_adj(self, conn, trade_date: str) -> str | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT adj FROM stock_daily WHERE trade_date=%s"
                " GROUP BY adj ORDER BY COUNT(*) DESC,"
                " CASE WHEN adj='qfq' THEN 0 ELSE 1 END, adj LIMIT 1",
                (trade_date,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def select_window_anchor(self, conn, trade_date: str, days: int) -> str | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT trade_date FROM stock_daily"
                " WHERE trade_date<=%s ORDER BY trade_date DESC LIMIT %s",
                (trade_date, days),
            )
            rows = cur.fetchall()
        return rows[-1][0] if rows else None

    def select_today_rows(self, conn, trade_date: str, adj: str) -> dict[str, dict]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, close, change_percent, volume, amount, turnover_rate"
                " FROM stock_daily WHERE trade_date=%s AND adj=%s",
                (trade_date, adj),
            )
            cols = ("stock_code", "close", "change_percent", "volume", "amount", "turnover_rate")
            return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}

    def select_window_max_close(self, conn, adj: str, start: str, end: str) -> dict[str, float]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, MAX(close) FROM stock_daily"
                " WHERE adj=%s AND trade_date BETWEEN %s AND %s"
                " AND close IS NOT NULL GROUP BY stock_code",
                (adj, start, end),
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    def select_sw_board_daily(
        self, conn, trade_date: str, level: int, prefix: str | None = None, adj: str = "qfq"
    ) -> list[dict]:
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
           COUNT(*) FILTER (WHERE d.change_percent > 0 AND d.amount IS NOT NULL) AS up_count,
           COUNT(*) FILTER (WHERE d.change_percent < 0 AND d.amount IS NOT NULL) AS down_count,
           COUNT(*) FILTER (WHERE d.change_percent = 0 AND d.amount IS NOT NULL) AS flat_count,
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
       a.up_count, a.down_count, a.flat_count, a.amount, a.stock_count, a.counted_count
FROM agg a
LEFT JOIN sw_industry_meta m ON m.industry_code = a.industry_code
ORDER BY a.industry_code
"""


_READERS = {"legacy": LegacyPriceReader()}


def register_price_reader(name: str, reader) -> None:
    _READERS[name] = reader


def get_price_reader():
    name = os.environ.get("QSTOCK_PRICE_SOURCE", "legacy")
    try:
        return _READERS[name]
    except KeyError as exc:
        raise RuntimeError(f"QSTOCK_PRICE_SOURCE={name!r} 尚未注册或实现") from exc


def select_daily(conn, stock_code: str, adj: str, start: str, end: str) -> list[dict]:
    return get_price_reader().select_daily(conn, stock_code, adj, start, end)


def select_dates(conn, stock_code: str, adj: str, start: str, end: str) -> set[str]:
    return get_price_reader().select_dates(conn, stock_code, adj, start, end)


def select_latest_date(conn) -> str | None:
    return get_price_reader().select_latest_date(conn)


def select_dominant_adj(conn, trade_date: str) -> str | None:
    return get_price_reader().select_dominant_adj(conn, trade_date)


def select_window_anchor(conn, trade_date: str, days: int) -> str | None:
    return get_price_reader().select_window_anchor(conn, trade_date, days)


def select_today_rows(conn, trade_date: str, adj: str) -> dict[str, dict]:
    return get_price_reader().select_today_rows(conn, trade_date, adj)


def select_window_max_close(conn, adj: str, start: str, end: str) -> dict[str, float]:
    return get_price_reader().select_window_max_close(conn, adj, start, end)


def select_sw_board_daily(
    conn, trade_date: str, level: int, prefix: str | None = None, adj: str = "qfq"
) -> list[dict]:
    return get_price_reader().select_sw_board_daily(conn, trade_date, level, prefix, adj)
