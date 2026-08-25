"""数据源适配层：真实适配器与测试 fake 共用同一协议（见 base.py）。"""

from .base import (
    BAR_FIELDS,
    SNAPSHOT_FIELDS,
    DailyAdapter,
    DataAdapter,
    FetchError,
    SnapshotAdapter,
    is_bse_code,
)

__all__ = [
    "BAR_FIELDS",
    "SNAPSHOT_FIELDS",
    "DailyAdapter",
    "DataAdapter",
    "FetchError",
    "SnapshotAdapter",
    "is_bse_code",
    "default_adapters",
]


def default_adapters() -> list[DataAdapter]:
    """生产环境的 fallback 链：efinance → akshare → baostock（懒加载真实库）。"""
    from .akshare_adapter import AkshareAdapter
    from .baostock_adapter import BaostockAdapter
    from .efinance_adapter import EfinanceAdapter

    return [EfinanceAdapter(), AkshareAdapter(), BaostockAdapter()]
