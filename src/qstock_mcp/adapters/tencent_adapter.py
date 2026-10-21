"""腾讯行情薄封装（个股日线，issue #112 / spec #111）。

自研直调腾讯 K 线裸接口（仅标准库 urllib，薄封装先例见 mx_client.py），不使用
akshare `stock_zh_a_hist_tx` / `stock_zh_index_daily_tx`：实测封装层把第 6 列
成交量误命名为 amount、丢弃真实成交额列，且北交所 920xxx 不在其代码归一化范围。

接口与解析口径（2026-10-20/21 实测）：

    GET https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get
    param={symbol},day,{start},{end},{count},{adjust}

响应为 JS 赋值 `kline_dayqfq={...}`；data[symbol] 下按复权口径取
qfqday/hfqday/day 键。单次最多返回 640 根，且上游忽略 start 参数（返回 end
之前最近 640 根），因此本层逐段向前回溯并本地裁剪请求区间；多段回溯间留
0.3s 间隔（实测连发 5–6 个请求触发 TLS 断连）。

请求的复权键缺失时（如北交所部分标的无复权事件）上游只给 `day`，数值与
不复权一致；响应无法区分"无复权事件"与"上游不提供调整序列"，本层按无复权
事件处理并返回 day 数据（实测 920047 三口径数值一致），不臆造调整序列。
回落可见化：发生时置 `TencentAdapter.adjust_fallback = True` 并记 WARNING
日志，对拍（daily_parity）与 fetch/query 工具层 note 据此标注。

原始行列位（按索引解析，兼容 10/11 列两种形态）：

    [date, open, close, high, low, volume, {}, turnover(%), amount(万元), ...]

- 注意列序是 open, close, high, low（非 OHLC）。
- volume：沪深主板/创业板/北交所/指数为手，×100 入库存股（ADR 0008）；
  科创板 sh688/sh689 实测已是股，不换算。
- amount：原生万元，直接入库（ADR 0006）。
- turnover：原生百分数，直接入 turnover_rate。
- change_percent / change_amount / amplitude：上游无列，由前收推算；无前收的
  首行置 None（不伪造）。
- tradestatus：腾讯无权威停牌列，置 None 由工具层启发式兜底（ADR 0009）。
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from time import sleep

from ._bse_codes import to_market_code
from ._daily_common import adjust_param, derive_change_fields
from ._eastmoney import _int, _num
from .base import FetchError

log = logging.getLogger(__name__)

KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
TIMEOUT_SEC = 20
MAX_BARS = 640  # 上游单次返回上限（实测请求 1000/3000 根仍只给 640）
MAX_CHUNKS = 64  # 640 × 64 ≈ 4 万根，覆盖 A 股全历史
CHUNK_PAUSE_S = 0.3  # 多段回溯时对腾讯限速（实测连发 5–6 个请求触发 TLS 断连）


def _response_key(adjust: str) -> tuple[str, ...]:
    """请求复权口径 → 响应键优先序；调整键缺失时上游回落到 day（不复权）。"""
    if adjust == "qfq":
        return ("qfqday", "day")
    if adjust == "hfq":
        return ("hfqday", "day")
    return ("day",)


def parse_kline_response(
    text: str, symbol: str, adjust: str
) -> tuple[list[list], str]:
    """腾讯 K 线响应文本 → (原始行列表, 实际使用的响应键)（纯函数，fixture 可测）。

    `text` 形如 `kline_dayqfq={"code":0,...}`；业务码非 0、缺 data[symbol]、
    JSON 损坏均抛 FetchError（收到响应后的错误，非连接级）。实际响应键用于
    识别复权回落：请求 qfq/hfq 但只取到 day 即发生了回落。
    """
    idx = text.find("=")
    if idx < 0:
        raise FetchError("腾讯K线响应格式异常：缺少 JS 赋值")
    raw = text[idx + 1 :].strip()
    if raw.endswith(";"):
        raw = raw[:-1]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FetchError(f"腾讯K线返回非法 JSON: {e}") from e
    if not isinstance(payload, dict):
        raise FetchError(f"腾讯K线返回非 JSON 对象：{type(payload).__name__}")
    if payload.get("code") != 0:
        raise FetchError(
            f"腾讯K线返回错误：code={payload.get('code')} msg={payload.get('msg')}"
        )
    node = (payload.get("data") or {}).get(symbol)
    if not isinstance(node, dict):
        raise FetchError(f"腾讯K线响应缺少 {symbol} 数据")
    for key in _response_key(adjust):
        rows = node.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, list)], key
    return [], _response_key(adjust)[0]


def fetch_kline_rows(
    symbol: str, end_date: str, adjust: str
) -> tuple[list[list], bool]:
    """请求 `end_date`（yyyymmdd）之前最近 MAX_BARS 根 K 线（可能含 start 之前行）。

    `symbol` 为带市场前缀的腾讯代码（sh600519/sz000001/bj920001）。
    返回 (原始行, 复权回落是否发生)：请求复权键缺失、上游按 day 返回时为 True。
    """
    params = urllib.parse.urlencode(
        {
            "_var": f"kline_day{adjust or 'none'}",
            # start 留空：上游忽略 start，只按 end 向前取 MAX_BARS 根
            "param": f"{symbol},day,,{_dashed_date(end_date)},{MAX_BARS},{adjust}",
            "r": "0.8205512681390605",
        }
    )
    req = urllib.request.Request(
        f"{KLINE_URL}?{params}", headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"腾讯K线 HTTP 错误：status={e.code}") from e
    except Exception as e:  # noqa: BLE001 - URLError/超时/TLS 断连等传输错误
        raise FetchError(f"腾讯K线传输错误：{e}") from e
    rows, used_key = parse_kline_response(text, symbol, adjust)
    return rows, used_key != _response_key(adjust)[0]


def _dashed_date(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _row_date(row: list) -> str:
    return str(row[0])[:10].replace("-", "") if row else ""


def _row_at(row: list, idx: int):
    return row[idx] if len(row) > idx else None


def _is_star_market(tx_code: str) -> bool:
    """科创板（STAR Market）成交量原生为股（实测 sh688981）；其余标的为手。"""
    return tx_code.startswith(("sh688", "sh689"))


def map_tencent_rows(tx_code: str, rows: list[list]) -> list[dict]:
    """腾讯原始行 → BAR_FIELDS bar dict（纯函数，按时间升序输入）。

    成交量按市场换算为股（ADR 0008）；amount 原生万元（ADR 0006）；
    涨跌幅/涨跌额/振幅由前收推算。前收为序列内上一行，序列首行无前收 → None。
    """
    star = _is_star_market(tx_code)
    bars: list[dict] = []
    prev_close = None
    for row in rows:
        trade_date = _row_date(row)
        if not trade_date:
            continue
        close = _num(_row_at(row, 2))
        high = _num(_row_at(row, 3))
        low = _num(_row_at(row, 4))
        volume = _int(_row_at(row, 5))
        if volume is not None and not star:
            volume *= 100
        change_amount, change_percent, amplitude = derive_change_fields(
            close, high, low, prev_close
        )
        bars.append(
            {
                "trade_date": trade_date,
                "open": _num(_row_at(row, 1)),
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": _num(_row_at(row, 8)),
                "amplitude": amplitude,
                "change_percent": change_percent,
                "change_amount": change_amount,
                "turnover_rate": _num(_row_at(row, 7)),
            }
        )
        if close is not None:
            prev_close = close
    return bars


class TencentAdapter:
    """腾讯个股日线源（fallback 第 1 位）：收盘后当晚含当日行（#104 实测）。

    `adjust_fallback`：fetch_daily 期间上游缺复权键按 day 回落时置 True
    （复权回落可见化，issue #112）；对拍与工具层 note 据此标注。
    """

    name = "tencent"

    def __init__(self) -> None:
        self.adjust_fallback = False

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]:
        """抓取 [start, end] 日线；北交所旧码先归一化到 920 新码。

        区间首日前收随分段回溯一并取得（>= start 的前一行），用于首行涨跌幅/
        振幅；本地按 [start, end] 裁剪。空区间返回 []（停牌/非交易日，非错误）。
        """
        adjust = adjust_param("腾讯", adj)
        self.adjust_fallback = False
        tx_code = to_market_code(stock_code)
        collected: dict[str, list] = {}
        cursor = end
        for chunk in range(MAX_CHUNKS):
            if chunk:
                # 全历史回溯逐段连续请求，按实测限速留出间隔
                sleep(CHUNK_PAUSE_S)
            rows, fell_back = fetch_kline_rows(tx_code, cursor, adjust)
            if fell_back:
                if not self.adjust_fallback:
                    log.warning(
                        "腾讯 %s 缺 %s 复权键，上游按 day 回落"
                        "（无复权事件或不提供调整序列）",
                        tx_code,
                        adjust or "none",
                    )
                self.adjust_fallback = True
            if not rows:
                break
            added = 0
            for row in rows:
                day = _row_date(row)
                if day and day not in collected:
                    collected[day] = row
                    added += 1
            if not added:
                break
            first = next((d for d in (_row_date(r) for r in rows) if d), "")
            if not first or first < start:
                break
            try:
                nxt = _previous_calendar_day(first)
            except ValueError as e:
                raise FetchError(f"腾讯K线日期格式异常: {first!r}") from e
            if nxt >= cursor:
                break  # 防御：上游未按预期向前推进
            cursor = nxt
        ordered = [collected[d] for d in sorted(collected)]
        bars = map_tencent_rows(tx_code, ordered)
        return [b for b in bars if start <= b["trade_date"] <= end]


def _previous_calendar_day(yyyymmdd: str) -> str:
    """前一个自然日（不感知周末/节假日；仅用于回溯游标推进）。"""
    d = datetime.strptime(yyyymmdd, "%Y%m%d").date() - timedelta(days=1)
    return d.strftime("%Y%m%d")
