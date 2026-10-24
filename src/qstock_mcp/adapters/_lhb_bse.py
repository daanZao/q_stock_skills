"""北交所龙虎榜（交易公开信息）客户端与解析器（issue #114，spec #111）。

官方源：**bseinfo.net**（非 bse.cn——后者是 JS cookie 行为挑战，纯 HTTP 客户端
永远过不了，#107 实测）。WAF 校验 TLS 指纹：需 curl_cffi `impersonate="chrome"`
（裸 urllib/requests 被 302 拦，#107 实测）；curl_cffi 懒加载，未安装报明确
FetchError。历史实测回溯到 2021-11 开市当周（#107）。

接口（均 POST + `Referer: https://www.bseinfo.net/disclosure/tradepublic.html`）：
- 汇总 `callAuctionController/list.do`：JSONP `cb({result, data:{content[], totalElements,
  totalPages}, startDate, endDate})`。行字段 zqdm/zqmc/jyrq/gpjyje（**万元**）/
  gpid... gpjysl（股）/plyy（含阈值）/sjsx/zflx/zrfs/gkxxlx/id；同股当日多情形
  各一行（独立 id，业务键天然对齐）。
- 席位明细 `callAuctionController/info.do?id=<id>&gkxxlx=B`：JSONP `cb([{result,
  data:[{inList(买1-5), outList(卖1-5)}]}])`；席位 yybmc/jgmrje（买入，元）/
  jgmcje（卖出，元）。北交所特有 yzycbdList（严重异常波动分类资金）本期不接线。

历史数据保留旧证券代码（2025-10-09 才全面切换 920 新码，#107）：入库前经
`_bse_codes.normalize_stock_code` 归一化（官方 248 条对照表，未覆盖旧码原样）。

解析器为纯函数，测试喂录制 JSONP fixture，不触网。
"""

import json
import logging
import urllib.parse

from .base import FetchError

log = logging.getLogger(__name__)

LIST_URL = "https://www.bseinfo.net/callAuctionController/list.do"
INFO_URL = "https://www.bseinfo.net/callAuctionController/info.do"
REFERER = "https://www.bseinfo.net/disclosure/tradepublic.html"
# 页面日期控件下限（精选层改革时点，#107）；不代表有数据
DIFF_DATE = "2019-12-29"


def _import_curl_cffi():
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as e:
        raise FetchError(
            "curl_cffi 未安装（北交所 bseinfo.net 需 Chrome TLS 指纹过 WAF）："
            "pip install qstock-mcp[sources]"
        ) from e
    return cffi_requests


def unwrap_jsonp(text: str):
    """剥 JSONP 包装：cb(...) / var details=(...); / 反热链注释前缀。"""
    body = text.strip()
    # 新浪席位响应带 /*<script>...</script>*/ 反热链注释前缀
    if body.startswith("/*"):
        end = body.find("*/")
        if end != -1:
            body = body[end + 2 :].strip()
    # var details=(...) 赋值形态
    eq = body.find("=")
    if body.startswith("var ") and eq != -1:
        body = body[eq + 1 :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    if "(" in body and body.endswith(")"):
        body = body[body.find("(") + 1 : -1]
    return json.loads(body)


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


def parse_bse_list(payload) -> list[dict]:
    """list.do JSONP → 行 dict 列表（金额万元/数量股/日期 yyyymmdd 规约）。"""
    inner = payload[0] if isinstance(payload, list) else payload
    data = (inner or {}).get("data") or {}
    rows = []
    for r in data.get("content") or []:
        rows.append(
            {
                "id": r.get("id"),
                "zqdm": str(r.get("zqdm") or "").strip(),
                "zqmc": str(r.get("zqmc") or "").strip() or None,
                "jyrq": str(r.get("jyrq") or "").replace("-", "") or None,
                "day_amount_wan": _num(r.get("gpjyje")),  # 万元（上游原生）
                "plyy": str(r.get("plyy") or "").strip() or None,
                "sjsx": str(r.get("sjsx") or "").strip() or None,
                "gjzf": _num(r.get("gjzf")),
                "zflx": str(r.get("zflx") or ""),
            }
        )
    return rows


def parse_bse_info(payload) -> dict:
    """info.do JSONP → {"buy_seats","sell_seats"}（金额万元；买取 inList.jgmrje，
    卖取 outList.jgmcje，与沪深口径一致）。"""
    inner = payload[0] if isinstance(payload, list) else payload
    data = (inner or {}).get("data") or []
    row = data[0] if data else {}
    buy = [
        {"seat": str(s.get("yybmc") or "").strip(), "yuan": _num(s.get("jgmrje"))}
        for s in row.get("inList") or []
    ]
    sell = [
        {"seat": str(s.get("yybmc") or "").strip(), "yuan": _num(s.get("jgmcje"))}
        for s in row.get("outList") or []
    ]
    return {"buy_seats": buy, "sell_seats": sell}


def _post(url: str, form: dict):
    cffi_requests = _import_curl_cffi()
    try:
        r = cffi_requests.post(
            url,
            headers={"Referer": REFERER},
            data=form,
            impersonate="chrome",
            timeout=30,
        )
        if r.status_code != 200:
            raise FetchError(f"HTTP {r.status_code}")
        return r.text
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"北交所请求失败（{url}）: {e}") from e


def fetch_list(start_dash: str, end_dash: str, page: int = 0) -> tuple[list[dict], int]:
    """汇总窗口（yyyy-mm-dd）；→ (rows, total_elements)。"""
    form = {
        "callback": "cb",
        "flag2": "1",
        "jyrqStart": start_dash,
        "jyrqEnd": end_dash,
        "diffDate": DIFF_DATE,
        "page": str(page),
        "flag": "true",
        "pageSize": "20",
        "zqdm": "",
        "fclx": "2",
        "gkxxlx[]": ["A", "B", "B1"],
        "sortField": "",
        "sortType": "",
        "keyword": "",
    }
    text = _post(LIST_URL, form)
    try:
        payload = unwrap_jsonp(text)
        inner = payload[0] if isinstance(payload, list) else payload
        total = int(((inner or {}).get("data") or {}).get("totalElements") or 0)
        return parse_bse_list(payload), total
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"北交所龙虎榜汇总解析失败（{start_dash}~{end_dash}）: {e}") from e


def fetch_info(list_id) -> dict:
    """单条上榜记录的席位明细（list_id 为汇总行 id）。"""
    form = {"callback": "cb", "id": str(list_id), "flag": "true", "gkxxlx": "B"}
    text = _post(INFO_URL, form)
    try:
        return parse_bse_info(unwrap_jsonp(text))
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"北交所龙虎榜明细解析失败（id={list_id}）: {e}") from e


def encode_form(form: dict) -> bytes:
    """测试辅助：与 _post 同口径的表单编码（curl_cffi data=dict 等价于 urlencode）。"""
    return urllib.parse.urlencode(form, doseq=True).encode()
