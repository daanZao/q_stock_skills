"""同花顺涨停揭秘 dataapi 薄封装（issue #120，spec #111，ADR 0012）。

自研 stdlib urllib 薄封装直调 data.10jqka.com.cn dataapi（薄封装先例见
tencent_adapter.py），承载 zt_pool 三池（涨停/跌停/炸板）。调研依据
docs/research/2026-10-20-zt-pool-alternatives.md（2026-10-20 本机实测）。

**低频纪律（实测风控极敏感：突发即 IP 级封禁、恢复 >5 分钟）**：

- 每日每池一次：工具层（tools_board）在执行前先查采集台账，当日已有
  ok/empty 记录即跳过上游请求；段抓取经 fetch_section_with_fallback
  max_retries=0——单池单次尝试，不进入同源重试。
- 长退避：池间与分页间默认 pause POOL_PAUSE_S（20s，env
  THS_POOL_PAUSE_S 可覆盖）；触发封禁后本轮剩余池全部放弃
  （封禁状态下单池重试只会加剧封禁，等次日再抓）。
- 封禁探测：HTTP 403 或响应体含 "forbidden"（Nginx 封禁页，实测直接打印
  本机 IP）即判定 IP 级封禁，抛 FetchError(kind="connection")——上游拒服
  视同连接级故障（upstream_unreachable），核心 section 中止线与新源故障
  语义对齐（spec #111；ADR 0012 补记）。probe() 提供单次轻量探测
  （GET 页面入口，不打 dataapi），供 CLI 健康巡检。

端点与字段（实测字段见 _board_ths.py 头注）：涨停池 limit_up_pool、炸板池
open_limit_pool、跌停池 lower_limit_pool（后两者端点名/字段 ID 取自
limtupInfo 页面 JS 资产；三池 2026-10-23 真实抓取全部验证通过：zt 80 行
全扩展字段、dt 6 行首封时间/板型、zb 12 行首封时间/炸板次数）。响应信封
（社区实现互证 + 冒烟实测）：{"status_code": 0, "data": {"info":
[行dict, ...]}}——新版 info 即行列表（命名键），兼容旧版
{"info": [{"items": [...]}]} 与单页 dict 形态。limit 参数上游上限 200
（实测超限报 status_code=-1 "limit must be less than or equal to 200"），
满页（返回行数 = limit）且信封无 pages 元数据时按页续抓、分页间与池间同
退避。field 投影参数必需：不带 field 上游只回 code/name/latest/change_rate
四字段（冒烟实测）。date 参数 yyyymmdd，历史回溯实测 4 天可取、更深处
未验证。
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from time import sleep

from ._board_ths import map_ths_pool_rows
from .base import FetchError

log = logging.getLogger(__name__)

DATAAPI = "https://data.10jqka.com.cn/dataapi/limit_up"
PAGE_URL = "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html"
TIMEOUT_SEC = 20

# 池间/分页间退避（秒）：实测秒内连发 6 请求即触发 IP 级封禁
POOL_PAUSE_S = float(os.environ.get("THS_POOL_PAUSE_S", "20"))
PAGE_LIMIT = 200  # 上游单页上限（实测 limit=500 报 status_code=-1）
MAX_PAGES = 5  # 防御：分页上限

# 池端点与 field 参数（lower_limit_pool 端点名与字段 ID 取自 limtupInfo
# 页面 JS 资产，2026-10-24；zt 字段组 2026-06-26 社区实测互证）。
# 上游默认投影只含 code/name/latest/change_rate——不带 field 参数封单额/
# 首封时间等扩展字段全部缺失（2026-10-23 冒烟实测）；field 组内 ID 与响应
# 行命名键一一对应（JS 字段表：330323 首次涨停时间/first_limit_up_time、
# 330325 涨停形态/limit_up_type、330333/330334/330335 为跌停时间/形态、
# 133970 封单额/order_amount、330329 几天几板/high_days 等）。
_FIELDS_ZT = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"
_FIELDS_DT = "199112,10,9001,330333,330334,330335,9002,330329,133971,133970,1968584,3475914,9003,9004"
_POOL_ENDPOINTS = (
    ("zt", "limit_up_pool", _FIELDS_ZT),
    ("dt", "lower_limit_pool", _FIELDS_DT),
    ("zb", "open_limit_pool", _FIELDS_ZT),
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        " AppleWebKit/537.36 (KHTML, like Gecko)"
        " Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html",
}

_BAN_MARK = "forbidden"


def _check_ban(status: int | None, body: str) -> None:
    """封禁探测：403 或 Nginx 封禁页 → IP 级封禁，按连接级故障语义抛错。"""
    if status == 403 or _BAN_MARK in body.lower():
        raise FetchError(
            "同花顺 IP 级封禁（Nginx forbidden，实测恢复 >5 分钟）；"
            "今日勿再重试，等次日采集",
            kind="connection",
        )


def parse_pool_payload(payload: dict, pool_type: str) -> tuple[list[dict], int]:
    """dataapi 响应 JSON → (items, 总页数)。信封异常抛 FetchError。

    status_code 非 0（上游业务错误，如端点不存在/日期无数据返回错误信封）
    归为抓取失败（非连接级）；date 无数据实测上游正常响应空 info。

    信封形态（社区实现互证 + 冒烟实测）：
    - 新版：data.info = [行dict, ...]（命名键，行列表本身即一页数据）
    - 旧版：data.info = [{"items": [...], "pages": ...}, ...]（分页 dict 列表）
      或单页 dict {"items": [...]}
    """
    if not isinstance(payload, dict):
        raise FetchError(f"THS {pool_type} 响应非 JSON 对象: {type(payload).__name__}")
    if payload.get("status_code") not in (0, None):
        raise FetchError(
            f"THS {pool_type} 业务错误: status_code={payload.get('status_code')}"
            f" msg={payload.get('status_msg')}"
        )
    data = payload.get("data") or {}
    info = data.get("info")
    if info is None:
        return [], None
    if isinstance(info, dict):  # 单页 dict 形态
        rows = info.get("items")
        if not isinstance(rows, list):
            return [], None
        try:
            pages = int(info.get("pages")) if info.get("pages") else None
        except (TypeError, ValueError):
            pages = None
        return [r for r in rows if isinstance(r, dict)], pages
    items: list[dict] = []
    pages: int | None = None
    for entry in info:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("items"), list):  # 旧版分页 dict
            items.extend(r for r in entry["items"] if isinstance(r, dict))
            try:
                if entry.get("pages"):
                    pages = max(pages or 1, int(entry["pages"]))
            except (TypeError, ValueError):
                pass
        else:  # 新版：行列表
            items.append(entry)
    return items, pages


def _fetch_pool_page(
    endpoint: str, trade_date: str, page: int, fields: str
) -> tuple[dict, int]:
    """单页请求 → (响应 JSON, HTTP status)。封禁探测在响应到手后立即执行。"""
    params = urllib.parse.urlencode(
        {
            "date": trade_date,
            "page": page,
            "limit": PAGE_LIMIT,
            "field": fields,  # 扩展字段投影（上游默认只给基础四字段）
            # 板块范围对齐调研实测口径：沪深主板+创业板+科创板
            "filter": "HS,GEM2STAR",
        }
    )
    req = urllib.request.Request(
        f"{DATAAPI}/{endpoint}?{params}", headers=dict(_HEADERS)
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 错误页读取失败不掩盖原始 HTTP 错误
            pass
        _check_ban(e.code, body)
        raise FetchError(f"THS {endpoint} HTTP 错误：status={e.code}") from e
    except Exception as e:  # noqa: BLE001 - URLError/超时/TLS 断连等传输错误
        raise FetchError(f"THS {endpoint} 传输错误：{e}") from e
    _check_ban(status, body)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise FetchError(f"THS {endpoint} 返回非法 JSON: {e}") from e
    return payload, status


def fetch_pool_records(
    endpoint: str,
    pool_type: str,
    trade_date: str,
    pause_s: float | None = None,
    fields: str = _FIELDS_ZT,
) -> list[dict]:
    """按端点抓全部分页（分页间退避），返回原始 items。单池失败抛 FetchError。

    分页终止：信封给 pages 元数据（旧版形态）按元数据；新版形态无元数据，
    按满页启发式——返回行数 = limit 视为可能还有下一页，行数 < limit 终止。
    """
    pause = POOL_PAUSE_S if pause_s is None else pause_s
    payload, _ = _fetch_pool_page(endpoint, trade_date, 1, fields)
    items, pages = parse_pool_payload(payload, pool_type)
    # 分页终止：信封有 pages 元数据按元数据；无元数据（新版形态）按满页
    # 启发式——返回行数 = limit 视为可能还有下一页，不满页即末页
    page = 2
    while page <= MAX_PAGES:
        has_more = pages is not None and page <= pages
        heuristic = pages is None and len(items) >= PAGE_LIMIT
        if not (has_more or heuristic):
            break
        sleep(pause)
        payload, _ = _fetch_pool_page(endpoint, trade_date, page, fields)
        more, more_pages = parse_pool_payload(payload, pool_type)
        items.extend(more)
        if pages is None and more_pages is not None:
            pages = more_pages
        if len(more) < PAGE_LIMIT:
            break  # 不满页：已到末页
        page += 1
    if pages is not None and pages > MAX_PAGES:
        log.warning(
            "THS %s 分页 %d 超过上限 %d，只取前 %d 页",
            pool_type, pages, MAX_PAGES, MAX_PAGES,
        )
    return items


class ThsAdapter:
    """同花顺涨停揭秘源（zt_pool 唯一承载源，issue #120）。

    fetch_zt_pool 顺序抓 涨停→跌停→炸板 三池（池间长退避）；触发封禁立即
    抛错并放弃本轮剩余池。单池失败记录 pool_errors 继续其余池（全部失败才
    抛错）——拿到的池照常落库，失败原因由工具层以 partial_error 报告。
    pool_errors 为 {池: 错误}，工具层读取后置空不影响下一次抓取。
    """

    name = "ths"

    def __init__(self, pause_s: float | None = None) -> None:
        self._pause = POOL_PAUSE_S if pause_s is None else pause_s
        self.pool_errors: dict[str, str] = {}

    def fetch_zt_pool(self, trade_date: str) -> list[dict]:
        self.pool_errors = {}
        rows: list[dict] = []
        banned = False
        for pool_type, endpoint, fields in _POOL_ENDPOINTS:
            if banned:
                self.pool_errors[pool_type] = "封禁探测命中，本轮剩余池放弃（长退避）"
                continue
            if rows:
                sleep(self._pause)  # 池间长退避（首个成功的池之后）
            try:
                records = fetch_pool_records(endpoint, pool_type, trade_date,
                                             pause_s=self._pause, fields=fields)
                rows += map_ths_pool_rows(records, pool_type)
            except FetchError as e:
                self.pool_errors[pool_type] = str(e)
                if e.kind == "connection":
                    banned = True  # IP 级封禁：剩余池必败，放弃本轮
                log.warning("THS %s 池抓取失败: %s", pool_type, e)
        if not rows and self.pool_errors:
            raise FetchError(
                f"THS 涨跌停池全部失败: {'; '.join(f'{k}: {v}' for k, v in self.pool_errors.items())}",
                kind="connection"
                if any("封禁" in v for v in self.pool_errors.values())
                else None,
            )
        if self.pool_errors:
            log.warning("THS 涨跌停池部分失败: %s", self.pool_errors)
        return rows

    # zt_pool 之外的 section THS 不承载（强势股为库内自算，见 tools_strong）
    def fetch_strong_stocks(self, trade_date: str) -> list[dict]:
        raise FetchError("THS 无强势股池（strong_stocks 为库内自算，issue #120）",
                         kind="unsupported")

    def fetch_indices(self, trade_date: str) -> list[dict]:
        raise FetchError("THS 适配器不支持 indices", kind="unsupported")

    def fetch_index_spot(self, index_code: str) -> list[dict]:
        raise FetchError("THS 适配器不支持指数补抓", kind="unsupported")

    def fetch_boards(self, trade_date: str) -> list[dict]:
        raise FetchError("THS 适配器不支持 boards", kind="unsupported")

    def fetch_lhb(self, trade_date: str) -> dict:
        raise FetchError("THS 适配器不支持 lhb", kind="unsupported")


def probe(timeout: float | None = None) -> dict:
    """封禁探测：GET 页面入口（不打 dataapi），报告可达/封禁。单次轻量请求。

    返回 {"reachable": bool, "banned": bool, "detail": str}；传输错误
    reachable=False 不归因封禁（网络不可达与封禁分开报告）。
    """
    req = urllib.request.Request(PAGE_URL, headers=dict(_HEADERS))
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status == 403 or _BAN_MARK in body.lower():
                return {"reachable": True, "banned": True,
                        "detail": f"HTTP {resp.status}，命中封禁页"}
            return {"reachable": True, "banned": False,
                    "detail": f"HTTP {resp.status}，页面正常"}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        if e.code == 403 or _BAN_MARK in body.lower():
            return {"reachable": True, "banned": True,
                    "detail": f"HTTP {e.code}，命中封禁页"}
        return {"reachable": False, "banned": False,
                "detail": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 - 传输错误
        return {"reachable": False, "banned": False, "detail": f"传输错误: {e}"}
