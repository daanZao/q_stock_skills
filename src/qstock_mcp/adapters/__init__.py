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
    "default_snapshot_adapters",
    "default_fundamentals_adapters",
    "default_index_daily_adapters",
    "default_index_spot_adapters",
]


def default_adapters() -> list[DataAdapter]:
    """盘面/快照等能力面的 fallback 链：efinance → akshare → baostock（懒加载真实库）。

    个股日线不走本链——见 default_daily_adapters（issue #112 东财退出日线）；
    全市场快照也不走本链——见 default_snapshot_adapters（issue #116 东财
    退出快照）；indices section 与指数日线不走本链——见
    default_index_spot_adapters / default_index_daily_adapters（issue #117
    东财退出指数面）；本链仅服务 boards/zt_pool/strong_stocks/lhb 盘面
    section、清单与基本面等。
    """
    from .akshare_adapter import AkshareAdapter
    from .baostock_adapter import BaostockAdapter
    from .efinance_adapter import EfinanceAdapter

    return [EfinanceAdapter(), AkshareAdapter(), BaostockAdapter()]


def default_snapshot_adapters() -> list[SnapshotAdapter]:
    """全市场快照 fallback 链（issue #116，spec #111）：腾讯批量报价 →
    腾讯 rank 榜单 → 新浪直连。

    主源 qt.gtimg.cn 批量报价 18 字段全覆盖、含北交所、时间戳含日期可过 #78
    日期防线，需股票清单驱动（默认 akshare 交易所官网清单 → 库内兜底）；
    rank 榜单免清单但缺 OHLC/pre_close（涨跌家数口径改由 zdf 判定）；新浪
    直连绕过 akshare 封装（其丢弃 7 字段）。东财链路（efinance/akshare
    spot_em）退出快照。详见 docs/research/2026-10-20-snapshot-alternatives.md。
    """
    from .sina_adapter import SinaSnapshotAdapter
    from .tencent_adapter import TencentRankAdapter, TencentSnapshotAdapter

    return [TencentSnapshotAdapter(), TencentRankAdapter(), SinaSnapshotAdapter()]


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


def default_index_spot_adapters() -> list[BoardAdapter]:
    """指数快照 fallback 链（issue #117，spec #111）：腾讯 → 新浪。

    腾讯 qt.gtimg.cn 批量报价为唯一覆盖量比（volume_ratio）的源，单请求 7 大
    指数一次取齐（含科创50）；新浪 hq.sinajs.cn 批量报价为备选（无量比，沪深
    成交量单位不一致已在映射层处理）。东财链路（efinance 沪深系列指数 /
    akshare 沪深重要指数）随本 ticket 退出指数快照面；baostock 无指数快照
    接口（unsupported，不入链）。
    """
    from .sina_adapter import SinaAdapter
    from .tencent_adapter import TencentAdapter

    return [TencentAdapter(), SinaAdapter()]


def default_index_daily_adapters() -> list[IndexDailyAdapter]:
    """指数日线 fallback 链（issue #117，spec #111）：腾讯 → baostock。

    腾讯 K 线薄封装收盘后当晚含当日行（T+0）且覆盖科创50（000688，baostock
    实测返回 0 行）；baostock 保留为次晨权威复核与历史回溯底座（全历史、
    tradestatus/turn 列；无 000688）。两源经 index_daily.source 列并存对账。
    东财指数日线（akshare index_zh_a_hist）随本 ticket 退出；新浪指数日线
    无成交额列，不入链（人工备选，见 sina_adapter）。
    """
    from .baostock_adapter import BaostockAdapter
    from .tencent_adapter import TencentAdapter

    return [TencentAdapter(), BaostockAdapter()]


def default_fundamentals_adapters() -> list[FundamentalsAdapter]:
    """基本面透传（issue #6）fallback 链：akshare → baostock（issue #115）。

    akshare 提供新浪财务摘要（financial_abstract）+ 百度股市通估值日序列
    （valuation）；efinance base_info 随东财链路退役（planned_absent，见
    deprecations，#113 第四态机制接管）；baostock 兜底最近季度
    profit/growth。
    """
    from .akshare_adapter import AkshareAdapter
    from .baostock_adapter import BaostockAdapter

    return [AkshareAdapter(), BaostockAdapter()]
