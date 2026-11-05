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


class PriceQueryError(RuntimeError):
    def __init__(self, message: str, error_kind: str):
        super().__init__(message)
        self.error_kind = error_kind


class NewPriceReader:
    def query_daily(self, conn, stock_code: str, adj: str, start: str, end: str) -> dict:
        from .dates import trading_days_between

        window_start = os.environ.get("QSTOCK_ADJ_WINDOW_START", "20230101").replace("-", "")
        if adj == "qfq" and end < window_start:
            rows = LegacyPriceReader().select_daily(conn, stock_code, "qfq", start, end)
            for row in rows:
                row["price_basis"] = "legacy_qfq"
            return {
                "rows": rows,
                "adjust": {
                    "anchor": rows[-1]["trade_date"] if rows else None,
                    "events": [],
                    "coverage": "partial",
                },
                "note": "2023 年以前为 legacy qfq 只读口径，不与新派生序列拼接",
            }
        if adj == "qfq" and start < window_start:
            raise PriceQueryError(
                "qfq 请求跨越新旧复权边界，请按 2023-01-01 分界分别查询",
                "invalid_range",
            )
        if adj in ("hfq", "none") and start < window_start:
            raise PriceQueryError(
                "新窗口外的 hfq/raw 数据不可用，不从 legacy qfq 反推",
                "data_unavailable",
            )

        price_table = "r" if adj == "none" else "h"
        hfq_join = "" if adj == "none" else (
            "JOIN stock_daily_hfq h"
            " ON h.stock_code=r.stock_code AND h.trade_date=r.trade_date"
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT r.trade_date, {price_table}.open, {price_table}.high,
                    {price_table}.low, {price_table}.close, r.volume, r.amount,
                    r.amplitude, r.change_percent, r.change_amount, r.turnover_rate,
                    r.source, r.tradestatus
                FROM stock_daily_raw r
                {hfq_join}
                WHERE r.stock_code=%s AND r.trade_date BETWEEN %s AND %s
                ORDER BY r.trade_date
                """,
                (stock_code, start, end),
            )
            rows = [
                dict(zip(BAR_FIELDS + ("source", "tradestatus"), row))
                for row in cur.fetchall()
            ]
            cur.execute(
                "SELECT baseline_factor, factor_latest, latest_trade_date, rebuild_pending"
                " FROM stock_factor_state WHERE stock_code=%s",
                (stock_code,),
            )
            state = cur.fetchone()
            cur.execute(
                """
                SELECT ex_date, factor_cum, kind, detail
                FROM stock_adjust_factor
                WHERE stock_code=%s AND ex_date BETWEEN %s AND %s
                ORDER BY ex_date
                """,
                (stock_code, start, end),
            )
            event_rows = cur.fetchall()
            cur.execute(
                "SELECT factor_cum FROM stock_adjust_factor"
                " WHERE stock_code=%s AND ex_date<%s ORDER BY ex_date DESC LIMIT 1",
                (stock_code, start),
            )
            previous_event = cur.fetchone()
            cur.execute(
                """
                SELECT DISTINCT ON (trade_date) trade_date, status
                FROM collection_log
                WHERE section='stock_adjust_factor' AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date, finished_at DESC, id DESC
                """,
                (start, end),
            )
            factor_log = cur.fetchall()

        if adj == "qfq" and (state is None or state[1] is None):
            raise PriceQueryError("复权基线或最新锚点不可用，无法派生 qfq", "data_unavailable")
        factor_latest = state[1] if state is not None else None
        for row in rows:
            if adj == "qfq":
                assert factor_latest is not None
                for field in ("open", "high", "low", "close"):
                    if row[field] is not None:
                        row[field] = float(row[field]) / float(factor_latest)
            row["price_basis"] = "raw" if adj == "none" else adj
        expected = set(trading_days_between(start, end))
        actual = {row["trade_date"] for row in rows}
        invalid_raw = adj == "none" and any(
            row["close"] is None or row["close"] <= 0 for row in rows
        )
        stale_state = bool(actual) and state is not None and state[2] < max(actual)
        unresolved_events = any(status == "error" for _, status in factor_log)
        if state is None:
            coverage = "unknown"
        elif state[3] or expected != actual or invalid_raw or stale_state or unresolved_events:
            coverage = "partial"
        else:
            coverage = "complete"
        previous_b = (
            previous_event[0] if previous_event is not None
            else state[0] if state is not None else None
        )
        events = []
        for ex_date, factor_cum, kind, detail in event_rows:
            events.append({
                "ex_date": ex_date,
                "factor_cum": float(factor_cum),
                "event_factor": (
                    float(previous_b / factor_cum) if previous_b is not None else None
                ),
                "kind": kind,
                "detail": detail,
            })
            previous_b = factor_cum
        return {
            "rows": rows,
            "adjust": {
                "anchor": state[2] if state is not None else None,
                "events": events,
                "coverage": coverage,
            },
            "note": (
                "复权重建、raw 连续性或事件采集存在未决问题"
                if coverage == "partial" else "复权覆盖状态尚未建立"
            ),
        }

    def select_daily(self, conn, stock_code: str, adj: str, start: str, end: str) -> list[dict]:
        return self.query_daily(conn, stock_code, adj, start, end)["rows"]

    def select_dates(self, conn, stock_code: str, adj: str, start: str, end: str) -> set[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date FROM stock_daily_raw"
                " WHERE stock_code=%s AND trade_date BETWEEN %s AND %s",
                (stock_code, start, end),
            )
            return {row[0] for row in cur.fetchall()}


_READERS = {"legacy": LegacyPriceReader(), "new": NewPriceReader()}


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
