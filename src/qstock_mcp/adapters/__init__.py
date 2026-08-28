"""数据源适配层：真实适配器与测试 fake 共用同一协议（见 base.py）。"""

from .base import (
    BAR_FIELDS,
    SNAPSHOT_FIELDS,
    BoardAdapter,
    DailyAdapter,
    DataAdapter,
    FetchError,
    FundamentalsAdapter,
    SnapshotAdapter,
    is_bse_code,
    json_safe,
)

__all__ = [
    "BAR_FIELDS",
    "SNAPSHOT_FIELDS",
    "BoardAdapter",
    "DailyAdapter",
    "DataAdapter",
    "FetchError",
    "FundamentalsAdapter",
    "SnapshotAdapter",
    "is_bse_code",
    "json_safe",
    "default_adapters",
    "default_fundamentals_adapters",
]


def default_adapters() -> list[DataAdapter]:
    """生产环境的 fallback 链：efinance → akshare → baostock（懒加载真实库）。"""
    from .akshare_adapter import AkshareAdapter
    from .baostock_adapter import BaostockAdapter
    from .efinance_adapter import EfinanceAdapter

    return [EfinanceAdapter(), AkshareAdapter(), BaostockAdapter()]


def default_fundamentals_adapters() -> list[FundamentalsAdapter]:
    """基本面透传（issue #6）fallback 链：akshare → efinance → baostock。

    与日线链顺序不同：按数据丰富度排序，akshare 提供财务指标序列，
    efinance 仅基础/估值快照，baostock 兜底最近季度利润/成长数据。
    """
    from .akshare_adapter import AkshareAdapter
    from .baostock_adapter import BaostockAdapter
    from .efinance_adapter import EfinanceAdapter

    return [AkshareAdapter(), EfinanceAdapter(), BaostockAdapter()]
