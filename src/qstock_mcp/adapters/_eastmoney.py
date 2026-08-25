"""东方财富系列映射：efinance 与 akshare(stock_zh_a_hist) 同为东财中文列，共用此映射。

纯函数，不 import 第三方库，测试无需触网。"-" / "" / None 统一映射为 None（不伪造数值）。
"""


def _num(v):
    if v is None or v in ("-", ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    return None if n is None else int(n)


def map_eastmoney_rows(records: list[dict]) -> list[dict]:
    """东财中文列记录 → BAR_FIELDS 结构的 bar dict。"""
    bars = []
    for r in records:
        bars.append(
            {
                "trade_date": str(r["日期"]).replace("-", ""),
                "open": _num(r.get("开盘")),
                "high": _num(r.get("最高")),
                "low": _num(r.get("最低")),
                "close": _num(r.get("收盘")),
                "volume": _int(r.get("成交量")),
                "amount": _num(r.get("成交额")),
                "amplitude": _num(r.get("振幅")),
                "change_percent": _num(r.get("涨跌幅")),
                "change_amount": _num(r.get("涨跌额")),
                "turnover_rate": _num(r.get("换手率")),
            }
        )
    return bars
