"""北交所代码归一化（issue #112）：旧码 43x/83x/87x → 920 新码。

数据源：北交所官网《新旧代码对照表》（2025-10-09 存量 248 只全部切换完成），
仓库内置 `data/bse_code_map.csv`（old_code,new_code 两列，官方对照表逐行收录）。
映射规则不是简单公式：后三位相同时按上市时间递进第四位（如 832023→920023、
837023→920123），必须以对照表为准。

腾讯/新浪只认 `bj920xxx` 新码：旧码在腾讯侧冻结于迁码前（实测 430047 停在
2020、833171 停在 2022），继续用旧码抓当日行会得到空结果。本模块统一把旧码
归一到新码；对照表未覆盖的旧码原样返回，保留其冻结历史可取（不臆造新码）。

市场前缀口径与 repository 分市场统计一致：6→sh、0/3→sz、4/8/920→bj；
9 开头非 920（900xxx 沪 B 股）与 2 开头（200xxx 深 B 股）明确拒绝。
"""

import csv
from functools import lru_cache
from importlib import resources

from .base import FetchError

_MARKET_PREFIXES = ("sh", "sz", "bj")


@lru_cache(maxsize=1)
def bse_code_map() -> dict[str, str]:
    """北交所官方旧码 → 920 新码对照表（248 条，进程内缓存）。"""
    seed = resources.files("qstock_mcp").joinpath("data/bse_code_map.csv")
    with seed.open(encoding="utf-8") as f:
        return {r["old_code"]: r["new_code"] for r in csv.DictReader(f)}


def _bare_code(stock_code: str) -> str:
    """剥掉可选的 sh/sz/bj 前缀，返回裸代码字符串。"""
    s = str(stock_code).strip().lower()
    if len(s) > 2 and s[:2] in _MARKET_PREFIXES and s[2:].isdigit():
        return s[2:]
    return s


def normalize_stock_code(stock_code: str) -> str:
    """北交所旧码归一化到 920 新码；其余代码原样返回（6 位数字校验）。"""
    code = _bare_code(stock_code)
    if len(code) != 6 or not code.isdigit():
        raise FetchError(f"无法识别的 A 股代码: {stock_code}", kind="unsupported")
    if code[:1] in ("4", "8"):
        return bse_code_map().get(code, code)
    return code


def market_of(stock_code: str) -> str:
    """代码所属市场：sh / sz / bj（北交所含 920 新码与未映射旧码）。"""
    code = normalize_stock_code(stock_code)
    if code.startswith("920") or code[:1] in ("4", "8"):
        return "bj"
    if code[:1] == "6":
        return "sh"
    if code[:1] in ("0", "3"):
        return "sz"
    raise FetchError(
        f"无法识别的 A 股代码: {stock_code}（仅支持沪深 A 股与北交所 920xxx）",
        kind="unsupported",
    )


def to_market_code(stock_code: str) -> str:
    """600519 → sh600519；000001 → sz000001；430047/920047 → bj920047。"""
    code = normalize_stock_code(stock_code)
    return f"{market_of(code)}{code}"


def is_bse_stock(stock_code: str) -> bool:
    """北交所个股（含旧码），供 CLI/工具层区分对拍源可用性。"""
    return market_of(stock_code) == "bj"
