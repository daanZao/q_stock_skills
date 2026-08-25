"""抓取 fallback 编排：按给定适配器顺序尝试，每源最多重试 2 次（最多 3 次尝试）。

只依赖适配器协议（name + fetch_daily），不感知具体数据库/第三方库，
测试注入 fake 适配器即可覆盖 fallback 顺序、重试次数、全失败报错。

空结果视为成功（该区间无交易日或停牌），不触发 fallback；
全失败抛 AllSourcesFailed，携带 attempted_sources（每源尝试次数与最后错误）。
"""

from .adapters.base import DailyAdapter

DEFAULT_MAX_RETRIES = 2


class AllSourcesFailed(RuntimeError):
    """所有数据源在给定分段上均失败。attempted 为每源的 {source, attempts, error}。"""

    def __init__(self, attempted: list[dict]):
        self.attempted = attempted
        summary = "; ".join(f"{a['source']}: {a['error']}" for a in attempted)
        super().__init__(f"全部数据源失败（{summary}）")


def fetch_with_fallback(
    adapters: list[DailyAdapter],
    stock_code: str,
    start: str,
    end: str,
    adj: str = "qfq",
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """按顺序尝试各数据源，成功返回 {rows, source, attempted_sources}。"""
    attempted: list[dict] = []
    for adapter in adapters:
        error = None
        for _ in range(max_retries + 1):
            try:
                rows = adapter.fetch_daily(stock_code, start, end, adj)
                return {
                    "rows": rows,
                    "source": adapter.name,
                    "attempted_sources": attempted,
                }
            except Exception as e:  # noqa: BLE001 - 适配器任何失败都收敛为"该源失败"
                error = str(e)
        attempted.append(
            {"source": adapter.name, "attempts": max_retries + 1, "error": error}
        )
    raise AllSourcesFailed(attempted)
