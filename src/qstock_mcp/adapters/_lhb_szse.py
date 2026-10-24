"""深交所龙虎榜（交易公开信息）客户端与解析器（issue #114，spec #111）。

官方源：www.szse.cn ShowReport API（零鉴权 JSON，#97 实测 2026-10-19/20）：
- 汇总 `CATALOGID=1842_xxpl&TABKEY=tab1&txtStart&txtEnd&PAGENO`——一页固定 10 行
  （PAGESIZE 参数实测不生效），按 metadata.pagecount 翻页；行字段 zqdm/zqjc/cjje
  （亿元，展示值）/cjsl（万股，展示值）/plyy（披露原因）/bz（详情链接，内嵌
  DQRQ/ZQDM/ZBDM 参数）。空交易日 recordcount=0、data=[]（正常语义）。
- 席位明细 `CATALOGID=1842_detal&TABKEY=tab1,tab2&DQRQ&ZQDM&ZBDM`——tab1 为明细
  表一行（cjje 带"元"后缀精确值、cjsl 带"份/股"后缀）；tab2 为买 1-5/卖 1-5
  席位（zsmc/mrje/mcje，元；同一席位可同时出现在买卖两侧，记录重复属正常）。

解析器为纯函数（接收已 JSON 化的 payload），测试喂录制响应 fixture，不触网。
"""

import json
import logging
import re
import urllib.parse
import urllib.request

from .base import FetchError

log = logging.getLogger(__name__)

SUMMARY_URL = "https://www.szse.cn/api/report/ShowReport/data"
DETAIL_CATALOG = "1842_detal"
SUMMARY_CATALOG = "1842_xxpl"
HEADERS = {
    "Referer": "https://www.szse.cn/disclosure/deal/public/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

# 汇总行 bz 详情链接里的参数（业务键：同股当日多榜单各一条明细）
_BZ_PARAM_RE = re.compile(
    r"CATALOGID=1842_detal&TABKEY=tab1,tab2&DQRQ=([^&]+)&ZQDM=([^&]+)&ZBDM=([^&']+)"
)


def _plain_num(v):
    """展示数字串（含千分位逗号）→ float；None/空/异常 → None。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _yuan(v):
    """深交所明细金额串（'2,042,838,632 元'）→ 元（float，原始单位）；异常 → None。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    for suffix in ("元", "份/股", "份", "股"):
        s = s.replace(suffix, "")
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _wan(v):
    """深交所明细金额串（'2,042,838,632 元'）→ 万元（day_amount 口径）。"""
    n = _yuan(v)
    return None if n is None else n / 1e4


def _first_tab(payload) -> dict:
    """ShowReport 响应顶层为按 TABKEY 排的 list（tab1 在首），取首个含 data 的元素。"""
    if isinstance(payload, list):
        for tab in payload:
            if isinstance(tab, dict) and "data" in tab:
                return tab
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_summary_page(payload) -> dict:
    """汇总单页 → {"rows": [...], "recordcount": int, "pagecount": int}。

    rows 元素含 zqdm/zqjc/cjje_yi（亿元）/plyy/trade_date/dqrq/zbdm——zbdm 从
    bz 详情链接解析（同股多榜单逐条取明细的入口参数）。
    """
    tab = _first_tab(payload)
    md = tab.get("metadata") or {}
    rows = []
    for r in tab.get("data") or []:
        zbdm = None
        bz = r.get("bz")
        if bz:
            m = _BZ_PARAM_RE.search(str(bz))
            if m:
                zbdm = m.group(3)
        rows.append(
            {
                "zqdm": str(r.get("zqdm") or "").strip(),
                "zqjc": str(r.get("zqjc") or "").strip() or None,
                "cjje_yi": _plain_num(r.get("cjje")),  # 展示值，亿元
                "plyy": str(r.get("plyy") or "").strip() or None,
                "dqrq": str(r.get("dqrq") or "").strip() or None,
                "zbdm": zbdm,
            }
        )
    return {
        "rows": rows,
        "recordcount": int(md.get("recordcount") or 0),
        "pagecount": int(md.get("pagecount") or 1),
    }


def parse_detail(payload) -> dict:
    """个股明细 → {"trade_date","stock_name","day_amount","buy_seats","sell_seats"}。

    tab1（明细表）取精确全日成交金额（万元）与简称；tab2 席位按 mmlb 买/卖前缀
    分流（买入合计 = Σ 买行 mrje，卖出合计 = Σ 卖行 mcje，东财同口径）。名称中
    的 HTML 实体（&nbsp;）与括号代码在此清洗。
    """
    tabs = payload if isinstance(payload, list) else [payload]
    tab1, tab2 = {}, []
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        data = tab.get("data") or []
        if (tab.get("metadata") or {}).get("tabkey") == "tab1" or any(
            "cjje" in row for row in data if isinstance(row, dict)
        ):
            tab1 = data[0] if data else {}
        else:
            tab2 = data
    name = str(tab1.get("zqjc") or "")
    name = re.sub(r"[ \s]*[(（]\d{6}[)）]", "", name).replace("&nbsp;", "").strip()
    buy_seats, sell_seats = [], []
    for r in tab2:
        side = str(r.get("mmlb") or "")
        if side.startswith("买"):
            buy_seats.append(
                {"seat": str(r.get("zsmc") or "").strip(), "yuan": _yuan(r.get("mrje"))}
            )
        elif side.startswith("卖"):
            sell_seats.append(
                {"seat": str(r.get("zsmc") or "").strip(), "yuan": _yuan(r.get("mcje"))}
            )
    return {
        "trade_date": (tab1.get("dqrq") or "").replace("-", "") or None,
        "stock_name": name or None,
        "day_amount": _wan(tab1.get("cjje")),  # 元 → 万元（day_amount 统一万元口径）
        "buy_seats": buy_seats,
        "sell_seats": sell_seats,
    }


def _get_json(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"深交所请求失败: {e}") from e


def fetch_summary(start_dash: str, end_dash: str, page: int = 1) -> dict:
    """汇总单页（start/end 为 yyyy-mm-dd）；解析失败抛 FetchError。"""
    q = urllib.parse.urlencode(
        {
            "SHOWTYPE": "JSON",
            "CATALOGID": SUMMARY_CATALOG,
            "TABKEY": "tab1",
            "txtStart": start_dash,
            "txtEnd": end_dash,
            "PAGENO": page,
        }
    )
    try:
        return parse_summary_page(_get_json(f"{SUMMARY_URL}?{q}"))
    except FetchError as e:
        raise FetchError(f"深交所龙虎榜汇总解析失败（{start_dash}~{end_dash} 第{page}页）: {e}") from e


def fetch_detail(dqrq_dash: str, zqdm: str, zbdm: str) -> dict:
    """单股席位明细（dqrq 为 yyyy-mm-dd，zbdm 榜单代码不可缺——由汇总 bz 链接提供）。"""
    q = urllib.parse.urlencode(
        {
            "SHOWTYPE": "JSON",
            "CATALOGID": DETAIL_CATALOG,
            "TABKEY": "tab1,tab2",
            "DQRQ": dqrq_dash,
            "ZQDM": zqdm,
            "ZBDM": zbdm,
        }
    )
    try:
        return parse_detail(_get_json(f"{SUMMARY_URL}?{q}"))
    except FetchError as e:
        raise FetchError(f"深交所龙虎榜明细解析失败（{zqdm} {dqrq_dash}）: {e}") from e
