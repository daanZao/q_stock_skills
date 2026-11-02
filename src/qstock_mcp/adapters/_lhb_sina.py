"""新浪龙虎榜客户端（issue #114，spec #111）：纯对拍校验源（`lhb --check`）。

定位（#97 调研）：交易所官方为主源，新浪五函数为**互补校验/可手动触发对拍**。
issue #114 code-review 修正后本模块不再承载任何落库面——lhb_stock_statistic
改由库内 lhb_basic 滚动自算（repository.recompute_lhb_statistic），新浪侧
三处用途全在对拍路径（`tools_lhb.compare_lhb_sources`，不落库）：
1. `SinaLhbClient.daily_detail`：每日详情 HTML（含收盘价/成交额/席位 type）；
2. `SinaLhbClient.seat_detail`：席位 JSONP `getLHBComBSData`（买/卖各五，
   万元，实测有重复行需去重）；
3. `SinaLhbClient.ggtj`：近 60 日统计窗口（akshare `stock_lhb_ggtj_sina`，
   万元原生，字段与 statistic 表一一对应）——与库内自算快照逐股对照。
`map_ggtj_statistic_rows` 为其承载映射（中文列 → LHB_STATISTIC_FIELDS）。

解析器为纯函数（HTML/JSONP 文本进、记录出），测试喂录制 fixture，不触网；
pandas 懒加载（akshare 同依赖族）。
"""

import logging
import re
import urllib.parse
import urllib.request

from .base import FetchError, import_akshare
from ._lhb_bse import unwrap_jsonp

log = logging.getLogger(__name__)

DAILY_DETAIL_URL = (
    "https://vip.stock.finance.sina.com.cn/q/go.php/vInvestConsult/kind/lhb/index.phtml"
)
SEAT_URL = (
    "https://vip.stock.finance.sina.com.cn/q/api/jsonp.php/var%20details=/"
    "InvestConsultService.getLHBComBSData"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

_SHOW_DETAIL_RE = re.compile(
    r"showDetail\('(\d+)','(\d+)','(\d{4}-\d{2}-\d{2})'"
)


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def map_ggtj_statistic_rows(records: list[dict]) -> list[dict]:
    """akshare ggtj 记录（中文列）→ LHB_STATISTIC_FIELDS 行（金额万元原生）。"""
    rows = []
    for r in records:
        code = str(r.get("股票代码") or "").strip().zfill(6)
        rows.append(
            {
                "stock_code": code,
                "stock_name": str(r.get("股票名称") or "").strip() or None,
                "appear_count_3m": int(_num(r.get("上榜次数")) or 0) or None,
                "buy_amount_3m": _num(r.get("累积购买额")),
                "sell_amount_3m": _num(r.get("累积卖出额")),
                "net_buy_3m": _num(r.get("净额")),
                "buy_seat_count": int(_num(r.get("买入席位数")) or 0) or None,
                "sell_seat_count": int(_num(r.get("卖出席位数")) or 0) or None,
            }
        )
    return rows


def parse_daily_detail_html(html: str) -> list[dict]:
    """每日详情页 → 行 dict（code/name/close/amount_wan/volume_wan/indicator/seat_type）。

    真实页面每榜为 `table.list_table`，首行是榜名（指标）、次行才是列头——
    与 akshare 同口径逐表 skiprows=1 + header=0 解析；每行席位 type 从
    showDetail('type','code','date') onclick 提取（席位 JSONP 的入口参数）。
    pandas/bs4/lxml 懒加载（akshare 同依赖族）。
    """
    try:
        import pandas as pd
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise FetchError(
            "pandas/bs4 未安装（新浪龙虎榜解析需要）：pip install akshare"
        ) from e
    from io import StringIO

    types: dict[str, str] = {}
    for m in _SHOW_DETAIL_RE.finditer(html):
        types.setdefault(m.group(2), m.group(1))
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for table in soup.find_all("table", attrs={"class": "list_table"}):
        title_tr = table.find("tr")
        indicator = (
            title_tr.find("td").get_text(strip=True) if title_tr is not None else None
        )
        try:
            tdf = pd.read_html(StringIO(table.prettify()), header=0, skiprows=1)[0]
        except Exception:  # noqa: BLE001 - 空榜/形状异常表跳过
            continue
        if tdf.empty:
            continue
        cols = [str(c) for c in tdf.columns]
        if "股票代码" not in cols:
            continue
        for _, r in tdf.iterrows():
            code = str(r.get("股票代码") or "").strip().zfill(6)
            if not code.isdigit():
                continue
            rows.append(
                {
                    "stock_code": code,
                    "stock_name": str(r.get("股票名称") or "").strip() or None,
                    "close_price": _num(r.get("收盘价(元)")),
                    "value": _num(r.get("对应值(%)")),
                    "volume_wan": _num(r.get("成交量(万股)")),
                    "amount_wan": _num(r.get("成交额(万元)")),
                    "indicator": indicator,
                    "seat_type": types.get(code),
                }
            )
    return rows


def parse_seat_jsonp(text: str) -> dict:
    """席位 JSONP → {"buy":[{seat,buy_wan,sell_wan,net_wan}...],"sell":[...]}。

    实测响应有重复行（同席位同额重复出现），按 (seat, buy, sell) 去重保留首条。
    """
    payload = unwrap_jsonp(text)
    out = {"buy": [], "sell": []}
    seen = set()
    for side in ("buy", "sell"):
        for s in payload.get(side) or []:
            seat = str(s.get("comName") or "").strip()
            buy = _num(s.get("buyAmount"))
            sell = _num(s.get("sellAmount"))
            net = _num(s.get("netAmount"))
            key = (side, seat, buy, sell)
            if key in seen:
                continue
            seen.add(key)
            out[side].append(
                {"seat": seat, "buy_wan": buy, "sell_wan": sell, "net_wan": net}
            )
    return out


def seat_net(seats: dict) -> float | None:
    """席位 JSONP 解析结果 → 净买额（万元），与落库口径对齐：
    Σ买侧 buyAmount − Σ卖侧 sellAmount（东财口径——只用买侧买入额与卖侧卖出额；
    各席位反向金额不计）。单边缺失按 0 参与（与 _basic_row 一致），全空返回
    None（不伪造）。"""
    buys = [s["buy_wan"] for s in seats.get("buy", []) if s.get("buy_wan") is not None]
    sells = [s["sell_wan"] for s in seats.get("sell", []) if s.get("sell_wan") is not None]
    if not buys and not sells:
        return None
    return (sum(buys) if buys else 0.0) - (sum(sells) if sells else 0.0)


class SinaLhbClient:
    """新浪龙虎榜 HTTP 客户端（对拍用手动触发，低频使用）。"""

    name = "sina_lhb"

    def daily_detail(self, trade_date: str) -> list[dict]:
        dash = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        q = urllib.parse.urlencode({"tradedate": dash})
        try:
            req = urllib.request.Request(
                f"{DAILY_DETAIL_URL}?{q}", headers=HEADERS
            )
            with urllib.request.urlopen(req) as resp:
                html = resp.read().decode("gb18030", errors="replace")
        except Exception as e:
            raise FetchError(f"新浪龙虎榜每日详情请求失败（{trade_date}）: {e}") from e
        return parse_daily_detail_html(html)

    def seat_detail(self, stock_code: str, trade_date: str, seat_type: str) -> dict:
        dash = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        q = urllib.parse.urlencode(
            {"symbol": stock_code, "tradedate": dash, "type": seat_type}
        )
        try:
            req = urllib.request.Request(f"{SEAT_URL}?{q}", headers=HEADERS)
            with urllib.request.urlopen(req) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            raise FetchError(
                f"新浪龙虎榜席位请求失败（{stock_code} {trade_date}）: {e}"
            ) from e
        return parse_seat_jsonp(text)

    def ggtj(self) -> list[dict]:
        """近 60 日统计窗口（ggtj）→ LHB_STATISTIC_FIELDS 行（万元原生）。

        仅对拍用（不落库）：与库内 lhb_basic 自算快照逐股对照；窗口锚定采集
        执行日（ggtj 无行级上榜日），与目标交易日非同日时差异含窗口滑移成分，
        输出侧带 note 说明（tools_lhb.compare_lhb_sources）。"""
        ak = import_akshare()
        try:
            df = ak.stock_lhb_ggtj_sina(symbol="60")
        except Exception as e:
            raise FetchError(f"新浪龙虎榜统计窗口抓取失败: {e}") from e
        if df is None or df.empty:
            return []
        return map_ggtj_statistic_rows(df.to_dict("records"))
