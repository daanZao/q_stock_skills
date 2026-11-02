"""新浪日线适配器（个股日线 fallback 第 2 位，issue #112 / spec #111）。

沿用 akshare `stock_zh_a_daily`（底层 finance.sina.com.cn klc_kl.js + 复权因子
文件，非东财接口）：JS 解密与本地复权因子合并不值得重复实现，akshare 已随
sources 依赖安装。代价是该接口无论请求区间多短都全量下载历史（实测老股
6.5–15.4s），且上游文档明示"大量抓取容易封 IP"——因此排腾讯之后作备选，
不作主源。

单位口径（2026-10-21 实测）：volume 原生为股（ADR 0008 透传）；amount 元 →
万元（ADR 0006）；turnover 为 fraction → ×100 百分数。change_percent /
change_amount / amplitude 由前收推算——多取 7 个自然日以拿到区间首日前收，
仍无前收（上市首日）则 None 不伪造。北交所 920xxx 实测可取（含 qfq 因子），
旧码按 bse_code_map 归一化到 920 新码。

全市场快照直连（issue #116，spec #111）：`SinaSnapshotAdapter` 为快照 fallback
第 3 位，绕过 akshare `stock_zh_a_spot` 封装——封装层把底层响应里的
per/pb/mktcap/nmc/turnoverratio 五列丢弃（实测 akshare 1.18.94
stock_zh_a_sina.py:64-83），直连原始接口 Market_Center.getHQNodeData 只缺
振幅/量比 2 字段。ticktime 仅时分秒无日期 → trade_date=None（受 #78 防线
限制，仅无参调用可落库）。上游官方明示"重复运行会暂时封 IP"，只做第三重
兜底。响应字段（2026-10-21 实录 tests/fixtures/sina/spot_page1.json）：
trade/settlement/open/high/low/pricechange/changepercent、volume(股)、
amount(元)、per、pb、mktcap/nmc（万元）、turnoverratio。

指数快照（issue #117 / spec #111）：`hq.sinajs.cn/list=sh000001,...` 批量报价
薄封装（须带 `Referer: https://finance.sina.com.cn`，响应 GBK），作腾讯批量
报价之后的第 2 位。字段：name/今开/昨收/最新/最高/最低/成交量/成交额（元）
/日期/时间；量比无源（单快照无 5 日均量）置 None，涨跌额/涨跌幅/振幅由现价
与昨收推算。**单位陷阱（2026-10-23 实测）**：沪系指数成交量为手（×100 入库），
深系为股（透传）——同响应内口径不一致，映射层按市场前缀分别换算。新浪
指数日线（stock_zh_index_daily）无成交额列，不入指数日线链（链：腾讯 →
baostock，issue #117）。
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from time import sleep

from ..dates import add_days
from ._bse_codes import to_market_code
from ._daily_common import adjust_param, derive_change_fields
from ._num import _int, _num, _wan
from .base import FetchError, import_akshare

log = logging.getLogger(__name__)

SINA_SPOT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
SINA_SPOT_COUNT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount"
)
SINA_PAGE_SIZE = 100  # 实测 num>100 也只返回 100 行；全市场约 56 页/轮
SINA_PAGE_PAUSE_S = 0.2  # 上游明示重复运行封 IP，翻页保守限速（第三重兜底本就低频）

SINA_HQ_URL = "https://hq.sinajs.cn/list="
SINA_HQ_TIMEOUT_S = 20
SINA_HQ_HEADERS = {
    "Referer": "https://finance.sina.com.cn",  # 上游校验，缺省 403
    "User-Agent": "Mozilla/5.0",
}


def to_sina_index_code(index_code: str) -> str:
    """指数代码 → 新浪代码（399xxx 深系 sz，其余沪系 sh；与腾讯/ baostock 同口径）。"""
    if index_code.startswith("399"):
        return f"sz{index_code}"
    if index_code[:1] in "015":
        return f"sh{index_code}"
    raise FetchError(f"无法识别的指数代码: {index_code}", kind="unsupported")


def parse_sina_index_quotes(text: str) -> list[dict]:
    """新浪指数批量报价文本 → INDEX_FIELDS 行 dict（纯函数，fixture 可测）。

    `var hq_str_sh000001="名称,今开,昨收,最新,最高,最低,买一,卖一,成交量,
    成交额(元),...,日期,时间"` 分号结尾；行级 trade_date 取自日期字段（位置
    -3/-4，尾部空字段数随标的略有出入），供工具层日期相符校验（issue #78）。
    空报价（非交易时段）跳过；涨跌额/涨跌幅/振幅由最新价与昨收推算（derive_
    change_fields）；量比无源置 None；成交量沪系手 ×100、深系股透传（ADR 0008）。
    """
    rows: list[dict] = []
    for stmt in text.split(";"):
        stmt = stmt.strip()
        if "=" not in stmt:
            continue
        head, _, payload = stmt.partition("=\"")
        if not head.startswith("var hq_str_") or not payload.rstrip('"'):
            continue
        f = payload.rstrip('"').split(",")
        if len(f) < 32:
            continue  # 字段数不足的异常行不臆造
        code = head.replace("var hq_str_", "")
        # 实测尾部为 [..., 日期, 时间, "00", ""]（34 列）或 [..., 日期, 时间, "00"]
        # （33 列，深市）：日期恒在时间前一位，从尾部倒序找第一个 yyyy-mm-dd。
        trade_date = None
        for v in reversed(f):
            s = str(v).strip()
            if len(s) == 10 and s[4] == "-" and s[:4].isdigit():
                trade_date = s.replace("-", "")
                break
        latest = _num(f[3])
        pre_close = _num(f[2])
        high = _num(f[4])
        low = _num(f[5])
        change_amount, change_percent, amplitude = derive_change_fields(
            latest, high, low, pre_close
        )
        volume = _int(f[8])
        if volume is not None and code.startswith("sh"):
            volume *= 100  # 沪系指数成交量为手（ADR 0008）；深系为股透传
        rows.append(
            {
                "trade_date": trade_date,
                "index_code": code[2:],
                "index_name": str(f[0]),
                "latest_price": latest,
                "change_percent": change_percent,
                "change_amount": change_amount,
                "volume": volume,
                "amount": _wan(f[9]),  # 元 → 万元（ADR 0006）
                "amplitude": amplitude,
                "high": high,
                "low": low,
                "open": _num(f[1]),
                "pre_close": pre_close,
                "volume_ratio": None,  # 新浪快照无量比（不伪造）
            }
        )
    return rows


def fetch_sina_index_quotes(sina_codes) -> str:
    """请求新浪指数批量报价，返回 GBK 解码后的文本。"""
    req = urllib.request.Request(
        SINA_HQ_URL + ",".join(sina_codes), headers=SINA_HQ_HEADERS
    )
    try:
        with urllib.request.urlopen(req, timeout=SINA_HQ_TIMEOUT_S) as resp:
            return resp.read().decode("gbk", errors="replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"新浪指数报价 HTTP 错误：status={e.code}") from e
    except Exception as e:  # noqa: BLE001 - URLError/超时/TLS 断连等传输错误
        raise FetchError(f"新浪指数报价传输错误：{e}") from e


_PRE_CLOSE_LOOKBACK_DAYS = 7  # 覆盖长假，保证区间首日前收可取


def map_sina_rows(records: list[dict]) -> list[dict]:
    """akshare stock_zh_a_daily 记录 → BAR_FIELDS bar dict（按时间升序）。

    上游 amount 为元、turnover 为 fraction，在此收敛到库内口径（ADR 0006）；
    volume 原生为股透传（ADR 0008）。
    """
    bars: list[dict] = []
    prev_close = None
    for r in records:
        raw_date = r.get("date")
        if raw_date is None:
            continue
        trade_date = str(raw_date)[:10].replace("-", "")
        close = _num(r.get("close"))
        high = _num(r.get("high"))
        low = _num(r.get("low"))
        amount = _num(r.get("amount"))
        turnover = _num(r.get("turnover"))
        change_amount, change_percent, amplitude = derive_change_fields(
            close, high, low, prev_close
        )
        bars.append(
            {
                "trade_date": trade_date,
                "open": _num(r.get("open")),
                "high": high,
                "low": low,
                "close": close,
                "volume": _int(r.get("volume")),
                "amount": amount / 1e4 if amount is not None else None,
                "amplitude": amplitude,
                "change_percent": change_percent,
                "change_amount": change_amount,
                "turnover_rate": turnover * 100 if turnover is not None else None,
            }
        )
        if close is not None:
            prev_close = close
    return bars


class SinaAdapter:
    """新浪个股日线源（fallback 第 2 位）：腾讯不可用时的备选。"""

    name = "sina"

    def fetch_daily(
        self, stock_code: str, start: str, end: str, adj: str = "qfq"
    ) -> list[dict]:
        adjust = adjust_param("新浪", adj)
        ak = import_akshare()
        symbol = to_market_code(stock_code)
        fetch_start = add_days(start, -_PRE_CLOSE_LOOKBACK_DAYS)
        try:
            df = ak.stock_zh_a_daily(
                symbol=symbol,
                start_date=fetch_start,
                end_date=end,
                adjust=adjust,
            )
        except Exception as e:  # noqa: BLE001 - 上游任何失败都收敛为该源失败
            raise FetchError(f"新浪日线抓取失败（{symbol}）: {e}") from e
        if df is None or df.empty:
            return []
        bars = map_sina_rows(df.to_dict("records"))
        return [b for b in bars if start <= b["trade_date"] <= end]

    # ---------------------------------------------------------------- 指数面（issue #117）

    def fetch_indices(self, trade_date: str) -> list[dict]:
        """7 大指数批量快照：hq.sinajs.cn 定点批量（沪 sh/深 sz 各指数同源族），
        行级 trade_date 取自响应日期字段，供工具层日期相符校验（issue #78）。"""
        from .tencent_adapter import MAJOR_INDEX_TX_CODES

        codes = [to_sina_index_code(c[2:]) for c in MAJOR_INDEX_TX_CODES]
        rows = parse_sina_index_quotes(fetch_sina_index_quotes(codes))
        if not rows:
            raise FetchError("新浪指数批量报价返回为空")
        return rows

    def fetch_index_spot(self, index_code: str) -> list[dict]:
        """单 code 指数行情（issue #49 补漏）：批量端点按 code 定点请求；
        空结果表示上游无该指数行情（由工具层转下一源）。"""
        try:
            sina_code = to_sina_index_code(index_code)
        except FetchError:
            return []
        return [
            r
            for r in parse_sina_index_quotes(fetch_sina_index_quotes((sina_code,)))
            if r["index_code"] == index_code
        ]

    def fetch_index_daily(self, index_code: str, start: str, end: str) -> list[dict]:
        """新浪指数日线无成交额列（index_daily.amount 会断供），不入指数日线链。

        指数日线链为 腾讯 → baostock（issue #117）；新浪日线仅留作科创50 历史
        回补的人工备选（akshare stock_zh_index_daily，全历史 T+1）。
        """
        raise FetchError(
            "新浪指数日线不入链（无成交额列）；指数日线链：腾讯 → baostock",
            kind="unsupported",
        )


# ---------------------------------------------------------------- 全市场快照直连
# （issue #116，spec #111：快照链第三重兜底）


def parse_sina_spot_rows(records: list[dict]) -> list[dict]:
    """新浪原始快照记录 → SNAPSHOT_FIELDS 行（纯函数）。

    单位收敛：amount 元 → 万元、mktcap/nmc 原生万元透传、volume 原生股透传
    （ADR 0006/0008）；振幅/量比上游无字段置 None 不伪造；北交所 symbol 带
    bj 前缀在此剥掉。
    """
    rows: list[dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        symbol = str(r.get("symbol") or "")
        code = symbol[2:] if symbol[:2] in ("sh", "sz", "bj") else symbol
        if not code:
            continue
        amount = _num(r.get("amount"))  # 元 → 万元（ADR 0006）
        rows.append(
            {
                "stock_code": code,
                "stock_name": r.get("name"),
                "latest_price": _num(r.get("trade")),
                "change_percent": _num(r.get("changepercent")),
                "change_amount": _num(r.get("pricechange")),
                "amplitude": None,
                "high": _num(r.get("high")),
                "low": _num(r.get("low")),
                "open": _num(r.get("open")),
                "pre_close": _num(r.get("settlement")),
                "volume_ratio": None,
                "turnover_rate": _num(r.get("turnoverratio")),
                "pe_ratio": _num(r.get("per")),
                "pb_ratio": _num(r.get("pb")),
                "volume": _int(r.get("volume")),
                "amount": None if amount is None else amount / 1e4,
                "market_cap": _num(r.get("mktcap")),  # 原生万元（ADR 0006）
                "float_cap": _num(r.get("nmc")),
            }
        )
    rows.sort(key=lambda r: r["stock_code"])
    return rows


def _sina_get(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"新浪快照 HTTP 错误：status={e.code}") from e
    except Exception as e:  # noqa: BLE001 - URLError/超时等传输错误
        raise FetchError(f"新浪快照传输错误：{e}") from e


def fetch_sina_spot_page(page: int) -> list[dict]:
    """请求快照一页（100 行），返回原始记录列表。"""
    params = urllib.parse.urlencode(
        {
            "page": str(page),
            "num": str(SINA_PAGE_SIZE),
            "sort": "symbol",
            "asc": "1",
            "node": "hs_a",
        }
    )
    text = _sina_get(f"{SINA_SPOT_URL}?{params}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise FetchError(f"新浪快照返回非法 JSON: {e}") from e
    if not isinstance(payload, list):
        raise FetchError("新浪快照返回非 JSON 数组")
    return [r for r in payload if isinstance(r, dict)]


def fetch_sina_spot_count() -> int:
    """hs_a 节点股票总数（Market_Center.getHQNodeStockCount）。"""
    text = _sina_get(f"{SINA_SPOT_COUNT_URL}?node=hs_a")
    try:
        return int(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        raise FetchError(f"新浪快照总数解析失败: {e}") from e


class SinaSnapshotAdapter:
    """全市场快照 fallback 第 3 位（issue #116）：新浪原始接口直连。

    绕过 akshare `stock_zh_a_spot` 封装（其丢弃 per/pb/mktcap/nmc/
    turnoverratio 五列，接线即缺 7 字段）；node=hs_a 含北交所（实录
    tests/fixtures/sina/spot_page1.json 首行即 bj920000）。ticktime 无日期
    → trade_date=None。上游明示重复运行封 IP，仅作第三重兜底，翻页保守限速。
    """

    name = "sina"

    def fetch_market_snapshot(self) -> dict:
        total = fetch_sina_spot_count()
        records = fetch_sina_spot_page(1)
        for page in range(2, max(1, -(-total // SINA_PAGE_SIZE)) + 1):
            if len(records) >= total:
                break
            sleep(SINA_PAGE_PAUSE_S)
            records.extend(fetch_sina_spot_page(page))
        return {"trade_date": None, "rows": parse_sina_spot_rows(records)}
