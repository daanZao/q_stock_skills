"""同花顺历史龙虎榜 basic 与深圳席位详情（ADR 0016/0017）。"""

import html as html_lib
import re
import time
import urllib.error
import urllib.request

from ._bse_codes import market_of, normalize_stock_code
from ._lhb_detail import seat_problem
from ._num import _money_cn
from .base import FetchError
from .ths_adapter import _check_ban

PAGE_URL = "https://data.10jqka.com.cn/ifmarket/lhbggxq/report/{date}/"
# 性能验证（2026-10-27）：单次历史页响应截断，未取得有效性能样本；
# 20 秒为保守请求纪律，最小安全间隔、TPS 与并发能力均待验证。
PAUSE_S = 20.0
TIMEOUT_SEC = 20
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        " AppleWebKit/537.36 (KHTML, like Gecko)"
        " Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.10jqka.com.cn/market/longhu/",
}

_BLOCK_RE = re.compile(
    r'<div\b(?=[^>]*\bclass="stockcont")(?=[^>]*\bstockcode="(\d{6})")'
    r"[^>]*>(.*?)(?=<div\b[^>]*\bclass=\"stockcont\"|</body>)",
    re.IGNORECASE | re.DOTALL,
)
_TARGET_CODE_RE = re.compile(
    r"^(?:(?:00[0-3]|30[01]|60[0135]|68[89])\d{3}|[48]\d{5}|920\d{3})$"
)


def _plain(fragment: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _span_amount(block: str, label: str) -> tuple[float, str]:
    match = re.search(
        re.escape(label) + r"\s*：<span[^>]*>\s*([-\d.,]+)\s*</span>\s*(亿元|万元|亿|万)",
        block,
    )
    if not match:
        raise FetchError(f"同花顺龙虎榜缺字段: {label}")
    unit = match.group(2)[:1]
    value = _money_cn(match.group(1).replace(",", "") + unit)
    if value is None:
        raise FetchError(f"同花顺龙虎榜字段非法: {label}")
    return value, unit


def _day_amount(block: str) -> float:
    match = re.search(r"成交额：\s*([-\d.,]+)\s*(亿元|万元|亿|万)", block)
    if not match:
        raise FetchError("同花顺龙虎榜缺字段: 成交额")
    value = _money_cn(match.group(1).replace(",", "") + match.group(2)[:1])
    if value is None:
        raise FetchError("同花顺龙虎榜字段非法: 成交额")
    return value


def _detail_rows(block: str, basic: dict) -> list[dict]:
    rows: list[dict] = []
    sides: set[str] = set()
    for table in re.findall(r"<table\b[^>]*>(.*?)</table>", block, re.DOTALL | re.IGNORECASE):
        heading = re.search(r"<th\b[^>]*>(.*?)</th>", table, re.DOTALL | re.IGNORECASE)
        heading_text = _plain(heading.group(1)) if heading else ""
        seat_type = "buy" if "买入金额最大" in heading_text else "sell" if "卖出金额最大" in heading_text else None
        if seat_type is None:
            continue
        sides.add(seat_type)
        body = re.search(r"<tbody\b[^>]*>(.*?)</tbody>", table, re.DOTALL | re.IGNORECASE)
        for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", body.group(1) if body else "", re.DOTALL | re.IGNORECASE):
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", tr, re.DOTALL | re.IGNORECASE)
            if len(cells) < 4:
                raise FetchError("席位行不完整")
            link = re.search(r"<a\b([^>]*)>(.*?)</a>", cells[0], re.DOTALL | re.IGNORECASE)
            attrs = link.group(1) if link else ""
            title = re.search(r'\btitle=["\'](.*?)["\']', attrs, re.DOTALL | re.IGNORECASE)
            seat_name = html_lib.unescape(title.group(1)).strip() if title else _plain(cells[0])
            code_match = re.search(r"/orgcode/([^/\"']+)/", attrs)
            seat_code = code_match.group(1) if code_match else None
            try:
                buy, sell, net = (float(_plain(cell).replace(",", "")) for cell in cells[1:4])
            except ValueError as exc:
                raise FetchError(f"席位金额非法: {seat_name}") from exc
            if not seat_name:
                raise FetchError("席位名称为空")
            rows.append({
                "trade_date": basic["trade_date"],
                "stock_code": basic["stock_code"],
                "lhb_reason": basic["lhb_reason"],
                "seat_name": seat_name,
                "seat_code": seat_code,
                "seat_type": seat_type,
                "amount": buy if seat_type == "buy" else sell,
                "amount_percent": None,
                "buy_count_3m": None,
                "sell_count_3m": None,
                "buy_amount": buy,
                "sell_amount": sell,
                "net_buy_amount": net,
            })
    if sides != {"buy", "sell"} or not rows:
        raise FetchError("买卖席位明细不完整")
    problem = seat_problem(rows)
    if problem:
        raise FetchError(problem)
    return rows


def parse_lhb_html(html: str, trade_date: str) -> dict:
    dash = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    if not re.search(rf'\breport=["\']{re.escape(dash)}["\']', html):
        raise FetchError(f"同花顺龙虎榜日期不符: 请求 {dash}")
    rows: list[dict] = []
    details: list[dict] = []
    detail_errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for match in _BLOCK_RE.finditer(html):
        raw_code, block = match.groups()
        if not _TARGET_CODE_RE.match(raw_code):
            continue
        title = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
        reason_match = re.search(
            rf"(.*?)\({raw_code}\)明细：(.*)", _plain(title.group(1)) if title else ""
        )
        if not reason_match:
            raise FetchError(f"同花顺龙虎榜缺上榜原因: {raw_code}")
        code = normalize_stock_code(raw_code)
        name, reason = (part.strip() for part in reason_match.groups())
        key = (code, reason)
        if key in seen:
            raise FetchError(f"同花顺龙虎榜业务键重复: {code} {reason}")
        seen.add(key)
        buy, buy_unit = _span_amount(block, "合计买入")
        sell, sell_unit = _span_amount(block, "合计卖出")
        net, net_unit = _span_amount(block, "净额")
        tolerance = 150.0 if "亿" in (buy_unit, sell_unit, net_unit) else 0.02
        if abs((buy - sell) - net) > tolerance:
            raise FetchError(f"同花顺龙虎榜净额不一致: {code} {reason}")
        basic = {
            "trade_date": trade_date,
            "stock_code": code,
            "stock_name": name,
            "close_price": None,
            "change_percent": None,
            "turnover_rate": None,
            "lhb_reason": reason,
            "net_buy_amount": net,
            "buy_amount": buy,
            "sell_amount": sell,
            "total_amount": buy + sell,
            "day_amount": (
                None
                if any(marker in reason for marker in ("累计", "连续三个交易日", "异常期间"))
                else _day_amount(block)
            ),
        }
        rows.append(basic)
        if market_of(code) == "sz":
            try:
                details.extend(_detail_rows(block, basic))
            except FetchError as exc:
                detail_errors.append(f"{code} {reason}: {exc}")
    result: dict = {"lhb_basic": rows, "lhb_stock_detail": details}
    if detail_errors:
        result["detail_errors"] = detail_errors
    return result


def parse_lhb_basic_html(html: str, trade_date: str) -> list[dict]:
    return parse_lhb_html(html, trade_date)["lhb_basic"]


def _fetch_page(url: str) -> bytes:
    request = urllib.request.Request(url, headers=dict(_HEADERS))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            body = response.read()
            _check_ban(response.status, body.decode("gb18030", errors="replace"))
            return body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("gb18030", errors="replace")
        _check_ban(exc.code, body)
        raise FetchError(f"同花顺龙虎榜 HTTP 错误: status={exc.code}") from exc
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"同花顺龙虎榜传输错误: {exc}") from exc


class ThsLhbAdapter:
    name = "ths"

    def __init__(self, *, pause_s: float = PAUSE_S, clock=time.sleep, page_loader=None):
        self._pause = pause_s
        self._clock = clock
        self._load_page = page_loader or _fetch_page
        self._requested_dates: set[str] = set()
        self._banned = False

    def _fetch(self, trade_date: str) -> dict:
        if self._banned:
            raise FetchError("同花顺已封禁，本轮停止后续历史日期请求", kind="connection")
        if trade_date in self._requested_dates:
            raise FetchError(f"同花顺同日期不自动重试: {trade_date}")
        if self._requested_dates:
            self._clock(self._pause)
        self._requested_dates.add(trade_date)
        url = PAGE_URL.format(date=f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}")
        try:
            payload = self._load_page(url)
            text = (
                payload.decode("gb18030", errors="replace")
                if isinstance(payload, bytes)
                else payload
            )
            _check_ban(None, text)
            return parse_lhb_html(text, trade_date)
        except FetchError as exc:
            if "封禁" in str(exc):
                self._banned = True
            raise

    def fetch_lhb(self, trade_date: str) -> dict:
        return self._fetch(trade_date)

    def fetch_lhb_basic(self, trade_date: str) -> list[dict]:
        return self._fetch(trade_date)["lhb_basic"]
