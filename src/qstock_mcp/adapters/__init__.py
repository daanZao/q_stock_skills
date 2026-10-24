"""数据源适配层：真实适配器与测试 fake 共用同一协议（见 base.py）。"""

import socket

# 全局 socket 超时（issue #88，spec #86）：efinance/akshare 不透传 timeout，
# requests/urllib3 在 timeout=None 时回落到 socket 默认值；不设则上游连接
# 挂死可无限阻塞（2026-10-19 实测 backfill 进程 99 分钟仅 3.5s CPU）。
# 60s 不误伤 PG（本地毫秒级）与 baostock 一把抓（实测 ~20s）。
SOCKET_TIMEOUT_S = 60

socket.setdefaulttimeout(SOCKET_TIMEOUT_S)

from .base import (
    BAR_FIELDS,
    SNAPSHOT_FIELDS,
    STOCK_LIST_FIELDS,
    AStockDailyAdapter,
    BoardAdapter,
    BoardConsAdapter,
    BoardHistoryAdapter,
    DailyAdapter,
    DataAdapter,
    FetchError,
    FundamentalsAdapter,
    IndexDailyAdapter,
    ListAdapter,
    SnapshotAdapter,
    fetch_failure_kind,
    is_bse_code,
    json_safe,
)

__all__ = [
    "BAR_FIELDS",
    "SNAPSHOT_FIELDS",
    "STOCK_LIST_FIELDS",
    "AStockDailyAdapter",
    "BoardAdapter",
    "BoardConsAdapter",
    "BoardHistoryAdapter",
    "DailyAdapter",
    "DataAdapter",
    "FetchError",
    "FundamentalsAdapter",
    "IndexDailyAdapter",
    "ListAdapter",
    "SnapshotAdapter",
    "fetch_failure_kind",
    "is_bse_code",
    "json_safe",
    "default_adapters",
    "default_daily_adapters",
    "default_fundamentals_adapters",
    "default_lhb_adapters",
]


def default_adapters() -> list[DataAdapter]:
    """盘面/快照等能力面的 fallback 链：efinance → akshare → baostock（懒加载真实库）。

    个股日线不走本链——见 default_daily_adapters（issue #112 东财退出日线）。
    """
    from .akshare_adapter import AkshareAdapter
    from .baostock_adapter import BaostockAdapter
    from .efinance_adapter import EfinanceAdapter

    return [EfinanceAdapter(), AkshareAdapter(), BaostockAdapter()]


def default_daily_adapters() -> list[DailyAdapter]:
    """个股日线 fallback 链（issue #112，spec #111）：腾讯 → 新浪 → baostock。

    顺序依据 #104 收盘后实测：腾讯/新浪当晚均含当日行（T+0）；腾讯原生换手率、
    amount 万元口径、北交所 920xxx 可取，故列首；新浪作备选（全历史下载慢、
    上游明示易封 IP）；baostock 兜底保留 tradestatus 权威停牌列与 turn 换手率。
    东财链路（efinance / akshare *_em）退出个股日线。
    """
    from .baostock_adapter import BaostockAdapter
    from .sina_adapter import SinaAdapter
    from .tencent_adapter import TencentAdapter

    return [TencentAdapter(), SinaAdapter(), BaostockAdapter()]


def default_fundamentals_adapters() -> list[FundamentalsAdapter]:
    """基本面透传（issue #6）fallback 链：akshare → efinance → baostock。

    与日线链顺序不同：按数据丰富度排序，akshare 提供财务指标序列，
    efinance 仅基础/估值快照，baostock 兜底最近季度利润/成长数据。
    """
    from .akshare_adapter import AkshareAdapter
    from .baostock_adapter import BaostockAdapter
    from .efinance_adapter import EfinanceAdapter

    return [AkshareAdapter(), EfinanceAdapter(), BaostockAdapter()]


def default_lhb_adapters() -> list[BoardAdapter]:
    """lhb section 专用链（issue #114，spec #111）：交易所官方 → akshare（东财 legacy）。

    主源为沪深北交易所一手披露（深交所 ShowReport JSON + 上交所定宽文本 +
    北交所 bseinfo.net JSONP，见 lhb_exchange_adapter）；akshare（东财 EM
    lhb 函数）留作链上兜底，待 spec 清理 ticket 退役。其余 section 不走本链。
    """
    from .akshare_adapter import AkshareAdapter
    from .lhb_exchange_adapter import ExchangeLhbAdapter

    return [ExchangeLhbAdapter(), AkshareAdapter()]
