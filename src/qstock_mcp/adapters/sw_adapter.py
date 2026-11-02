"""申万官网 swsresearch.com 薄封装适配器（issue #118，spec #111）。

自研 stdlib urllib 薄封装，直调申万宏源研究官网前端 JSON API（免鉴权），
不用 akshare 申万系封装——akshare 的 index_hist_sw 丢弃 hike/markup
（涨跌额/涨跌幅），index_realtime_sw 按位置取列（脆）；且本仓库源码 seam
惯例是真实响应解析为纯函数 + 薄封装（同 tencent_adapter，issue #112）。

接口（docs/research/2026-10-20-sw-boards-mapping.md 实测）：
- /institute-sw/api/index_publish/current/?indextype=一级行业|二级行业
  分页 50/页：实时快照（收盘后末次快照即当日收盘截面），31 一级 + 124 二级。
- /institute-sw/api/index_publish/trend/?swindexcode=<code>&period=DAY
  单指数全历史（1999 起，T+1），raw 含 hike/markup。
- /institute-sw/api/index_analysis/index_analysis_report/
  按日区间 × 指数类型（DAY），每日分析报表 T+1（换手率/PE/PB/流通市值/股息率）。
- /swindex/pdf/SwClass2021/StockClassifyUse_stock.xls 成分分类文件直链
  （成分字典在线刷新源，含北交所归属）。

WAF 纪律（2026-10-24 实测）：裸请求（无浏览器 UA）被 Tengine 508 拒绝，
必须带 User-Agent/Accept/Referer 头；verify=False（官网证书链问题，
akshare 申万系调用同款处理）。限速缺省 0.4s/请求（≤2 req/s，调研建议）。
"""

import json
import logging
import ssl
import urllib.parse
import urllib.request
from time import monotonic, sleep

from ._sw_board import (
    SW_LEVEL_INDEX_TYPES,
    check_sw_envelope,
    map_sw_analysis_rows,
    map_sw_board_list,
    map_sw_current_rows,
    map_sw_trend_rows,
)
from .base import FetchError

log = logging.getLogger(__name__)

BASE_URL = "https://www.swsresearch.com"
CURRENT_URL = f"{BASE_URL}/institute-sw/api/index_publish/current/"
TREND_URL = f"{BASE_URL}/institute-sw/api/index_publish/trend/"
ANALYSIS_URL = f"{BASE_URL}/institute-sw/api/index_analysis/index_analysis_report/"
CLASS_FILE_URL = f"{BASE_URL}/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.swsresearch.com/institute_sw/allIndex/releasedIndex",
}
_CLASS_FILE_HEADERS = {
    **_HEADERS,
    "Referer": "https://www.swsresearch.com/institute_sw/allIndex/downloadCenter",
}
_PAGE_SIZE = 50
_MAX_PAGES = 20  # 124 二级/50 = 3 页；20 页为异常防呆上限
DEFAULT_REQUEST_INTERVAL_S = 0.4  # ≤2 req/s（调研建议，无 SLA web 接口限速纪律）

# 官网证书链问题（akshare 申万系同款），verify=False 显式关闭校验
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


class SwSResearchAdapter:
    """申万官网板块数据源：boards section 主承载（issue #118）。

    fetch_boards 为 BoardAdapter 协议方法（盘面 boards 段直接注入 fallback
    链）；申万专属方法（板块清单/历史/分析报表/分类文件）供调度 CLI
    （backfill --source sw / sw-boards / sw-dict / morning 估值 part）调用。
    """

    name = "swsresearch"

    def __init__(self, request_interval_s: float = DEFAULT_REQUEST_INTERVAL_S):
        self._interval = request_interval_s
        self._last_request_at = 0.0

    # ---------------------------------------------------------------- HTTP 薄层

    def _throttle(self) -> None:
        if self._interval <= 0:
            return
        wait = self._interval - (monotonic() - self._last_request_at)
        if wait > 0:
            sleep(wait)

    def _get_json(self, url: str, params: dict, context: str) -> dict:
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{url}?{qs}", headers=_HEADERS
        )
        self._throttle()
        try:
            with urllib.request.urlopen(
                req, timeout=60, context=_SSL_CONTEXT
            ) as resp:
                self._last_request_at = monotonic()
                text = resp.read().decode("utf-8")
        except Exception as e:
            self._last_request_at = monotonic()
            raise FetchError(f"swsresearch {context} 抓取失败: {e}") from e
        try:
            payload = json.loads(text)
        except ValueError as e:
            raise FetchError(f"swsresearch {context} 响应非 JSON: {e}") from e
        try:
            check_sw_envelope(payload, context)
        except ValueError as e:
            raise FetchError(f"swsresearch {e}") from e
        return payload

    def _get_paged_results(self, url: str, params: dict, context: str) -> list[dict]:
        """分页收集 current/analysis 结果（page_size=50，按 count 翻页）。"""
        results: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            payload = self._get_json(
                url, {**params, "page": page, "page_size": _PAGE_SIZE}, context
            )
            data = payload["data"]
            if isinstance(data, list):  # 兼容个别端点直出列表
                results.extend(data)
                break
            batch = data.get("results") or []
            results.extend(batch)
            count = data.get("count") or 0
            if len(results) >= count or not batch:
                break
        return results

    # ---------------------------------------------------------------- BoardAdapter 协议（boards 段）

    def fetch_boards(self, trade_date: str) -> list[dict]:
        """申万一级+二级实时快照（收盘后调用即当日收盘截面）→ market_boards 行。"""
        return self._current_rows_by_level()

    # BoardAdapter 其余 section 申万不承载：显式拒绝（by-design unsupported，
    # 误用时单次即弃不重试）；本适配器只入 boards 段链（default_boards_adapters）
    def fetch_indices(self, trade_date: str) -> list[dict]:
        raise FetchError("swsresearch 不承载 indices section", kind="unsupported")

    def fetch_index_spot(self, index_code: str) -> list[dict]:
        raise FetchError("swsresearch 不承载指数补漏", kind="unsupported")

    def fetch_zt_pool(self, trade_date: str) -> list[dict]:
        raise FetchError("swsresearch 不承载 zt_pool section", kind="unsupported")

    def fetch_strong_stocks(self, trade_date: str) -> list[dict]:
        raise FetchError("swsresearch 不承载 strong_stocks section", kind="unsupported")

    def fetch_lhb(self, trade_date: str) -> dict:
        raise FetchError("swsresearch 不承载 lhb section", kind="unsupported")

    # ---------------------------------------------------------------- 申万专属能力（调度 CLI 用）

    def fetch_sw_board_list(self) -> list[dict]:
        """申万双级板块清单（回补/对拍目标集）：board_code/name/level。"""
        return map_sw_board_list(self._current_raw_by_level())

    def _current_raw_by_level(self) -> dict[int, list[dict]]:
        raw: dict[int, list[dict]] = {}
        for level, index_type in SW_LEVEL_INDEX_TYPES.items():
            raw[level] = self._get_paged_results(
                CURRENT_URL, {"indextype": index_type}, f"current(L{level})"
            )
        return raw

    def _current_rows_by_level(self) -> list[dict]:
        raw = self._current_raw_by_level()
        rows = []
        for level, records in raw.items():
            rows += map_sw_current_rows(records, level)
        return rows

    def fetch_sw_board_history(
        self, board: dict, start: str, end: str
    ) -> list[dict]:
        """trend 指数全历史（1999 起，T+1），客户端过滤 [start, end] 闭区间。

        board: {"board_code", "board_name", "level"}。空区间返回 []（非错误）。
        """
        records = self._get_json(
            TREND_URL,
            {"swindexcode": board["board_code"], "period": "DAY"},
            f"trend({board['board_code']})",
        )["data"]
        rows = map_sw_trend_rows(
            records, board["level"], board["board_code"], board["board_name"]
        )
        return [r for r in rows if r["trade_date"] and start <= r["trade_date"] <= end]

    def fetch_sw_board_analysis(self, start: str, end: str) -> list[dict]:
        """每日分析报表（T+1）双级一把抓：start/end 为 yyyymmdd 闭区间。"""
        rows = []
        for level, index_type in SW_LEVEL_INDEX_TYPES.items():
            records = self._get_paged_results(
                ANALYSIS_URL,
                {
                    "index_type": index_type,
                    "start_date": f"{start[:4]}-{start[4:6]}-{start[6:]}",
                    "end_date": f"{end[:4]}-{end[4:6]}-{end[6:]}",
                    "type": "DAY",
                    "swindexcode": "all",
                },
                f"analysis(L{level})",
            )
            rows += map_sw_analysis_rows(records, level)
        return rows

    def fetch_sw_class_file(self, url: str = CLASS_FILE_URL) -> bytes:
        """申万分类 xls 直链下载（成分字典在线刷新源，含北交所归属）。"""
        req = urllib.request.Request(url, headers=_CLASS_FILE_HEADERS)
        self._throttle()
        try:
            with urllib.request.urlopen(
                req, timeout=60, context=_SSL_CONTEXT
            ) as resp:
                self._last_request_at = monotonic()
                return resp.read()
        except Exception as e:
            self._last_request_at = monotonic()
            raise FetchError(f"swsresearch 分类文件下载失败: {e}") from e
