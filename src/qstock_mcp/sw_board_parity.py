"""申万板块三方位对拍（issue #118，spec #111 对拍要求）：不落库。

同一交易日三个口径逐板块字段级比较：
1. official：申万官网实时快照（current API，收盘后末次快照=当日收盘截面），
   经 fetch_section_with_fallback 取数（申万单源链，可注入 fake）。
2. db：库内 market_boards 申万口径行（board_type=industry_l1/l2）——
   校验落库链路（收盘快照写入/历史回补）与官网的一致性。
3. calc：库内自算 query_sw_board_daily（成分字典 point-in-time × stock_daily
   成交额加权，spec #111 申万降级/交叉验证角色）——与官网指数口径并列呈现，
   两者口径不同（自算为成分聚合、官方为发布指数），只做量级/方向参考，
   不做等价判定。

对齐键：official/db 按 board_code（801xxx）；自算行为分类码（110000 形态），
经行业名称与官网指数名对齐（level=2 名称去 Ⅱ/Ⅲ 后缀）。差异输出为报告
内容（差异即对拍产出，非失败）；official 抓取失败才整体 error。
"""

import logging

from .adapters import default_boards_adapters
from .dates import normalize_date, today_str
from .db import connect
from .fetch_chain import (
    AllSourcesFailed,
    UPSTREAM_RETRY_ADVICE,
    aggregate_error_kind,
    fetch_section_with_fallback,
)
from .output import error as _error
from .repository import select_board_rows
from .tools_sw_board import query_sw_board_daily

log = logging.getLogger(__name__)

_SW_BOARD_TYPES = {1: "industry_l1", 2: "industry_l2"}
# 对拍容差：库内行源自官网写入，浮点重放/快照 vs 收盘修订可能有末位差
_PRICE_TOL = 1e-2  # 点位/百分数绝对容差
_AMOUNT_TOL = 1e-4  # 金额相对容差


def _close(a, b, tol):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= max(tol, abs(b) * tol)


def _diff_pairs(official: dict, db_row: dict | None) -> list[str]:
    """official（官网行） vs db_row（库内行）字段级差异清单。"""
    if db_row is None:
        return ["库内无行"]
    diffs = []
    for f, tol in (
        ("latest_price", _PRICE_TOL),
        ("change_percent", _PRICE_TOL),
        ("change_amount", _PRICE_TOL),
        ("volume", 1.0),
        ("amount", _AMOUNT_TOL),
    ):
        if not _close(official.get(f), db_row.get(f), tol):
            diffs.append(f"{f}: 官网={official.get(f)} 库内={db_row.get(f)}")
    return diffs


def _norm_name(name: str | None) -> str:
    """行业名称对齐：自算分类名（level=2 带 Ⅱ/Ⅲ 后缀）→ 官网指数名。"""
    if not name:
        return ""
    for suffix in ("Ⅲ", "Ⅱ", "III", "II"):
        name = name.replace(suffix, "")
    return name.strip()


def compare_sw_board_sources(
    trade_date: str | None = None,
    level: int | str = 1,
    limit: int | None = None,
    adapters=None,
) -> dict:
    """申万板块对拍（不落库）：差异列于 rows[].diffs / calc，非失败信号。"""
    tool = "compare_sw_board_sources"
    params = {"trade_date": trade_date, "level": level, "limit": limit}
    if trade_date is None:
        effective = today_str()
    else:
        try:
            effective = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    if level in (1, 2):
        levels = [level]
    elif level == "all":
        levels = [1, 2]
    else:
        return _error(tool, params, f"非法 level: {level!r}（1/2/all）")
    if adapters is None:
        adapters = default_boards_adapters()
    try:
        r = fetch_section_with_fallback(adapters, "boards", effective)
    except AllSourcesFailed as e:
        kind = aggregate_error_kind(e.attempted)
        out = {
            "status": "error",
            "tool": tool,
            "params": params,
            "error": str(e),
            "error_kind": kind,
            "attempted_sources": e.attempted,
        }
        if kind == "upstream_unreachable":
            out["retry_advice"] = UPSTREAM_RETRY_ADVICE
        return out
    official = [
        row for row in r["data"]
        if row.get("board_type") in _SW_BOARD_TYPES.values()
    ]
    if limit is not None:
        official = official[:limit]
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        db_rows = select_board_rows(
            conn, "market_boards", "trade_date", "board_name", effective
        )
    finally:
        conn.close()
    db_by_code = {
        row["board_code"]: row
        for row in db_rows
        if row.get("board_type") in _SW_BOARD_TYPES.values()
    }
    # 自算口径（交叉验证参考）：level 逐档取，按行业名称对齐官网指数名
    calc_by_name: dict[str, dict] = {}
    dict_as_of = None
    for lv in levels:
        calc = query_sw_board_daily(trade_date=effective, level=lv)
        if lv == 1:
            dict_as_of = calc.get("dict_as_of")
        if calc.get("status") == "ok":
            for row in calc.get("rows") or []:
                calc_by_name[_norm_name(row.get("industry_name"))] = row
    rows = []
    mismatches = 0
    for o in official:
        db_row = db_by_code.get(o["board_code"])
        diffs = _diff_pairs(o, db_row)
        mismatches += 1 if diffs else 0
        calc_row = calc_by_name.get(_norm_name(o["board_name"]))
        entry = {
            "board_code": o["board_code"],
            "board_name": o["board_name"],
            "board_type": o["board_type"],
            "official": {
                "latest_price": o.get("latest_price"),
                "change_percent": o.get("change_percent"),
                "volume": o.get("volume"),
                "amount": o.get("amount"),
            },
            "in_db": db_row is not None,
            "diffs": diffs,
        }
        if calc_row is not None:
            entry["calc"] = {
                "industry_code": calc_row.get("industry_code"),
                "change_pct": calc_row.get("change_pct"),
                "amount": calc_row.get("amount"),
                "stock_count": calc_row.get("stock_count"),
                "counted_count": calc_row.get("counted_count"),
            }
        rows.append(entry)
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "trade_date": effective,
        "source": r["source"],
        "levels": levels,
        "count": len(rows),
        "mismatches": mismatches,
        "dict_as_of": dict_as_of,
        "rows": rows,
    }
