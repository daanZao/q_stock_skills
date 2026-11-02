"""数据源适配层：真实适配器与测试 fake 共用同一协议（见 base.py）。"""

import socket

# 全局 socket 超时（issue #88，spec #86）：akshare 等第三方封装不透传 timeout，
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
    DailyAdapter,
    DataAdapter,
    FetchError,
    FundamentalsAdapter,
    IndexDailyAdapter,
    ListAdapter,
    SnapshotAdapter,
    SwBoardsAdapter,
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
    "DailyAdapter",
    "DataAdapter",
    "FetchError",
    "FundamentalsAdapter",
    "IndexDailyAdapter",
    "ListAdapter",
    "SnapshotAdapter",
    "SwBoardsAdapter",
    "fetch_failure_kind",
    "is_bse_code",
    "json_safe",
    "default_adapters",
    "default_daily_adapters",
    "default_snapshot_adapters",
    "default_fundamentals_adapters",
    "default_index_daily_adapters",
    "default_index_spot_adapters",
    "default_lhb_adapters",
    "default_boards_adapters",
]


def default_adapters() -> list[DataAdapter]:
    """股票清单 fallback 链（issue #121 定稿）：akshare → baostock（懒加载真实库）。

    efinance 随东财链路整体退役（issue #121，spec #111 收口）：本链此前承载
    盘面/快照等多面，各面迁移完成后（个股日线 #112 / 快照 #116 / 指数 #117 /
    boards #118 / zt_pool·strong_stocks #120 / lhb #114+#121）仅剩股票清单
    消费本链——akshare 交易所官网源（stock_info_a_code_name，含北交所）→
    baostock（query_stock_basic）兜底。清单以外勿挂本链，各面专用链见下。
    """
    from .akshare_adapter import AkshareAdapter
    from .baostock_adapter import BaostockAdapter

    return [AkshareAdapter(), BaostockAdapter()]


def default_snapshot_adapters() -> list[SnapshotAdapter]:
    """全市场快照 fallback 链（issue #116，spec #111）：腾讯批量报价 →
    腾讯 rank 榜单 → 新浪直连。

    主源 qt.gtimg.cn 批量报价 18 字段全覆盖、含北交所、时间戳含日期可过 #78
    日期防线，需股票清单驱动（默认 akshare 交易所官网清单 → 库内兜底）；
    rank 榜单免清单但缺 OHLC/pre_close（涨跌家数口径改由 zdf 判定）；新浪
    直连绕过 akshare 封装（其丢弃 7 字段）。东财链路（akshare spot_em）退出
    快照。详见 docs/research/2026-10-20-snapshot-alternatives.md。
    """
    from .sina_adapter import SinaSnapshotAdapter
    from .tencent_adapter import TencentRankAdapter, TencentSnapshotAdapter

    return [TencentSnapshotAdapter(), TencentRankAdapter(), SinaSnapshotAdapter()]


def default_daily_adapters() -> list[DailyAdapter]:
    """个股日线 fallback 链（issue #112，spec #111）：腾讯 → 新浪 → baostock。

    顺序依据 #104 收盘后实测：腾讯/新浪当晚均含当日行（T+0）；腾讯原生换手率、
    amount 万元口径、北交所 920xxx 可取，故列首；新浪作备选（全历史下载慢、
    上游明示易封 IP）；baostock 兜底保留 tradestatus 权威停牌列与 turn 换手率。
    东财链路（akshare *_em 系列）退出个股日线。
    """
    from .baostock_adapter import BaostockAdapter
    from .sina_adapter import SinaAdapter
    from .tencent_adapter import TencentAdapter

    return [TencentAdapter(), SinaAdapter(), BaostockAdapter()]


def default_index_spot_adapters() -> list[BoardAdapter]:
    """指数快照 fallback 链（issue #117，spec #111）：腾讯 → 新浪。

    腾讯 qt.gtimg.cn 批量报价为唯一覆盖量比（volume_ratio）的源，单请求 7 大
    指数一次取齐（含科创50）；新浪 hq.sinajs.cn 批量报价为备选（无量比，沪深
    成交量单位不一致已在映射层处理）。东财链路（akshare 沪深重要指数）随 #117 退出指数快照面；
    baostock 无指数快照接口（unsupported，不入链）。
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
    （valuation）；base_info 随东财链路退役（planned_absent，见
    deprecations，#113 第四态机制接管）；baostock 兜底最近季度
    profit/growth。
    """
    from .akshare_adapter import AkshareAdapter
    from .baostock_adapter import BaostockAdapter

    return [AkshareAdapter(), BaostockAdapter()]


def default_lhb_adapters() -> list[BoardAdapter]:
    """lhb section 专用链（issue #114/#121，spec #111 定稿）：交易所官方单源。

    主源为沪深北交易所一手披露（深交所 ShowReport JSON + 上交所定宽文本 +
    北交所 bseinfo.net JSONP，见 lhb_exchange_adapter），承载 lhb_basic 与
    同花顺营业部榜两子表；lhb_stock_statistic 由库内 lhb_basic 滚动自算
    （repository.recompute_lhb_statistic），不在链上。akshare 东财 lhb legacy
    兜底随 issue #121 摘除；新浪互补校验（含 ggtj 统计窗口）为对拍通道
    （`qstock-scheduler lhb --check`，不落库），不入 fallback 链。
    其余 section 不走本链。
    """
    from .lhb_exchange_adapter import ExchangeLhbAdapter

    return [ExchangeLhbAdapter()]


def default_lhb_history_adapters(ths=None) -> list[BoardAdapter]:
    """历史回溯专用：深圳同花顺主采，沪深北官方保留失败补缺能力。"""
    from .lhb_exchange_adapter import ExchangeLhbAdapter
    from .lhb_ths_adapter import ThsLhbAdapter

    return [ExchangeLhbAdapter(szse_primary=ths or ThsLhbAdapter())]


def default_boards_adapters() -> list[BoardAdapter]:
    """boards 行业段 fallback 链（issue #118，spec #111）：申万官网单源。

    申万一级（31）+二级（124）双级实时快照（收盘后末次快照即当日收盘截面）；
    东财 industry/concept 口径随剔东财退役（概念维度退役定稿见 #119，行业
    口径断代不混排）。申万故障时 daily-review 降级用库内自算
    query_sw_board_daily（spec 决策），故本链无第二承载源。
    """
    from .sw_adapter import SwSResearchAdapter

    return [SwSResearchAdapter()]
