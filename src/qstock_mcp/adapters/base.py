"""数据源适配层接缝：协议 + 错误类型 + 代码工具。

真实适配器（tencent/sina/efinance/akshare/baostock）与测试注入的 fake 都实现
同一协议：
    name: str
    fetch_daily(stock_code, start, end, adj) -> list[bar]
bar 为 dict，键见 BAR_FIELDS；trade_date 为 yyyymmdd 字符串，数值缺失为 None。
失败一律抛 FetchError；返回空列表表示该区间无数据（停牌/非交易日），不是错误。

全市场快照（issue #4）走另一协议：fetch_market_snapshot() 一次调用返回
{"trade_date": str | None, "rows": [snapshot]}；trade_date 为 None 表示 API 未报告，
由工具层回退到传入日期/当天。snapshot 键见 SNAPSHOT_FIELDS。

init 轻量初始化（issue #8）新增两协议：fetch_stock_list() 返回股票清单行
（键见 STOCK_LIST_FIELDS）；fetch_index_daily(index_code, start, end) 返回
指数日线 bar（键同 BAR_FIELDS，无复权概念）。
"""

from typing import Protocol

BAR_FIELDS = (
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "change_percent",
    "change_amount",
    "turnover_rate",
)

SNAPSHOT_FIELDS = (
    "stock_code",
    "stock_name",
    "latest_price",
    "change_percent",
    "change_amount",
    "amplitude",
    "high",
    "low",
    "open",
    "pre_close",
    "volume_ratio",
    "turnover_rate",
    "pe_ratio",
    "pb_ratio",
    "volume",
    "amount",
    "market_cap",
    "float_cap",
)

# 股票清单（issue #8）：fetch_stock_list 行 dict 的键集，与 stock_list 表对应
STOCK_LIST_FIELDS = ("stock_code", "stock_name")

# 盘面快照（issue #5）：各 section 行 dict 的键集，与落库表列一一对应（日期列除外，
# 见 repository.BOARD_SECTION_TABLES）。行可自带日期键（trade_date/fetch_date）覆盖
# 工具层传入的日期（如 lhb_basic 的"上榜日"、efinance 指数的"最新交易日"）。
INDEX_FIELDS = (
    "index_code",
    "index_name",
    "latest_price",
    "change_percent",
    "change_amount",
    "volume",
    "amount",
    "amplitude",
    "high",
    "low",
    "open",
    "pre_close",
    "volume_ratio",
)

BOARD_FIELDS = (
    "board_type",
    "board_code",
    "board_name",
    "latest_price",
    "change_percent",
    "change_amount",
    "volume",
    "amount",
    "amplitude",
    "high",
    "low",
    "open",
    "pre_close",
    "volume_ratio",
    "stock_count",
    "leading_stock",
    "leading_change",
)

ZT_POOL_FIELDS = (
    "pool_type",
    "stock_code",
    "stock_name",
    "latest_price",
    "change_percent",
    "zt_price",
    "volume",
    "amount",
    "limit_up_time",
    "limit_up_type",
    "consecutive_boards",
    "industry",
    "dt_price",
    "zb_info",
    "sealed_amount",
    "reason_type",
)

STRONG_FIELDS = (
    "stock_code",
    "stock_name",
    "latest_price",
    "change_percent",
    "volume",
    "amount",
    "turnover_rate",
    "market_cap",
    "consecutive_boards",
    "industry",
    "reason",
)

LHB_BASIC_FIELDS = (
    "stock_code",
    "stock_name",
    "close_price",
    "change_percent",
    "turnover_rate",
    "lhb_reason",
    "net_buy_amount",
    "buy_amount",
    "sell_amount",
    "total_amount",
)

LHB_STATISTIC_FIELDS = (
    "stock_code",
    "stock_name",
    "appear_count_3m",
    "buy_amount_3m",
    "sell_amount_3m",
    "net_buy_3m",
    "buy_seat_count",
    "sell_seat_count",
)

LHB_YYB_CAPITAL_FIELDS = (
    "rank",
    "seat_name",
    "total_amount",
    "buy_amount",
    "sell_amount",
    "net_buy_amount",
    "avg_amount_per_trade",
)

LHB_YYB_MOST_FIELDS = (
    "rank",
    "seat_name",
    "appear_count",
    "buy_amount",
    "buy_count",
    "sell_amount",
    "sell_count",
    "net_buy_amount",
)


class FetchError(RuntimeError):
    """单个数据源抓取失败（库未安装、网络错误、接口报错、代码不支持等）。

    kind 为显式故障分类（三态契约，ADR 0007）："unsupported" 表示该源按设计
    不支持此能力（如 baostock 无全市场快照），不算抓取故障；缺省 None 由
    fetch_failure_kind 沿异常链自动判定。
    """

    def __init__(self, msg: str, *, kind: str | None = None):
        self.kind = kind
        super().__init__(msg)


def import_akshare():
    """akshare 懒加载（可选依赖 [sources]）：未安装时抛 FetchError。

    akshare 只在真实抓取路径用到，测试注入 fake 不触第三方库，故 import 推迟
    到调用点。akshare_adapter 与 sina_adapter 共用此入口（issue #112）。
    """
    try:
        import akshare as ak
    except ImportError as e:
        raise FetchError("akshare 未安装：pip install qstock-mcp[sources]") from e
    return ak


# 连接级故障（ADR 0007）：请求未获得任何 HTTP 响应——连接重置、TLS 后空响应、
# 超时、DNS/建连失败。按异常类名鸭子类型判定，不 import requests/urllib3（可选依赖）；
# 覆盖标准库、http.client、requests、urllib3 的对应类名。
_CONNECTION_ERROR_NAMES = frozenset(
    {
        # 标准库 / socket
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "TimeoutError",
        "timeout",
        "gaierror",
        # http.client
        "RemoteDisconnected",
        "IncompleteRead",
        "BadStatusLine",
        # urllib（issue #112 腾讯薄封装）：URLError 表示请求未拿到响应
        # （DNS/连接重置/TLS 断连/超时），HTTPError 是其子类但类名不同，
        # 已收到状态码的失败仍归 other
        "URLError",
        # requests / urllib3
        "ConnectTimeout",
        "ReadTimeout",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "MaxRetryError",
        "NewConnectionError",
        "NameResolutionError",
        "ProtocolError",
        "ChunkedEncodingError",
        "SSLError",
    }
)


def fetch_failure_kind(exc: BaseException) -> str:
    """抓取失败分类：unsupported / connection / other（ADR 0007 判定内核）。

    显式 kind（FetchError.kind）优先；否则沿 __cause__/__context__ 链按类名
    判定连接级故障（请求未获得任何 HTTP 响应）；收到响应后的失败（含 4xx/5xx、
    解析错误）归 other。
    """
    for want_connection in (False, True):
        seen: set[int] = set()
        cur: BaseException | None = exc
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if want_connection:
                if type(cur).__name__ in _CONNECTION_ERROR_NAMES:
                    return "connection"
            elif isinstance(cur, FetchError) and cur.kind is not None:
                return cur.kind
            cur = cur.__cause__ or cur.__context__
    return "other"


class DailyAdapter(Protocol):
    name: str

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]: ...


class SnapshotAdapter(Protocol):
    name: str

    def fetch_market_snapshot(self) -> dict:
        """单次全市场快照 → {"trade_date": str | None, "rows": [snapshot dict]}。"""
        ...


class BoardAdapter(Protocol):
    """盘面快照（issue #5）协议：按 section 抓取，trade_date 为 yyyymmdd。

    前四个方法返回行 dict 列表（键见 *_FIELDS）；fetch_lhb 返回
    {"lhb_basic": [...], "lhb_stock_statistic": [...], "lhb_yyb_capital": [...],
     "lhb_yyb_most": [...]}；子项部分失败时额外带 "errors" 键（原因列表），
    工具层据此报告 partial_error。盘中/非交易日 lhb_basic 为空列表是正常语义
    （盘后发布），适配器应将其归一化为空列表而非抛错；不支持某 section 的源
    抛 FetchError。

    fetch_index_spot（issue #49 白名单定点补漏）：按单 code 取指数行情，返回
    0 或 1 行（键同 fetch_indices）；空列表表示源口径不含该指数（非错误，
    工具层转下一源），抓取失败抛 FetchError。
    """

    name: str

    def fetch_indices(self, trade_date: str) -> list[dict]: ...
    def fetch_index_spot(self, index_code: str) -> list[dict]: ...
    def fetch_boards(self, trade_date: str) -> list[dict]: ...
    def fetch_zt_pool(self, trade_date: str) -> list[dict]: ...
    def fetch_strong_stocks(self, trade_date: str) -> list[dict]: ...
    def fetch_lhb(self, trade_date: str) -> dict: ...


class FundamentalsAdapter(Protocol):
    """基本面透传（issue #6）协议：proxy 能力面，不规格化、不落库。

    fetch_fundamentals 返回 {section: 原始记录} 的透传 payload：section 名对应
    上游接口，记录保留上游原始字段名（仅做 JSON 安全化），工具层原样回传。
    失败抛 FetchError；空 payload（所有 section 均无数据）视为该源失败。
    """

    name: str

    def fetch_fundamentals(self, stock_code: str) -> dict: ...


class ListAdapter(Protocol):
    """股票清单（issue #8）协议：单次全市场代码/名称抓取。"""

    name: str

    def fetch_stock_list(self) -> list[dict]:
        """→ [{stock_code, stock_name}]（键见 STOCK_LIST_FIELDS）；失败抛 FetchError。"""
        ...


class IndexDailyAdapter(Protocol):
    """指数日线（issue #8）协议：bar 键同 BAR_FIELDS，trade_date 为 yyyymmdd。

    指数无复权概念，接口不带 adj 参数；返回空列表表示该区间无数据，不是错误。
    """

    name: str

    def fetch_index_daily(
        self, index_code: str, start: str, end: str
    ) -> list[dict]: ...


class BoardHistoryAdapter(Protocol):
    """板块历史 K 线（issue #84 回补）协议：start/end 为 yyyymmdd 闭区间。

    不复权（板块行情无复权语义）；空区间返回 [] 而非错误；by-design 不支持
    该能力的源抛 FetchError(kind="unsupported")（不参与 fallback 故障判定）。
    """

    name: str

    def fetch_board_history(
        self, board_type: str, board_name: str, start: str, end: str
    ) -> list[dict]: ...


class BoardConsAdapter(Protocol):
    """板块成分（issue #84 概念成分积累）协议：按板块名取成分股列表。

    返回行至少含 stock_code；空列表表示该板块无成分，不是错误；不支持的源
    抛 FetchError(kind="unsupported")。
    """

    name: str

    def fetch_board_cons(self, board_name: str) -> list[dict]: ...


class AStockDailyAdapter(Protocol):
    """全市场单日 A 股日线一把抓（issue #83 次晨批）协议：trade_date 为 yyyymmdd。

    单次调用返回当日全市场日线行（映射见 map_astock_daily_rows），adj='none'
    不复权口径；当日无数据返回 []。单源接口，不走 fallback 链。
    """

    name: str

    def fetch_astock_daily(self, trade_date: str) -> list[dict]: ...


class SwBoardsAdapter(Protocol):
    """申万官网板块能力（issue #118，spec #111）协议。

    SwSResearchAdapter 实现：boards 行业段主承载（fetch_boards 见
    BoardAdapter 协议）+ 调度 CLI 申万专属能力。申万单源无 fallback 链，
    失败一律抛 FetchError；行结构见 _sw_board 映射（落库单位万元/股，
    ADR 0006/0008）。

    fetch_sw_board_list：申万双级板块清单（board_code/board_name/level），
    回补与对拍的目标集。
    fetch_sw_board_history：trend 指数全历史（1999 起，T+1）按 board
    （board_code/board_name/level 的 dict）+ yyyymmdd 闭区间取数，行自带
    trade_date；空区间返回 []（非错误）。
    fetch_sw_board_analysis：每日分析报表（T+1）双级一把抓，yymmdd 闭区间，
    行键见 sw_board_valuation 表（换手率/PE/PB/流通市值/股息率）。
    fetch_sw_class_file：成分分类 xls 直链下载（成分字典在线刷新源）。
    """

    name: str

    def fetch_sw_board_list(self) -> list[dict]: ...

    def fetch_sw_board_history(
        self, board: dict, start: str, end: str
    ) -> list[dict]: ...

    def fetch_sw_board_analysis(self, start: str, end: str) -> list[dict]: ...

    def fetch_sw_class_file(self, url: str) -> bytes: ...


class DataAdapter(
    DailyAdapter,
    SnapshotAdapter,
    BoardAdapter,
    FundamentalsAdapter,
    ListAdapter,
    IndexDailyAdapter,
    BoardHistoryAdapter,
    BoardConsAdapter,
    Protocol,
):
    """同时支持日线、全市场快照、盘面快照、基本面透传、股票清单、指数日线与
    板块历史/成分的适配器（fallback 链元素类型）。"""


def is_bse_code(stock_code: str) -> bool:
    """北交所代码：4/8/9 开头（baostock 不支持，需明确拒绝）。

    baostock 适配器的粗粒度拒绝守卫；精确 A 股市场路由见
    `adapters/_bse_codes.market_of`（900xxx 沪 B 股不属北交所，ADR 0007 补记）。
    """
    return stock_code[:1] in ("4", "8", "9")


def json_safe(value):
    """上游值 → JSON 安全值（透传的序列化步骤，不改键名与语义）。

    NaN/NaT/Inf → None；numpy 标量 → Python 标量；日期时间 → ISO 字符串；
    dict/list 递归。不 import pandas/numpy（可选依赖），全部鸭子类型判定。
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    item = getattr(value, "item", None)  # numpy 标量
    if callable(item):
        try:
            return json_safe(item())
        except Exception:  # noqa: BLE001 - 序列化失败按 None 处理
            return None
    iso = getattr(value, "isoformat", None)  # date/datetime/Timestamp
    if callable(iso):
        try:
            return iso()
        except Exception:  # noqa: BLE001 - NaT 等不支持 isoformat
            return None
    return str(value)
