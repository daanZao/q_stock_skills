"""同花顺涨停揭秘 dataapi 字段映射（issue #120，spec #111）。

纯函数，不 import 第三方库，测试无需触网。字段名以 2026-10-20 本机实测为准
（docs/research/2026-10-20-zt-pool-alternatives.md）：high_days/order_amount/
order_volume/open_num/first_limit_up_time/last_limit_up_time/reason_type/
limit_up_type/is_again_limit/limit_up_suc_rate/currency_value/turnover_rate/
latest/change_rate。THS 不提供：所属行业、总市值、成交额、成交量——对应列
置 None（不伪造）。

金额换算（ADR 0006）：order_amount（封单额，元）→ sealed_amount（万元）。
时间口径：first_limit_up_time（unix 秒）→ limit_up_time（"HHMMSS"，东八区），
与东财链路落库格式一致；缺失/0 置 None。
"""

import re
from datetime import datetime, timedelta, timezone

from ._num import _num

_TZ8 = timezone(timedelta(hours=8))

# high_days → 连板数："2天2板"→2、"首板"→1、"4天2板"（区间板）→取板数末位。
# 取最后一个 "N板" 的数字（连板高度以板数为准，不取天数）。
_HIGH_DAYS_RE = re.compile(r"(\d+)\s*板")


def parse_high_days(value) -> int | None:
    """high_days 文本 → 连板数；"首板"→1，"N天M板"取板数末位；无信息返回 None。"""
    if value is None:
        return None
    text = str(value)
    matches = _HIGH_DAYS_RE.findall(text)
    if matches:
        return int(matches[-1])
    return 1 if "首" in text else None


def parse_limit_time(value) -> str | None:
    """unix 秒 → "HHMMSS"（东八区）；缺失/0/非法返回 None。"""
    ts = _num(value)
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=_TZ8).strftime("%H%M%S")
    except (OverflowError, OSError, ValueError):
        return None


def map_ths_pool_rows(records: list[dict], pool_type: str) -> list[dict]:
    """THS 涨停揭秘 items → zt_pool 落库行 dict（pool_type ∈ zt/dt/zb）。

    THS 无行业字段 → industry 恒 None（归因题材走 reason_type，调研结论）；
    dt_price 上游无对应列（东财链路同样恒 None），置 None。dt 池首封时间/
    板型字段名为 first_limit_down_time/limit_down_type（limtupInfo 页面 JS
    资产），缺省回退 first_limit_up_time/limit_up_type。
    """
    rows = []
    for r in records:
        order_amount = _num(r.get("order_amount"))
        open_num = r.get("open_num")
        if pool_type == "dt":
            first_time = r.get("first_limit_down_time")
            board_type = r.get("limit_down_type")
        else:
            first_time = r.get("first_limit_up_time")
            board_type = r.get("limit_up_type")
        rows.append(
            {
                "pool_type": pool_type,
                "stock_code": str(r.get("code") or "").zfill(6),
                "stock_name": r.get("name"),
                "latest_price": _num(r.get("latest")),
                "change_percent": _num(r.get("change_rate")),
                "zt_price": _num(r.get("limit_up_price") or r.get("zt_price")),
                "volume": None,  # THS 不提供成交量（不伪造）
                "amount": None,  # THS 不提供成交额（不伪造）
                "limit_up_time": parse_limit_time(
                    first_time if first_time is not None
                    else r.get("first_limit_up_time")
                ),
                "limit_up_type": board_type or None,
                "consecutive_boards": parse_high_days(r.get("high_days")),
                "industry": None,  # THS 无所属行业（题材经 reason_type 承载）
                "dt_price": None,
                "zb_info": str(int(open_num)) if open_num not in (None, "") else None,
                "sealed_amount": order_amount / 10000 if order_amount else None,
                "reason_type": r.get("reason_type") or None,
            }
        )
    return rows
