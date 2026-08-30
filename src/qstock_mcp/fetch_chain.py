"""抓取 fallback 编排：按给定适配器顺序尝试，每源最多重试 2 次（最多 3 次尝试）。

只依赖适配器协议（name + fetch_daily / fetch_market_snapshot / fetch_<section> /
fetch_fundamentals），不感知具体数据库/第三方库，测试注入 fake 适配器即可覆盖
fallback 顺序、重试次数、全失败报错。

空结果视为成功（该区间无交易日或停牌），不触发 fallback；
全失败抛 AllSourcesFailed，携带 attempted_sources（每源尝试次数与最后错误）。
"""

from typing import Callable, Sequence

from .adapters.base import (
    BoardAdapter,
    DailyAdapter,
    FundamentalsAdapter,
    IndexDailyAdapter,
    ListAdapter,
    SnapshotAdapter,
)

DEFAULT_MAX_RETRIES = 2


class AllSourcesFailed(RuntimeError):
    """所有数据源在给定分段上均失败。attempted 为每源的 {source, attempts, error}。"""

    def __init__(self, attempted: list[dict]):
        self.attempted = attempted
        summary = "; ".join(f"{a['source']}: {a['error']}" for a in attempted)
        super().__init__(f"全部数据源失败（{summary}）")


def _with_fallback(adapters, call: Callable, max_retries: int) -> dict:
    """按顺序尝试各数据源，成功返回 {result, source, attempted_sources}。"""
    attempted: list[dict] = []
    for adapter in adapters:
        error = None
        for _ in range(max_retries + 1):
            try:
                result = call(adapter)
                return {
                    "result": result,
                    "source": adapter.name,
                    "attempted_sources": attempted,
                }
            except Exception as e:  # noqa: BLE001 - 适配器任何失败都收敛为"该源失败"
                error = str(e)
        attempted.append(
            {"source": adapter.name, "attempts": max_retries + 1, "error": error}
        )
    raise AllSourcesFailed(attempted)


def fetch_with_fallback(
    adapters: Sequence[DailyAdapter],
    stock_code: str,
    start: str,
    end: str,
    adj: str = "qfq",
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """按顺序尝试各数据源，成功返回 {rows, source, attempted_sources}。"""
    r = _with_fallback(
        adapters,
        lambda a: a.fetch_daily(stock_code, start, end, adj),
        max_retries,
    )
    return {
        "rows": r["result"],
        "source": r["source"],
        "attempted_sources": r["attempted_sources"],
    }


def fetch_snapshot_with_fallback(
    adapters: Sequence[SnapshotAdapter],
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """单次全市场快照，成功返回 {trade_date, rows, source, attempted_sources}。"""
    r = _with_fallback(adapters, lambda a: a.fetch_market_snapshot(), max_retries)
    return {
        "trade_date": r["result"]["trade_date"],
        "rows": r["result"]["rows"],
        "source": r["source"],
        "attempted_sources": r["attempted_sources"],
    }


def fetch_fundamentals_with_fallback(
    adapters: Sequence[FundamentalsAdapter],
    stock_code: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """基本面透传（issue #6），成功返回 {data, source, attempted_sources}。

    data 为适配器返回的 {section: 原始记录} 透传 payload；无数据的源由适配器
    自行抛 FetchError（空 payload 不落到这里），全失败抛 AllSourcesFailed。
    """
    r = _with_fallback(
        adapters,
        lambda a: a.fetch_fundamentals(stock_code),
        max_retries,
    )
    return {
        "data": r["result"],
        "source": r["source"],
        "attempted_sources": r["attempted_sources"],
    }


def fetch_section_with_fallback(
    adapters: Sequence[BoardAdapter],
    section: str,
    trade_date: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """单 section 盘面抓取（issue #5），成功返回 {data, source, attempted_sources}。

    section 取值与 BoardAdapter 方法同名：indices/boards/zt_pool/strong_stocks/lhb。
    data 为行列表（lhb 为 {表名: 行列表}）；空结果视为成功，不触发 fallback。
    重试策略与日线共用 DEFAULT_MAX_RETRIES：每源最多重试 2 次（最多 3 次
    尝试）——issue #5 的"最多 3 次"按尝试次数理解（与 issue #13 措辞口径一致）。
    """
    r = _with_fallback(
        adapters,
        lambda a: getattr(a, f"fetch_{section}")(trade_date),
        max_retries,
    )
    return {
        "data": r["result"],
        "source": r["source"],
        "attempted_sources": r["attempted_sources"],
    }


def fetch_stock_list_with_fallback(
    adapters: Sequence[ListAdapter],
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """股票清单抓取（issue #8），成功返回 {rows, source, attempted_sources}。"""
    r = _with_fallback(adapters, lambda a: a.fetch_stock_list(), max_retries)
    return {
        "rows": r["result"],
        "source": r["source"],
        "attempted_sources": r["attempted_sources"],
    }


def fetch_index_daily_with_fallback(
    adapters: Sequence[IndexDailyAdapter],
    index_code: str,
    start: str,
    end: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """指数日线抓取（issue #8），成功返回 {rows, source, attempted_sources}。"""
    r = _with_fallback(
        adapters,
        lambda a: a.fetch_index_daily(index_code, start, end),
        max_retries,
    )
    return {
        "rows": r["result"],
        "source": r["source"],
        "attempted_sources": r["attempted_sources"],
    }
