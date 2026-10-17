"""统一查询面：query_sw_board_daily 申万板块自算行情（issue #75，spec #70）。

point-in-time 成分归属（字典中 source_system='sw' 且 effective_date ≤ trade_date
的最新行）× 库内 stock_daily（固定 adj='qfq'，与 query_daily 缺省口径一致；
板块截面用 change_percent/amount，与复权方式无关，固定 adj 是为避免一股多行
重复计入）。单日截面聚合下推 SQL：level=1 按行业码前 2 位补 '0000'、level=2
按前 4 位补 '00' 聚合，聚合码形态与 sw_industry_meta 的 XX0000/XXXX00 一致，
可直接 join 名称（缺失时 industry_name 为 None 不报错）。change_pct 为成交额
加权（停牌占位行 change_percent/amount 为 null 时从权重剔除并归一；涨/跌/平
家数与加权同一口径，双非 null 才计家数），amount 为成交额合计（万元，库内
已是万元口径，ADR 0006），stock_count 为 point-in-time 归属该行业的成分股
总数（当日无日线行的成分也计入），counted_count 为参与加权计算数，差即停牌/
无当日行情数；全行业当日无任何日线行时该行业不出现。字典未覆盖的股票（含
非 sw 体系成分）不参与聚合，不报错。每行携带 basis: "sw_amount_weighted"
口径标注。
"""

import logging

from .dates import normalize_date
from .db import connect, ensure_schema
from .output import error as _error
from .repository import select_latest_daily_date, select_sw_board_daily

log = logging.getLogger(__name__)


def _normalize_industry_filter(code: str) -> str:
    """行业码过滤 → 成分匹配前缀：2 位一级前缀 / 4 位二级前缀 / 6 位完整码
    （6 位按尾零对化简：'480000'→'48'、'480100'→'4801'；非尾零的三级码
    原样返回，如 '480301'→'480301'，精确匹配该三级行业、不命中同属前 4 位
    前缀的兄弟码）。非法值抛 ValueError。"""
    c = code.strip()
    if c.isdigit() and len(c) in (2, 4):
        return c
    if c.isdigit() and len(c) == 6:
        if c.endswith("0000"):
            return c[:2]
        if c.endswith("00"):
            return c[:4]
        return c
    raise ValueError(
        f"无法识别的 industry_code: {code!r}（支持 2/4 位前缀或 6 位完整行业码）"
    )


def query_sw_board_daily(
    trade_date: str | None = None,
    level: int = 1,
    industry_code: str | None = None,
) -> dict:
    """申万板块自算行情（纯读，不触发补抓）：point-in-time 成分归属 × 库内
    个股日线单日截面聚合。trade_date 缺省取 stock_daily 库内最新交易日；
    level 1=一级 / 2=二级；industry_code 可选过滤（2/4 位前缀或 6 位完整码）。
    空结果 count:0 非错误；非法 level/industry_code/trade_date 走 error 契约。"""
    tool = "query_sw_board_daily"
    params = {"trade_date": trade_date, "level": level, "industry_code": industry_code}
    if level not in (1, 2):
        return _error(tool, params, f"非法 level: {level!r}（仅支持 1=一级 / 2=二级）")
    prefix = None
    if industry_code is not None:
        try:
            prefix = _normalize_industry_filter(industry_code)
        except ValueError as e:
            return _error(tool, params, str(e))
    if trade_date is not None:
        try:
            trade_date = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        try:
            ensure_schema(conn)
            if trade_date is None:
                trade_date = select_latest_daily_date(conn)
            rows = (
                select_sw_board_daily(conn, trade_date, level, prefix)
                if trade_date is not None
                else []
            )
        except Exception as e:  # noqa: BLE001 - 工具层把异常收敛为自描述错误
            log.exception("申万板块行情查询失败")
            return _error(tool, params, f"申万板块行情查询失败: {e}")
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "trade_date": trade_date,
        "count": len(rows),
        "rows": rows,
    }
