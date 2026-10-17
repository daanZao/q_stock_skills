"""东方财富系列映射：efinance 与 akshare(stock_zh_a_hist) 同为东财中文列，共用此映射。

纯函数，不 import 第三方库，测试无需触网。"-" / "" / None 统一映射为 None（不伪造数值）。

金额单位：全库金额统一规约为万元（ADR 0006）——东财原始列为元，经 _wan 换算；
中文金额字符串（"X亿"/"X万"/无单位）经 _money_cn 解析为万元。
成交量单位：全库 volume 统一规约为股（ADR 0008）——东财原始列为手，经 _gu
×100 换算；baostock 原生为股，透传不换算。

全市场快照（issue #4）：efinance(get_realtime_quotes) 与 akshare(stock_zh_a_spot_em)
列名略有差异（昨收/昨日收盘、市盈率-动态/动态市盈率），map_spot_rows 兼容两种；
efinance 无振幅/市净率列，对应字段为 None（不伪造）。
快照无逐行交易日，spot_trade_date 从 efinance 的"最新交易日"列提取（akshare 无此列 → None）。
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


def _wan(v):
    """金额元 → 万元（全库金额统一规约到万元，ADR 0006）。"""
    n = _num(v)
    return None if n is None else n / 1e4


def _gu(v):
    """成交量手 → 股（全库 volume 统一规约到股，ADR 0008；东财原始列为手）。"""
    n = _int(v)
    return None if n is None else n * 100


def _money_cn(v):
    """东财中文金额字符串 → 万元："X亿"→×1e4，"X万"→原值，无单位纯数字按万元计
    （上游实测推断：无单位值与同行"万"档量级自洽，ADR 0006）。"""
    if v is None:
        return None
    s = str(v).strip()
    if s.endswith("亿"):
        n = _num(s[:-1])
        return None if n is None else n * 1e4
    if s.endswith("万"):
        return _num(s[:-1])
    return _num(s)


def _first(r: dict, *keys: str):
    for k in keys:
        if k in r:
            return r[k]
    return None


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
                "volume": _gu(r.get("成交量")),
                "amount": _wan(r.get("成交额")),
                "amplitude": _num(r.get("振幅")),
                "change_percent": _num(r.get("涨跌幅")),
                "change_amount": _num(r.get("涨跌额")),
                "turnover_rate": _num(r.get("换手率")),
            }
        )
    return bars


def map_spot_rows(records: list[dict]) -> list[dict]:
    """东财全市场快照记录 → SNAPSHOT_FIELDS 结构的 dict（不含 trade_date）。"""
    rows = []
    for r in records:
        code = _first(r, "代码", "股票代码")
        rows.append(
            {
                "stock_code": None if code is None else str(code),
                "stock_name": _first(r, "名称", "股票名称"),
                "latest_price": _num(r.get("最新价")),
                "change_percent": _num(r.get("涨跌幅")),
                "change_amount": _num(r.get("涨跌额")),
                "amplitude": _num(r.get("振幅")),
                "high": _num(r.get("最高")),
                "low": _num(r.get("最低")),
                "open": _num(r.get("今开")),
                "pre_close": _num(_first(r, "昨收", "昨日收盘")),  # akshare/efinance
                "volume_ratio": _num(r.get("量比")),
                "turnover_rate": _num(r.get("换手率")),
                "pe_ratio": _num(_first(r, "市盈率-动态", "动态市盈率", "市盈率(动态)")),
                "pb_ratio": _num(r.get("市净率")),
                "volume": _gu(r.get("成交量")),
                "amount": _wan(r.get("成交额")),
                "market_cap": _wan(r.get("总市值")),
                "float_cap": _wan(r.get("流通市值")),
            }
        )
    return rows


def spot_trade_date(records: list[dict]) -> str | None:
    """从快照记录的"最新交易日"列提取交易日（yyyymmdd）；无此列返回 None。"""
    dates = set()
    for r in records:
        v = r.get("最新交易日")
        if v is None or v in ("-", ""):
            continue
        s = str(v).replace("-", "").strip()
        if len(s) == 8 and s.isdigit():
            dates.add(s)
    return max(dates) if dates else None
