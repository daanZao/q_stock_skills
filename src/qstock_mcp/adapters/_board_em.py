"""盘面快照列映射：东财中文列 → 落库行 dict（issue #5）。

纯函数，不 import 第三方库，测试无需触网。映射口径以旧项目 appdb 实测写入端
（SKILLProject/stock-data/fetch_board_snapshot.py）为准："-" / "" / nan / None
统一映射为 None（不伪造数值）；行级日期列（efinance"最新交易日"、lhb"上榜日"/
"最近上榜日"）写入 trade_date 键，缺失时为 None 由工具层回退到传入日期。
"""

from ._eastmoney import _first, _int, _num


def _text(v):
    """文本字段：None/""/"-"/nan 统一为 None，否则 str。"""
    if v is None or v in ("-", ""):
        return None
    s = str(v)
    return None if s == "nan" else s


def _yyyymmdd(v):
    s = _text(v)
    return None if s is None else s.replace("-", "")


def map_index_rows(records: list[dict]) -> list[dict]:
    """指数快照（efinance 沪深系列指数 / akshare 沪深重要指数，列名互为别名）。"""
    rows = []
    for r in records:
        code = _first(r, "代码", "股票代码")
        rows.append(
            {
                "trade_date": _yyyymmdd(r.get("最新交易日")),  # 仅 efinance 有此列
                "index_code": None if code is None else str(code),
                "index_name": _first(r, "名称", "股票名称"),
                "latest_price": _num(r.get("最新价")),
                "change_percent": _num(r.get("涨跌幅")),
                "change_amount": _num(r.get("涨跌额")),
                "volume": _int(r.get("成交量")),
                "amount": _num(r.get("成交额")),
                "amplitude": _num(r.get("振幅")),  # efinance 无此列 → None
                "high": _num(r.get("最高")),
                "low": _num(r.get("最低")),
                "open": _num(r.get("今开")),
                "pre_close": _num(_first(r, "昨收", "昨日收盘")),
                "volume_ratio": _num(r.get("量比")),
            }
        )
    return rows


def map_board_rows(records: list[dict], board_type: str) -> list[dict]:
    """板块快照（行业/概念同列）；成交量/振幅等列接口不提供，置 None（不伪造）。"""
    rows = []
    for r in records:
        rows.append(
            {
                "board_type": board_type,
                "board_code": _text(r.get("板块代码")),
                "board_name": str(r["板块名称"]),
                "latest_price": _num(r.get("最新价")),
                "change_percent": _num(r.get("涨跌幅")),
                "change_amount": _num(r.get("涨跌额")),
                "volume": None,
                "amount": None,
                "amplitude": None,
                "high": None,
                "low": None,
                "open": None,
                "pre_close": None,
                "volume_ratio": None,
                "stock_count": None,
                "leading_stock": _text(r.get("领涨股票")),
                "leading_change": _num(r.get("领涨股票-涨跌幅")),
            }
        )
    return rows


# pool_type → 该池特有的列映射（zt=涨停 dt=跌停 zb=炸板）
_ZT_EXTRA_MAP = {
    "zt": {"limit_up_time": "首次封板时间", "consecutive_boards": "连板数"},
    "dt": {"limit_up_time": "最后封板时间", "consecutive_boards": "连续跌停"},
    "zb": {"limit_up_time": "首次封板时间", "zt_price": "涨停价", "zb_info": "炸板次数"},
}


def map_zt_pool_rows(records: list[dict], pool_type: str) -> list[dict]:
    """涨跌停/炸板池；各池特有列之外（limit_up_type/dt_price/volume 等）置 None。"""
    rows = []
    for r in records:
        row = {
            "pool_type": pool_type,
            "stock_code": str(r["代码"]),
            "stock_name": str(r["名称"]),
            "latest_price": _num(r.get("最新价")),
            "change_percent": _num(r.get("涨跌幅")),
            "zt_price": None,
            "volume": None,
            "amount": _num(r.get("成交额")),
            "limit_up_time": None,
            "limit_up_type": None,
            "consecutive_boards": None,
            "industry": _text(r.get("所属行业")),
            "dt_price": None,
            "zb_info": None,
        }
        for col, src in _ZT_EXTRA_MAP[pool_type].items():
            if col == "consecutive_boards":
                row[col] = _int(r.get(src))
            elif col == "zt_price":
                row[col] = _num(r.get(src))
            else:
                row[col] = _text(r.get(src))
        rows.append(row)
    return rows


def map_strong_rows(records: list[dict]) -> list[dict]:
    """强势股池；volume/consecutive_boards 列接口不提供，置 None。"""
    rows = []
    for r in records:
        rows.append(
            {
                "stock_code": str(r["代码"]),
                "stock_name": str(r["名称"]),
                "latest_price": _num(r.get("最新价")),
                "change_percent": _num(r.get("涨跌幅")),
                "volume": None,
                "amount": _num(r.get("成交额")),
                "turnover_rate": _num(r.get("换手率")),
                "market_cap": _num(r.get("总市值")),
                "consecutive_boards": None,
                "industry": _text(r.get("所属行业")),
                "reason": _text(r.get("入选理由")),
            }
        )
    return rows


def map_lhb_basic_rows(records: list[dict]) -> list[dict]:
    """龙虎榜个股明细；行级"上榜日"写入 trade_date（可能异于传入日期）。"""
    rows = []
    for r in records:
        rows.append(
            {
                "trade_date": _yyyymmdd(r.get("上榜日")),
                "stock_code": str(r["代码"]),
                "stock_name": str(r["名称"]),
                "close_price": _num(r.get("收盘价")),
                "change_percent": _num(r.get("涨跌幅")),
                "turnover_rate": _num(r.get("换手率")),
                "lhb_reason": _text(r.get("上榜原因")),
                "net_buy_amount": _num(r.get("龙虎榜净买额")),
                "buy_amount": _num(r.get("龙虎榜买入额")),
                "sell_amount": _num(r.get("龙虎榜卖出额")),
                "total_amount": _num(r.get("龙虎榜成交额")),
            }
        )
    return rows


def map_lhb_statistic_rows(records: list[dict]) -> list[dict]:
    """龙虎榜个股近三月统计；行级"最近上榜日"写入 trade_date。"""
    rows = []
    for r in records:
        rows.append(
            {
                "trade_date": _yyyymmdd(r.get("最近上榜日")),
                "stock_code": str(r["代码"]),
                "stock_name": str(r["名称"]),
                "appear_count_3m": _int(r.get("上榜次数")),
                "buy_amount_3m": _num(r.get("龙虎榜买入额")),
                "sell_amount_3m": _num(r.get("龙虎榜卖出额")),
                "net_buy_3m": _num(r.get("龙虎榜净买额")),
                "buy_seat_count": _int(r.get("买方机构次数")),
                "sell_seat_count": _int(r.get("卖方机构次数")),
            }
        )
    return rows


def map_lhb_yyb_capital_rows(records: list[dict]) -> list[dict]:
    """营业部资金榜（当前累计，无日期参数；fetch_date 由工具层记执行日）。"""
    rows = []
    for r in records:
        rows.append(
            {
                "rank": _int(r.get("序号")),
                "seat_name": str(r["营业部名称"]),
                "total_amount": _num(r.get("累计参与金额")),
                "buy_amount": _num(r.get("累计买入金额")),
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
                "buy_amount": _num(r.get("合计动用资金")),
                "buy_count": _int(r.get("年内买入股票只数")),
                "sell_amount": None,
                "sell_count": None,
                "net_buy_amount": None,
            }
        )
    return rows
