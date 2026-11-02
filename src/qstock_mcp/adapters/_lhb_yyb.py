"""龙虎榜营业部两子表映射：akshare 同花顺接口中文列 → 落库行 dict。

上游为同花顺（非东财，#97 勘误幸存），经 akshare `stock_lh_yyb_capital` /
`stock_lh_yyb_most` 调用（ExchangeLhbAdapter 子项）。纯函数，不 import 第三方库；
"-" / "" / None 统一映射为 None（不伪造数值）。

金额单位统一为万元（ADR 0006）：中文金额字符串（"X亿"/"X万"/无单位纯数字按
万元）经 `_money_cn` 解析。本模块原属 `_board_em.py`——东财盘面映射随 issue #121
（spec #111 剔东财收口）整体退役后，仅剩同花顺营业部两映射迁此。
"""

from ._num import _int, _money_cn


def map_lhb_yyb_capital_rows(records: list[dict]) -> list[dict]:
    """营业部资金榜（当前累计，无日期参数；fetch_date 由工具层记执行日）。"""
    rows = []
    for r in records:
        rows.append(
            {
                "rank": _int(r.get("序号")),
                "seat_name": str(r["营业部名称"]),
                "total_amount": _money_cn(r.get("累计参与金额")),
                "buy_amount": _money_cn(r.get("累计买入金额")),
                "sell_amount": None,
                "net_buy_amount": None,
                "avg_amount_per_trade": None,
            }
        )
    return rows


def map_lhb_yyb_most_rows(records: list[dict]) -> list[dict]:
    """营业部上榜排行（无日期参数；fetch_date 由工具层记执行日）。"""
    rows = []
    for r in records:
        rows.append(
            {
                "rank": _int(r.get("序号")),
                "seat_name": str(r["营业部名称"]),
                "appear_count": _int(r.get("上榜次数")),
                "buy_amount": _money_cn(r.get("合计动用资金")),  # 上游口径为"合计动用资金"（买+卖合计），非纯买入
                "buy_count": _int(r.get("年内买入股票只数")),
                "sell_amount": None,
                "sell_count": None,
                "net_buy_amount": None,
            }
        )
    return rows
