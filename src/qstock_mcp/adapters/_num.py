"""数值与金额换算辅助（纯函数，不 import 第三方库，测试无需触网）。

换算收敛在适配器映射层（ADR 0006 金额万元 / ADR 0008 成交量股）：
"-" / "" / None 统一映射为 None（不伪造数值）。本模块原属 `_eastmoney.py`
的通用工具部分——东财列映射随 issue #121（spec #111 剔东财收口）整体退役后，
非东财源（tencent/sina/baostock/ths/sw/lhb 系）的映射继续复用此处。
"""

from typing import Any


def _num(v: Any) -> float | None:
    if v is None or v in ("-", ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def _wan(v: Any) -> float | None:
    """金额元 → 万元（全库金额统一规约到万元，ADR 0006）。"""
    n = _num(v)
    return None if n is None else n / 1e4


def _gu(v: Any) -> int | None:
    """成交量手 → 股（全库 volume 统一规约到股，ADR 0008）。"""
    n = _int(v)
    return None if n is None else n * 100


def _money_cn(v: Any) -> float | None:
    """中文金额字符串 → 万元："X亿"→×1e4，"X万"→原值，无单位纯数字按万元计
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
