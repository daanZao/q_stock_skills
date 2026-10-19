"""抓取 fallback 编排：按给定适配器顺序尝试，每源最多重试 2 次（最多 3 次尝试）。

只依赖适配器协议（name + fetch_daily / fetch_market_snapshot / fetch_<section> /
fetch_fundamentals / fetch_index_daily / fetch_board_history / fetch_board_cons），
不感知具体数据库/第三方库，测试注入 fake 适配器即可覆盖
fallback 顺序、重试次数、全失败报错。

空结果视为成功（该区间无交易日或停牌），不触发 fallback；
全失败抛 AllSourcesFailed，携带 attempted_sources（每源尝试次数、错误与
failure_kind 故障分类——三态契约 ADR 0007）。

重试韧性（issue #87，spec #86）：同源重试间按 RETRY_BACKOFF_S 指数退避
（±25% 抖动），避免对故障源立即重打；failure_kind=unsupported 的源为
by-design 不支持，单次即弃不重试。
"""

import random
from time import sleep
from typing import Callable, Sequence

from .adapters.base import (
    BoardAdapter,
    BoardConsAdapter,
    BoardHistoryAdapter,
    DailyAdapter,
    FundamentalsAdapter,
    IndexDailyAdapter,
    ListAdapter,
    SnapshotAdapter,
    fetch_failure_kind,
)

DEFAULT_MAX_RETRIES = 2

# 同源重试退避基数（秒，issue #87）：第 i 次重试前睡 RETRY_BACKOFF_S[i-1]
# × ±25% 抖动；超出序列长度时沿用末值
RETRY_BACKOFF_S = (0.5, 1.0, 2.0)

UPSTREAM_RETRY_ADVICE = (
    "连接级故障通常为临时性，建议稍后重试；"
    "持续失败请按 attempted_sources 逐源检查数据源可达性"
)


class AllSourcesFailed(RuntimeError):
    """所有数据源在给定分段上均失败。

    attempted 为每源的 {source, attempts, error, failure_kind}；failure_kind 取值
    unsupported / connection / other（ADR 0007）；单 code 指数补漏路径
    （fetch_index_spot_with_fallback）另有 empty——源口径不含该指数，
    与 unsupported 一样不算抓取故障。
    """

    def __init__(self, attempted: list[dict]):
        self.attempted = attempted
        summary = "; ".join(f"{a['source']}: {a['error']}" for a in attempted)
        super().__init__(f"全部数据源失败（{summary}）")


def aggregate_error_kind(attempted: list[dict]) -> str:
    """逐源 failure_kind 聚合 error_kind（ADR 0007）。

    by-design 不支持（unsupported）的源不算抓取故障、不参与判定；其余源中
    任一连接级故障即 upstream_unreachable，否则 fetch_failed。
    """
    failures = [a for a in attempted if a["failure_kind"] != "unsupported"]
    if any(a["failure_kind"] == "connection" for a in failures):
        return "upstream_unreachable"
    return "fetch_failed"


def aggregate_patch_error_kind(attempted: list[dict]) -> str:
    """单 code 指数补漏的 error_kind 聚合（issue #49，复用 ADR 0007 三态命名）。

    empty（源口径不含该指数）与 unsupported 一样不算抓取故障；其余源中任一
    连接级故障即 upstream_unreachable，有非连接级失败即 fetch_failed；
    全部 empty/unsupported 为 no_data_for_date（白名单指数当日全源无行情，
    正常语义，但覆盖目标未达成，由工具层列入 patch failed 明细）。
    """
    failures = [
        a for a in attempted if a["failure_kind"] not in ("unsupported", "empty")
    ]
    if any(a["failure_kind"] == "connection" for a in failures):
        return "upstream_unreachable"
    if failures:
        return "fetch_failed"
    return "no_data_for_date"


def _with_fallback(adapters, call: Callable, max_retries: int) -> dict:
    """按顺序尝试各数据源，成功返回 {result, source, attempted_sources}。

    同源重试间按 RETRY_BACKOFF_S 退避（±25% 抖动）；failure_kind=unsupported
    的源单次即弃（by-design 不支持，重试无意义，issue #87）。"""
    attempted: list[dict] = []
    for adapter in adapters:
        error = None
        kind = None
        attempts = 0
        for attempt in range(max_retries + 1):
            if attempt:
                base = RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)]
                sleep(base * random.uniform(0.75, 1.25))
            attempts += 1
            try:
                result = call(adapter)
                return {
                    "result": result,
                    "source": adapter.name,
                    "attempted_sources": attempted,
                }
            except Exception as e:  # noqa: BLE001 - 适配器任何失败都收敛为"该源失败"
                error = str(e)
                kind = fetch_failure_kind(e)
                if kind == "unsupported":
                    break
        attempted.append(
            {
                "source": adapter.name,
                "attempts": attempts,
                "error": error,
                "failure_kind": kind,
            }
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


def fetch_index_spot_with_fallback(
    adapters: Sequence[BoardAdapter],
    index_code: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """单 code 指数行情补抓（issue #49 白名单定点补漏），成功返回
    {rows, source, attempted_sources}。

    与 fetch_section_with_fallback 不同：空结果（源口径不含该指数，如 efinance
    沪深系列指数口径系统性排除科创板系列）不视为成功，立即转下一源并记
    failure_kind "empty"（口径性缺失，重试无意义）；异常仍按每源最多重试 2 次。
    全源无果抛 AllSourcesFailed，error_kind 由 aggregate_patch_error_kind 判定。
    """
    attempted: list[dict] = []
    for adapter in adapters:
        error = None
        kind = None
        attempts = 0
        for _ in range(max_retries + 1):
            attempts += 1
            try:
                rows = adapter.fetch_index_spot(index_code)
                if rows:
                    return {
                        "rows": rows,
                        "source": adapter.name,
                        "attempted_sources": attempted,
                    }
                error = "源口径不含该指数"
                kind = "empty"
                break
            except Exception as e:  # noqa: BLE001 - 适配器任何失败都收敛为"该源失败"
                error = str(e)
                kind = fetch_failure_kind(e)
        attempted.append(
            {
                "source": adapter.name,
                "attempts": attempts,
                "error": error,
                "failure_kind": kind,
            }
        )
    raise AllSourcesFailed(attempted)


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


def fetch_board_history_with_fallback(
    adapters: Sequence[BoardHistoryAdapter],
    board_type: str,
    board_name: str,
    start: str,
    end: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """板块历史 K 线抓取（issue #84 回补），成功返回 {rows, source, attempted_sources}。"""
    r = _with_fallback(
        adapters,
        lambda a: a.fetch_board_history(board_type, board_name, start, end),
        max_retries,
    )
    return {
        "rows": r["result"],
        "source": r["source"],
        "attempted_sources": r["attempted_sources"],
    }


def fetch_board_cons_with_fallback(
    adapters: Sequence[BoardConsAdapter],
    board_name: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """概念板块成分抓取（issue #84），成功返回 {rows, source, attempted_sources}。"""
    r = _with_fallback(
        adapters,
        lambda a: a.fetch_board_cons(board_name),
        max_retries,
    )
    return {
        "rows": r["result"],
        "source": r["source"],
        "attempted_sources": r["attempted_sources"],
    }
