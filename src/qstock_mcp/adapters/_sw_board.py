"""申万官网（swsresearch.com）板块接口映射：原始 JSON → 落库行 dict（issue #118）。

纯函数，不 import 第三方库，测试无需触网（fixture 为 2026-10-23/24 实抓响应
裁剪，见 tests/fixtures/sw/）。响应信封统一 {"code": "200", "data": ...}，
行值为字符串数字，"-" / "" / None 统一映射为 None（不伪造数值）。

量纲（2026-10-24 同一交易日收盘截面对拍核实，current 收盘后末次快照 vs
trend 收盘行，比例恰为 100/100，见 docs/research/2026-10-20-sw-boards-mapping.md
风险 1 的闭环）：
- current l5（成交额）单位百万元 → 万元 ×1e2；trend/analysis bargainsum
  （成交额）单位亿元 → 万元 ×1e4。
- current l11（成交量）单位万手 → 股 ×1e6；trend/analysis bargainamount
  （成交量）单位亿股 → 股 ×1e8（对拍：223.53 亿 ÷ 29.63 亿 = 7.54 元/股，
  与均价量级自洽）。
- analysis negotiablessharesum1（流通市值）单位亿元 → 万元 ×1e4。
- 价格/涨跌幅/换手率/PE/PB/股息率为无量纲或百分数原值透传。

board_type：申万双级落 industry_l1（31）/ industry_l2（124），与东财存量
industry/concept 行按 (trade_date, board_type, board_name) 业务键天然隔离。
申万不发布的字段（量比/领涨股/主力资金净流入）置 None——三字段按 #113
第四态语义废弃，见 tools_board.PLANNED_ABSENT_BOARD_FIELDS。
"""

from ._num import _num

# 量纲换算系数（对拍核实，见模块 docstring）
_CURRENT_AMOUNT_X = 1e2  # 百万元 → 万元（ADR 0006）
_CURRENT_VOLUME_X = 1e6  # 万手 → 股（ADR 0008）
_TREND_AMOUNT_X = 1e4  # 亿元 → 万元（ADR 0006）
_TREND_VOLUME_X = 1e8  # 亿股 → 股（ADR 0008）
_FLOAT_CAP_X = 1e4  # 亿元 → 万元（ADR 0006）

SW_LEVEL_TYPES = {1: "industry_l1", 2: "industry_l2"}
SW_LEVEL_INDEX_TYPES = {1: "一级行业", 2: "二级行业"}

# market_boards 行中申万不发布、恒为 None 的列（PLANNED_ABSENT_BOARD_FIELDS
# 对应的三字段废弃口径；stock_count 官网接口无直接列，成分数可经成分字典派生）
_SW_ABSENT_BOARD_COLS = {
    "volume_ratio": None,
    "stock_count": None,
    "leading_stock": None,
    "leading_change": None,
}


def _yyyymmdd(v) -> str | None:
    """申万日期（YYYY-MM-DD 或 ISO 带时区）→ yyyymmdd；无法解析返回 None。"""
    s = str(v).strip() if v is not None else ""
    day = s[:10].replace("-", "")
    return day if len(day) == 8 and day.isdigit() else None


def _pct(numerator, base):
    """派生百分数：(numerator/base)×100；base 缺失/为零返回 None（不伪造）。"""
    if numerator is None or base in (None, 0):
        return None
    return numerator / base * 100


def map_sw_current_rows(records: list[dict], level: int) -> list[dict]:
    """current 接口（实时快照，收盘后末次快照即当日收盘截面）→ market_boards 行。

    列位（按键名，非位置）：l3=昨收 l4=今开 l5=成交额 l6=最高 l7=最低
    l8=最新价 l11=成交量。涨跌幅/涨跌额/振幅由 最新价/昨收 派生（接口无直接列）。
    """
    board_type = SW_LEVEL_TYPES[level]
    rows = []
    for r in records:
        pre_close = _num(r.get("l3"))
        latest = _num(r.get("l8"))
        high = _num(r.get("l6"))
        low = _num(r.get("l7"))
        rows.append(
            {
                "board_type": board_type,
                "board_code": str(r.get("swindexcode") or ""),
                "board_name": str(r.get("swindexname") or ""),
                "latest_price": latest,
                "change_percent": _pct(latest - pre_close, pre_close)
                if latest is not None and pre_close is not None
                else None,
                "change_amount": None
                if latest is None or pre_close is None
                else latest - pre_close,
                "volume": None
                if _num(r.get("l11")) is None
                else int(_num(r.get("l11")) * _CURRENT_VOLUME_X),
                "amount": None
                if _num(r.get("l5")) is None
                else _num(r.get("l5")) * _CURRENT_AMOUNT_X,
                "amplitude": _pct(high - low, pre_close),
                "high": high,
                "low": low,
                "open": _num(r.get("l4")),
                "pre_close": pre_close,
                **_SW_ABSENT_BOARD_COLS,
            }
        )
    return rows


def map_sw_trend_rows(
    records: list[dict], level: int, board_code: str, board_name: str
) -> list[dict]:
    """trend 接口（指数全历史 K 线，1999 起，T+1）→ market_boards 历史回补行。

    行自带 bargaindate（逐日 trade_date）；markup（涨跌幅%）/hike（涨跌额）
    接口直给，不派生；振幅由 (最高−最低)/昨收 派生——昨收取上一行收盘
    （全历史序列首行无昨收，振幅为 None 不伪造）。
    """
    board_type = SW_LEVEL_TYPES[level]
    rows = []
    prev_close = None
    for r in records:
        close = _num(r.get("closeindex"))
        high = _num(r.get("maxindex"))
        low = _num(r.get("minindex"))
        rows.append(
            {
                "trade_date": _yyyymmdd(r.get("bargaindate")),
                "board_type": board_type,
                "board_code": board_code,
                "board_name": board_name,
                "latest_price": close,
                "change_percent": _num(r.get("markup")),
                "change_amount": _num(r.get("hike")),
                "volume": None
                if _num(r.get("bargainamount")) is None
                else int(_num(r.get("bargainamount")) * _TREND_VOLUME_X),
                "amount": None
                if _num(r.get("bargainsum")) is None
                else _num(r.get("bargainsum")) * _TREND_AMOUNT_X,
                "amplitude": _pct(high - low, prev_close),
                "high": high,
                "low": low,
                "open": _num(r.get("openindex")),
                "pre_close": prev_close,
                **_SW_ABSENT_BOARD_COLS,
            }
        )
        prev_close = close
    return rows


def map_sw_board_list(raw: dict[int, list[dict]]) -> list[dict]:
    """current 接口结果（level → 行列表）→ 申万板块清单
    （回补目标集）：code/name/level。"""
    boards = []
    for level, records in raw.items():
        for r in records:
            boards.append(
                {
                    "board_code": str(r.get("swindexcode") or ""),
                    "board_name": str(r.get("swindexname") or ""),
                    "level": level,
                }
            )
    return boards


def map_sw_analysis_rows(records: list[dict], level: int) -> list[dict]:
    """每日分析报表（T+1，换手率/PE/PB/流通市值/股息率）→ sw_board_valuation 行。

    bargaindate 为 ISO 带时区（东八区发布日），取日期部分；流通市值亿元→万元
    （ADR 0006）；百分数字段（换手率/股息率）原值透传。
    """
    rows = []
    for r in records:
        rows.append(
            {
                "trade_date": _yyyymmdd(r.get("bargaindate")),
                "board_code": str(r.get("swindexcode") or ""),
                "board_name": str(r.get("swindexname") or ""),
                "level": level,
                "close_index": _num(r.get("closeindex")),
                "turnover_rate": _num(r.get("turnoverrate")),
                "pe_ratio": _num(r.get("pe")),
                "pb_ratio": _num(r.get("pb")),
                "float_cap": None
                if _num(r.get("negotiablessharesum1")) is None
                else _num(r.get("negotiablessharesum1")) * _FLOAT_CAP_X,
                "dividend_yield": _num(r.get("dp")),
            }
        )
    return rows


def check_sw_envelope(payload: dict, context: str) -> None:
    """申万响应信封校验：code != "200" 或 data 缺失抛 FetchError 语义错误。

    由适配器捕获包装为 FetchError（本函数只抛 ValueError 携带原因）。
    """
    if not isinstance(payload, dict):
        raise ValueError(f"{context}: 响应非 JSON 对象")
    if str(payload.get("code")) != "200":
        raise ValueError(
            f"{context}: 业务码 {payload.get('code')!r} message={payload.get('message')!r}"
        )
    if payload.get("data") is None:
        raise ValueError(f"{context}: data 为空")
