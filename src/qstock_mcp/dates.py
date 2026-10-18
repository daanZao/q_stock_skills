"""日期工具：yyyymmdd 规格化、查询区间解析与 A 股交易日判定。

days 参数按"交易日 ≈ 自然日 7/5"加 buffer 换算成 start/end：
宁多抓（落库后后续查询自愈命中），不少抓导致尾部再补一轮。
换算落点是非交易日（周末/交易所公告休市日）时，start 向前对齐到最近
交易日，避免头尾自愈产生全非交易日的空抓分段（issue #51）。
"""

import math
from datetime import datetime, time, timedelta, timezone

_TZ8 = timezone(timedelta(hours=8))  # A 股语境东八区（无夏令时）

_CALENDAR_PER_TRADING = 7 / 5
_DAYS_BUFFER = 10

# A 股休市日（周末恒休市，不列入；只列落在工作日的节假日休市）。
# 2026 年取自上证公告〔2025〕45 号；表外日期按周末规则近似。
_MARKET_CLOSURES = frozenset(
    {
        # 2024
        "20240101",
        "20240209", "20240212", "20240213", "20240214", "20240215", "20240216",
        "20240404", "20240405",
        "20240501", "20240502", "20240503",
        "20240610",
        "20240916", "20240917",
        "20241001", "20241002", "20241003", "20241004", "20241007",
        # 2025
        "20250101",
        "20250128", "20250129", "20250130", "20250131", "20250203", "20250204",
        "20250404",
        "20250501", "20250502", "20250505",
        "20250602",
        "20251001", "20251002", "20251003", "20251006", "20251007", "20251008",
        # 2026
        "20260101", "20260102",
        "20260216", "20260217", "20260218", "20260219", "20260220", "20260223",
        "20260406",
        "20260501", "20260504", "20260505",
        "20260619",
        "20260925",
        "20261001", "20261002", "20261005", "20261006", "20261007",
    }
)


def normalize_date(s: str) -> str:
    """接受 yyyymmdd 或 yyyy-mm-dd，统一返回 yyyymmdd。"""
    s = s.strip()
    if "-" in s:
        s = s.replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"无法识别的日期: {s!r}（支持 yyyymmdd 或 yyyy-mm-dd）")
    datetime.strptime(s, "%Y%m%d")  # 校验是真实日期
    return s


def dash_date(yyyymmdd: str) -> str:
    """yyyymmdd → yyyy-mm-dd（上游 API 要求横线格式时的口径转换）。"""
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def add_days(yyyymmdd: str, n: int) -> str:
    d = datetime.strptime(yyyymmdd, "%Y%m%d").date() + timedelta(days=n)
    return d.strftime("%Y%m%d")


def is_trading_day(yyyymmdd: str) -> bool:
    """A 股交易日判定：非周末且不在已知休市表内。"""
    d = datetime.strptime(yyyymmdd, "%Y%m%d").date()
    return d.weekday() < 5 and yyyymmdd not in _MARKET_CLOSURES


def next_trading_day(yyyymmdd: str) -> str:
    """向前对齐到最近交易日（本身即交易日则原样返回）。"""
    while not is_trading_day(yyyymmdd):
        yyyymmdd = add_days(yyyymmdd, 1)
    return yyyymmdd


def prev_trading_day(yyyymmdd: str) -> str:
    """前一交易日（本身是否交易日不影响：恒向前取更早的交易日）。"""
    yyyymmdd = add_days(yyyymmdd, -1)
    while not is_trading_day(yyyymmdd):
        yyyymmdd = add_days(yyyymmdd, -1)
    return yyyymmdd


def today_str(now: datetime | None = None) -> str:
    """东八区当日 yyyymmdd；now 可注入（测试）。"""
    now = (now or datetime.now(_TZ8)).astimezone(_TZ8)
    return now.strftime("%Y%m%d")


# 交易时段（盘中禁写防线，issue #78）：交易日 09:30–15:00（东八区）
_SESSION_OPEN = time(9, 30)
_SESSION_CLOSE = time(15, 0)


def is_trading_time(now: datetime | None = None) -> bool:
    """交易时段判定：交易日的连续竞价时段内（09:30–15:00）为 True。"""
    now = (now or datetime.now(_TZ8)).astimezone(_TZ8)
    return is_trading_day(now.strftime("%Y%m%d")) and _SESSION_OPEN <= now.time() <= _SESSION_CLOSE


def resolve_range(
    days: int | None,
    start: str | None,
    end: str | None,
    *,
    today: str | None = None,
) -> tuple[str, str]:
    """把 days 或 start/end 参数解析为闭区间 [start, end]（yyyymmdd）。"""
    today = today or today_str()
    if days is not None:
        if start is not None or end is not None:
            raise ValueError("days 与 start/end 不可同时使用")
        if days <= 0:
            raise ValueError("days 必须为正整数")
        return next_trading_day(
            add_days(today, -(math.ceil(days * _CALENDAR_PER_TRADING) + _DAYS_BUFFER))
        ), today
    if start is None:
        raise ValueError("需要 days 或 start 参数")
    start, end = normalize_date(start), normalize_date(end) if end else today
    if start > end:
        raise ValueError(f"start({start}) 晚于 end({end})")
    return start, end
