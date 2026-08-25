"""日期工具：yyyymmdd 规格化与查询区间解析。

days 参数按"交易日 ≈ 自然日 7/5"加 buffer 换算成 start/end：
宁多抓（落库后后续查询自愈命中），不少抓导致尾部再补一轮。
"""

import math
from datetime import date, datetime, timedelta

_CALENDAR_PER_TRADING = 7 / 5
_DAYS_BUFFER = 10


def normalize_date(s: str) -> str:
    """接受 yyyymmdd 或 yyyy-mm-dd，统一返回 yyyymmdd。"""
    s = s.strip()
    if "-" in s:
        s = s.replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"无法识别的日期: {s!r}（支持 yyyymmdd 或 yyyy-mm-dd）")
    datetime.strptime(s, "%Y%m%d")  # 校验是真实日期
    return s


def add_days(yyyymmdd: str, n: int) -> str:
    d = datetime.strptime(yyyymmdd, "%Y%m%d").date() + timedelta(days=n)
    return d.strftime("%Y%m%d")


def resolve_range(
    days: int | None,
    start: str | None,
    end: str | None,
    *,
    today: str | None = None,
) -> tuple[str, str]:
    """把 days 或 start/end 参数解析为闭区间 [start, end]（yyyymmdd）。"""
    today = today or date.today().strftime("%Y%m%d")
    if days is not None:
        if start is not None or end is not None:
            raise ValueError("days 与 start/end 不可同时使用")
        if days <= 0:
            raise ValueError("days 必须为正整数")
        return add_days(today, -(math.ceil(days * _CALENDAR_PER_TRADING) + _DAYS_BUFFER)), today
    if start is None:
        raise ValueError("需要 days 或 start 参数")
    start, end = normalize_date(start), normalize_date(end) if end else today
    if start > end:
        raise ValueError(f"start({start}) 晚于 end({end})")
    return start, end
