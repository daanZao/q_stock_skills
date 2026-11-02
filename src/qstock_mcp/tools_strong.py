"""strong_stocks 库内自算（issue #120，spec #111，ADR 0012）。

东财/THS 均无"强势股池"对应物，东财口径（60日新高 或 近期多次涨停）本身是
纯派生定义——按调研结论（docs/research/2026-10-20-zt-pool-alternatives.md
"strong_stocks 专项结论"）改为库内自算，口径透明、零外部依赖、可测试：

- **60 日新高**：当日收盘价 = 最近 60 个交易日（含当日，adj 取当日横截面
  口径 select_dominant_adj，issue #92 同规则）收盘价窗口最大值。
- **近期多次涨停**：最近 10 个交易日（含当日，以 stock_daily 库内交易日历
  为准）zt_pool 涨停（pool_type='zt'）次数 ≥ 2。
- 两集合取并集入选；reason 为规则文本（"60日新高" / "多次涨停（近10日 N 次）"
  及组合），对齐东财口径"入选理由"列的退化承载（调研结论）。

stock_daily 当日无数据（自算输入缺失，如收盘后批跑在日线入口之前）→
compute_strong_rows 返回 None，工具层按 no_data_for_date 正常语义跳过
（非故障不中止），先经 daily 入口补当日日线后重跑本段即可。
"""

import logging

from .repository import select_dominant_adj

log = logging.getLogger(__name__)

# 窗口口径（ADR 0012）：60 日新高窗口 / 多次涨停窗口（交易日历来自 stock_daily）
HIGH_WINDOW_DAYS = 60
MULTI_ZT_WINDOW_DAYS = 10
MULTI_ZT_MIN_COUNT = 2


def _window_anchor(conn, trade_date: str, days: int) -> str | None:
    """最近 days 个交易日（含当日）的窗口首日 yyyymmdd；无数据返回 None。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT trade_date FROM stock_daily"
            " WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT %s",
            (trade_date, days),
        )
        rows = cur.fetchall()
    return rows[-1][0] if len(rows) >= 1 else None


def _today_rows(conn, trade_date: str, adj: str) -> dict[str, dict]:
    """当日截面 {stock_code: 行}（收盘价/涨跌/量/额/换手率）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, close, change_percent, volume, amount,"
            "       turnover_rate"
            " FROM stock_daily WHERE trade_date = %s AND adj = %s",
            (trade_date, adj),
        )
        cols = ("stock_code", "close", "change_percent", "volume", "amount",
                "turnover_rate")
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


def _window_max_close(conn, adj: str, start: str, end: str) -> dict[str, float]:
    """窗口内每股最高收盘价 {stock_code: max_close}。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, MAX(close) FROM stock_daily"
            " WHERE adj = %s AND trade_date BETWEEN %s AND %s"
            " AND close IS NOT NULL GROUP BY stock_code",
            (adj, start, end),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def _zt_stats(conn, start: str, end: str) -> dict[str, dict]:
    """窗口内涨停统计 {stock_code: {"count": n, "name": 名, "boards": 最大连板}}。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, COUNT(*), MAX(stock_name), MAX(consecutive_boards)"
            " FROM zt_pool WHERE pool_type = 'zt' AND trade_date BETWEEN %s AND %s"
            " GROUP BY stock_code",
            (start, end),
        )
        return {
            r[0]: {"count": r[1], "name": r[2], "boards": r[3]}
            for r in cur.fetchall()
        }


def _today_boards(conn, trade_date: str) -> dict[str, int]:
    """当日涨停连板数 {stock_code: consecutive_boards}（并发算字段来源）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, MAX(consecutive_boards) FROM zt_pool"
            " WHERE pool_type = 'zt' AND trade_date = %s GROUP BY stock_code",
            (trade_date,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def _stock_names(conn, codes: list[str]) -> dict[str, str]:
    """stock_list 补名称；不在清单内的（防御）缺省 None。"""
    if not codes:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, stock_name FROM stock_list"
            f" WHERE stock_code IN ({', '.join(['%s'] * len(codes))})",
            codes,
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def compute_strong_rows(conn, trade_date: str) -> list[dict] | None:
    """库内自算强势股行（STRONG_FIELDS 键集）；输入缺失（stock_daily 当日无
    数据）返回 None，调用方按 no_data_for_date 正常语义处理。"""
    adj = select_dominant_adj(conn, trade_date)
    if adj is None:
        return None
    high_start = _window_anchor(conn, trade_date, HIGH_WINDOW_DAYS)
    zt_start = _window_anchor(conn, trade_date, MULTI_ZT_WINDOW_DAYS)
    if high_start is None or zt_start is None:
        return None

    today = _today_rows(conn, trade_date, adj)
    max_close = _window_max_close(conn, adj, high_start, trade_date)
    zt_stats = _zt_stats(conn, zt_start, trade_date)
    today_boards = _today_boards(conn, trade_date)

    selected: dict[str, dict] = {}
    for code, row in today.items():
        close = row["close"]
        is_high = close is not None and max_close.get(code) is not None \
            and close >= max_close[code]
        stat = zt_stats.get(code)
        multi_zt = stat is not None and stat["count"] >= MULTI_ZT_MIN_COUNT
        if not (is_high or multi_zt):
            continue
        reasons = []
        if is_high:
            reasons.append("60日新高")
        if multi_zt:
            reasons.append(f"多次涨停（近{MULTI_ZT_WINDOW_DAYS}日 {stat['count']} 次）")
        selected[code] = {
            "stock_code": code,
            "stock_name": None,  # 下方统一从 stock_list/zt_pool 补
            "latest_price": close,
            "change_percent": row["change_percent"],
            "volume": row["volume"],
            "amount": row["amount"],
            "turnover_rate": row["turnover_rate"],
            "market_cap": None,  # 库内无个股总市值真源（快照市值不落本表），不伪造
            "consecutive_boards": today_boards.get(code),
            "industry": None,
            "reason": "+".join(reasons),
            "_zt_name": stat["name"] if stat else None,
        }
    names = _stock_names(conn, list(selected))
    rows = []
    for code, row in selected.items():
        row["stock_name"] = names.get(code) or row.pop("_zt_name")
        row.pop("_zt_name", None)
        rows.append(row)
    rows.sort(key=lambda r: r["stock_code"])
    log.info("strong_stocks 库内自算 %s: %d 只（60日新高/多次涨停口径）",
             trade_date, len(rows))
    return rows
