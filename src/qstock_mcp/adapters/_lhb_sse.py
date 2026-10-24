"""上交所龙虎榜（每日交易信息）客户端与定宽文本解析器（issue #114，spec #111）。

官方源：query.sse.com.cn `showTradePublicFile.do?jsonCallBack=cb&isPagination=false
&dateTx=YYYY-MM-DD`，**必须带 Referer: https://www.sse.com.cn/disclosure/diclosure/
public/`（实测不带会被拒/超时）**。返回 JSONP 包裹的 fileContents——当日全部
披露情形的官方定宽文本（#97 实测 2026-10-19/20，14 类情形，逐股买卖前五席位）。

文本结构（行导向，格式数十年稳定；2021-10-08 起前五口径，此前为前三只，正则
天然兼容）：
- 交易日期:2026年10月20日（文件头）
- 情形标题 `一、...的前五只证券:`（可折行，如"十、"跨两行）；小节 ` 1、A股`/
  ` 2、B股`/` 3、封闭式基金`（只取 A 股，B 股/基金忽略）
- 汇总行 `    (1)  600302      ST标准    9.93%   6721210   4461.78`
  （代码/简称/偏离值%/成交量(股)/成交金额(万元)）
- 个股块 `证券代码: 600302 ... 证券简称: ST标准` + 分隔线 + `买入营业部名称:`
  前五（元）+ `卖出营业部名称:` 前五（元），席位名含"机构专用"/"沪股通专用"。

解析器（parse_sse_trade_file）为纯函数，测试喂录制文本 fixture，不触网。
偏离值%是偏离基准的数值（非个股涨跌幅），只用于情形归类，不写入 change_percent。
"""

import json
import logging
import re
import urllib.parse
import urllib.request

from .base import FetchError

log = logging.getLogger(__name__)

URL = "https://query.sse.com.cn/infodisplay/showTradePublicFile.do"
HEADERS = {
    # 实测必需（Referer 拼写以交易所页面为准，"diclosure" 是上游原样路径）
    "Referer": "https://www.sse.com.cn/disclosure/diclosure/public/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

_DATE_RE = re.compile(r"交易日期[:：]\s*(\d{4})年(\d{2})月(\d{2})日")
_SECTION_RE = re.compile(r"^\s*([一二三四五六七八九十]+、.+?)\s*[:：]?\s*$")
_SUBSECTION_RE = re.compile(r"^\s*\d+、\s*(A股|B股|封闭式基金)\s*$")
# 汇总行：从右往左取 成交金额(万元)/成交量(股)/偏离值%，左侧再拆 序号/代码/简称
_SUMMARY_RE = re.compile(
    r"^\s*\((\d+)\)\s+(\d{6})\s+(.+?)\s+([-\d.]+)%\s+([\d,]+)\s+([\d,]+(?:\.\d+)?)\s*$"
)
_STOCK_RE = re.compile(r"^\s*证券代码[:：]\s*(\d{6})\s+.*?证券简称[:：]\s*(.+?)\s*$")
_BUY_HDR_RE = re.compile(r"买入营业部名称")
_SELL_HDR_RE = re.compile(r"卖出营业部名称")
_SEAT_RE = re.compile(r"^\s*\(\d+\)\s+(.+?)\s+([\d,]+(?:\.\d+)?)\s*$")


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_sse_trade_file(payload: dict) -> dict:
    """fileContents payload → {"trade_date","stocks":[...]}。

    stocks 元素：{code,name,reason,deviation,volume,amount_wan,buy_seats,sell_seats}
    （seats 为 [{seat, yuan}]；无席位块的汇总行（历史/异常裁剪）容忍为空表）。
    """
    lines = payload.get("fileContents") or []
    trade_date = None
    section = None
    in_a_share = False
    stocks: list[dict] = []
    cur: dict | None = None
    seat_side = None
    for line in lines:
        m = _DATE_RE.search(line)
        if m:
            trade_date = "".join(m.groups())
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1)
            in_a_share = False
            continue
        m = _SUBSECTION_RE.match(line)
        if m:
            in_a_share = m.group(1) == "A股"
            continue
        m = _SUMMARY_RE.match(line)
        if m and in_a_share and section:
            stocks.append(
                {
                    "code": m.group(2),
                    "name": m.group(3).strip(),
                    "reason": section,
                    "deviation": _num(m.group(4)),
                    "volume": _num(m.group(5)),
                    "amount_wan": _num(m.group(6)),
                    "buy_seats": [],
                    "sell_seats": [],
                }
            )
            cur = stocks[-1]
            seat_side = None
            continue
        m = _STOCK_RE.match(line)
        if m:
            # 席位块挂到本小节内最近一个汇总行（同代码跨情形时各有独立块）
            cur = next((s for s in reversed(stocks) if s["code"] == m.group(1)), None)
            seat_side = None
            continue
        if _BUY_HDR_RE.search(line):
            seat_side = "buy_seats"
            continue
        if _SELL_HDR_RE.search(line):
            seat_side = "sell_seats"
            continue
        if line.strip("-—－= \t\r\n") == "":
            continue  # 分隔线/空行
        m = _SEAT_RE.match(line)
        if m and cur is not None and seat_side is not None:
            cur[seat_side].append({"seat": m.group(1).strip(), "yuan": _num(m.group(2))})
    return {"trade_date": trade_date, "stocks": stocks}


def fetch_trade_file(dash_date: str) -> dict:
    """按日抓取并解析（dash_date 为 yyyy-mm-dd）；失败/解析异常抛 FetchError。"""
    q = urllib.parse.urlencode(
        {"jsonCallBack": "cb", "isPagination": "false", "dateTx": dash_date}
    )
    req = urllib.request.Request(f"{URL}?{q}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode("utf-8")
    except Exception as e:
        raise FetchError(f"上交所交易信息请求失败（{dash_date}）: {e}") from e
    body = text.strip()
    if body.startswith("cb(") and body.endswith(")"):
        body = body[3:-1]
    try:
        payload = json.loads(body)
    except ValueError as e:
        raise FetchError(f"上交所交易信息解析失败（{dash_date}）: {e}") from e
    parsed = parse_sse_trade_file(payload)
    if not parsed["trade_date"]:
        raise FetchError(f"上交所交易信息缺少交易日期（{dash_date}）")
    return parsed
