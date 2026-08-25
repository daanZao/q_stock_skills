"""头尾分段规则：只补请求区间的头部和尾部缺口，中段缺口视为停牌/非交易日。

输入为库内已有的交易日集合与请求区间 [start, end]（yyyymmdd，字典序即可比较）；
输出为需要补抓的分段列表（最多两段）。
"""

from .dates import add_days


def head_tail_gaps(
    existing_dates: set[str], start: str, end: str
) -> list[tuple[str, str]]:
    if not existing_dates:
        return [(start, end)]
    earliest, latest = min(existing_dates), max(existing_dates)
    segments = []
    if earliest > start:
        segments.append((start, add_days(earliest, -1)))
    if latest < end:
        segments.append((add_days(latest, 1), end))
    return segments
