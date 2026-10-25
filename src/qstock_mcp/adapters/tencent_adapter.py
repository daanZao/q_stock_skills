"""腾讯行情薄封装（个股日线 issue #112 / spec #111；全市场快照 issue #116）。

自研直调腾讯裸接口（仅标准库 urllib，薄封装先例见 mx_client.py），不使用
akshare `stock_zh_a_hist_tx` / `stock_zh_index_daily_tx`：实测封装层把第 6 列
成交量误命名为 amount、丢弃真实成交额列，且北交所 920xxx 不在其代码归一化范围。

全市场快照（issue #116，spec #111 决策地图）：三条快照承载路径里腾讯占两条——

- `TencentSnapshotAdapter`（主源）：qt.gtimg.cn 批量报价（`/q=sh600000,...`），
  18 个快照字段全覆盖（2026-10-20 盘中逐项核对），时间戳字段含日期可过 #78
  日期防线，amount 原生万元（ADR 0006）、市值原生亿（×1e4 收敛到万元）、
  成交量主板/北交所为手（×100 收敛到股，ADR 0008）、科创板 sh688/sh689 原生
  股不换算（与日线同规则）。需外部股票清单驱动（默认提供者见
  `_snapshot_codes.default_snapshot_codes`：akshare 交易所官网清单 → 库内
  stock_list 兜底）。
- `TencentRankAdapter`（fallback 第 2 位）：proxy.finance.qq.com 沪深京榜单
  （getBoardRankList，200 行/页约 28 页/轮），免清单；缺 open/high/low/
  pre_close 置 None 不伪造，涨跌家数口径由工具层改按 zdf 判定
  （repository 聚合 SQL，issue #116）；pe_ratio 为 pe_ttm 口径（与东财
  "市盈率-动态"口径切换，见 docs/research/2026-10-20-snapshot-alternatives.md
  §7 未验证清单）。无时间戳字段 → trade_date=None。

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

指数面（issue #117 / spec #111，2026-10-23 收盘后实测）：

- 指数快照：`qt.gtimg.cn` 批量报价 `q=sh000001,sz399001,...`，单请求 7 大
  指数一次取齐，是所有候选源中唯一覆盖 `volume_ratio` 的源。响应 GBK 编码、
  `v_sh000001="1~名称~代码~..."` 每行一只。列位（与个股同源布局，逐字段
  实测验算）：[36] 成交量（手）、[37] 成交额（万元，原生免换算）、
  [31]/[32] 涨跌额/涨跌幅、[33]/[34]/[5] 最高/最低/今开、[4] 昨收、
  [43] 振幅、[49] **量比**、[30] 行情时间戳（行级 trade_date 取自此）。
  注意 [38] 是换手率（与 K 线 turnover 列逐日实测一致），不是量比——
  调研报告（2026-10-20-index-alternatives.md §2.1）把 [38] 标为量比，
  本层以 10-23 收盘数据复核证伪；量比字段位为 [49]。
- 指数日线：复用上述 K 线薄封装（指数为手 ×100、amount 原生万元、turnover
  原生列实测有效），请求复权口径为空（指数无复权概念，上游只回 `day` 键，
  不触发复权回落标记）。收盘后当晚含当日行（T+0，#117 实测 10-23 行
  16:15 已可取）。
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from time import sleep
from typing import Sequence

from ._bse_codes import to_market_code
from ._daily_common import adjust_param, derive_change_fields
from ._num import _int, _num
from .base import FetchError

log = logging.getLogger(__name__)

KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
QUOTE_URL = "https://qt.gtimg.cn/q="
TIMEOUT_SEC = 20
MAX_BARS = 640  # 上游单次返回上限（实测请求 1000/3000 根仍只给 640）
MAX_CHUNKS = 64  # 640 × 64 ≈ 4 万根，覆盖 A 股全历史
CHUNK_PAUSE_S = 0.3  # 多段回溯时对腾讯限速（实测连发 5–6 个请求触发 TLS 断连）

QT_QUOTES_URL = "https://qt.gtimg.cn/q="
QT_BATCH_SIZE = 80  # 实测单次 80 只全部返回（响应约 37KB）；5576 只实测 70 请求/轮
QT_BATCH_PAUSE_S = 0.1  # 批量报价逐批限速（qt.gtimg.cn 为轻量报价端点，略保守）

RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
RANK_PAGE_SIZE = 200  # 榜单分页上限；全市场约 28 页/轮
RANK_PAGE_PAUSE_S = 0.3  # 与 K 线同域（proxy.finance.qq.com），沿用实测限速

# 指数快照批量请求的 7 大指数（腾讯代码形态；与 tools_init.MAJOR_INDICES 同一
# 清单，tests 有等价性守护）。适配器只取复盘白名单，不捞全量指数口径。
MAJOR_INDEX_TX_CODES = (
    "sh000001",
    "sz399001",
    "sz399006",
    "sh000300",
    "sh000016",
    "sh000905",
    "sh000688",
)


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


def _collect_kline_rows(
    tx_code: str, start: str, end: str, adjust: str
) -> tuple[list[list], bool]:
    """逐段向前回溯收集 [start, end] 的原始 K 线行（升序）与复权回落标记。

    上游单次最多 MAX_BARS 根且忽略 start（只按 end 向前取），因此循环回溯；
    区间首日前收随回溯一并取得（>= start 的前一行）。fell_back 为请求复权键
    缺失、上游按 day 返回的标志（复权回落可见化，issue #112）。
    """
    collected: dict[str, list] = {}
    cursor = end
    fell_back = False
    for chunk in range(MAX_CHUNKS):
        if chunk:
            # 全历史回溯逐段连续请求，按实测限速留出间隔
            sleep(CHUNK_PAUSE_S)
        rows, chunk_fell_back = fetch_kline_rows(tx_code, cursor, adjust)
        if chunk_fell_back:
            fell_back = True
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
    return [collected[d] for d in sorted(collected)], fell_back


def to_tx_index_code(index_code: str) -> str:
    """指数代码 → 腾讯代码（399xxx 深系 sz，其余沪系 sh；与 baostock 路由同口径）。"""
    if index_code.startswith("399"):
        return f"sz{index_code}"
    if index_code[:1] in "015":
        return f"sh{index_code}"
    raise FetchError(f"无法识别的指数代码: {index_code}", kind="unsupported")


# 指数批量报价响应为 GBK 编码的 `v_sh000001="1~名称~代码~..."` 行集（分号结尾）。
def parse_tx_index_quotes(text: str) -> list[dict]:
    """腾讯指数批量报价文本 → INDEX_FIELDS 行 dict（纯函数，fixture 可测）。

    列位见模块 docstring（2026-10-23 收盘实测验算）：量比=[49] 是候选源中唯一
    覆盖 volume_ratio 的字段；行级 trade_date 取自行情时间戳 [30]（yyyymmdd），
    供工具层做日期相符校验（issue #78）。空报价（非交易时段未开盘）跳过。
    """
    rows: list[dict] = []
    for stmt in text.split(";"):
        stmt = stmt.strip()
        if "=" not in stmt:
            continue
        head, _, payload = stmt.partition("=\"")
        if not head.startswith("v_") or not payload.rstrip('"'):
            continue
        f = payload.rstrip('"').rstrip(";").split("~")
        if len(f) < 50:
            continue  # 字段数不足的异常行不臆造
        ts = str(f[30]).strip()
        rows.append(
            {
                "trade_date": ts[:8] if ts.isdigit() and len(ts) >= 8 else None,
                "index_code": str(f[2]),
                "index_name": str(f[1]),
                "latest_price": _num(f[3]),
                "change_percent": _num(f[32]),
                "change_amount": _num(f[31]),
                "volume": _int(f[36]) and _int(f[36]) * 100,  # 手 → 股（ADR 0008）
                "amount": _num(f[37]),  # 原生万元（ADR 0006）
                "amplitude": _num(f[43]),
                "high": _num(f[33]),
                "low": _num(f[34]),
                "open": _num(f[5]),
                "pre_close": _num(f[4]),
                "volume_ratio": _num(f[49]),
            }
        )
    return rows


def fetch_tx_index_quotes(tx_codes: Sequence[str]) -> str:
    """请求腾讯指数批量报价，返回 GBK 解码后的文本。"""
    req = urllib.request.Request(
        QUOTE_URL + ",".join(tx_codes), headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return resp.read().decode("gbk", errors="replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"腾讯指数报价 HTTP 错误：status={e.code}") from e
    except Exception as e:  # noqa: BLE001 - URLError/超时/TLS 断连等传输错误
        raise FetchError(f"腾讯指数报价传输错误：{e}") from e


class TencentAdapter:
    """腾讯源（个股日线 fallback 第 1 位；指数快照/指数日线主源，issue #117）。

    个股日线：收盘后当晚含当日行（#104 实测）。指数快照：qt.gtimg.cn 批量
    报价，唯一覆盖量比字段的源。指数日线：K 线薄封装 T+0 当日行，收盘后
    当晚可得；科创50（000688）覆盖（baostock 实测不支持该指数）。

    `adjust_fallback`：fetch_daily 期间上游缺复权键按 day 回落时置 True
    （复权回落可见化，issue #112）；对拍与工具层 note 据此标注。指数日线
    请求不带复权口径（指数无复权概念），不会触发该标记。
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
        ordered, fell_back = _collect_kline_rows(tx_code, start, end, adjust)
        if fell_back:
            log.warning(
                "腾讯 %s 缺 %s 复权键，上游按 day 回落"
                "（无复权事件或不提供调整序列）",
                tx_code,
                adjust or "none",
            )
            self.adjust_fallback = True
        bars = map_tencent_rows(tx_code, ordered)
        return [b for b in bars if start <= b["trade_date"] <= end]

    # ---------------------------------------------------------------- 指数面（issue #117）

    def fetch_indices(self, trade_date: str) -> list[dict]:
        """7 大指数批量快照：单请求一次取齐（MAJOR_INDEX_TX_CODES），
        行级 trade_date 取自行情时间戳，供工具层日期相符校验（issue #78）。"""
        rows = parse_tx_index_quotes(fetch_tx_index_quotes(MAJOR_INDEX_TX_CODES))
        if not rows:
            raise FetchError("腾讯指数批量报价返回为空")
        return rows

    def fetch_index_spot(self, index_code: str) -> list[dict]:
        """单 code 指数行情（issue #49 补漏）：批量端点按 code 定点请求；
        空结果表示上游无该指数行情（由工具层转下一源）。"""
        try:
            tx_code = to_tx_index_code(index_code)
        except FetchError:
            return []
        return [
            r
            for r in parse_tx_index_quotes(fetch_tx_index_quotes((tx_code,)))
            if r["index_code"] == index_code
        ]

    def fetch_index_daily(self, index_code: str, start: str, end: str) -> list[dict]:
        """指数日线 [start, end]（不复权；T+0 当日行收盘后当晚可得）。

        复用个股 K 线薄封装（指数为手 ×100、amount 原生万元、turnover 原生列
        实测有效）；科创50（000688）上游只回 `day` 键，按不复权直取（指数无
        复权概念，不触发复权回落标记）。空区间返回 []（非交易日，非错误）。
        """
        tx_code = to_tx_index_code(index_code)
        ordered, _fell_back = _collect_kline_rows(tx_code, start, end, "")
        bars = map_tencent_rows(tx_code, ordered)
        return [b for b in bars if start <= b["trade_date"] <= end]


def _previous_calendar_day(yyyymmdd: str) -> str:
    """前一个自然日（不感知周末/节假日；仅用于回溯游标推进）。"""
    d = datetime.strptime(yyyymmdd, "%Y%m%d").date() - timedelta(days=1)
    return d.strftime("%Y%m%d")


# ---------------------------------------------------------------- 全市场快照
# （issue #116，spec #111：快照链 腾讯批量报价 → 腾讯 rank 榜单 → 新浪直连）


def _at(fields: list[str], idx: int) -> str:
    return fields[idx] if len(fields) > idx else ""


def parse_qt_quotes(text: str) -> tuple[list[dict], str | None]:
    """qt.gtimg.cn 批量报价响应文本 → (SNAPSHOT_FIELDS 行, 交易日)（纯函数）。

    响应 GBK 编码，每行 `v_sh600000="1~名称~代码~...";`。字段下标经 2026-10-20
    盘中逐项核对（tests/fixtures/tencent/qt_batch_quotes.txt 实录）：

        1/2 名称/代码  3 最新价  4 昨收  5 今开  6 成交量(手; 科创板原生股)
        30 时间戳(含日期)  31/32 涨跌额/涨跌幅%  33/34 最高/最低
        37 成交额(万元)  38 换手率%  39 PE(TTM)  43 振幅%
        44/45 流通/总市值(亿)  46 市净率  49 量比

    空字段（停牌等）映射 None 不伪造；无代码的残缺行跳过。交易日取各行
    时间戳的最大日期（yyyymmdd），无时间戳返回 None。
    """
    rows: list[dict] = []
    stamps: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        var, _, body = line.partition("=")
        # 变量名 v_<symbol>（如 v_sh688981）是权威市场前缀来源：科创板
        # 成交量原生为股，判断须用带前缀代码（_is_star_market）
        symbol = var.strip()[2:] if var.strip().startswith("v_") else ""
        body = body.strip().strip(";").strip('"')
        if not body:
            continue
        f = body.split("~")
        code = _at(f, 2)
        if not code:
            continue
        stamp = _at(f, 30)
        if len(stamp) >= 8 and stamp[:8].isdigit():
            stamps.append(stamp[:8])
        volume = _int(_at(f, 6))
        if volume is not None and not _is_star_market(symbol):
            volume *= 100  # 手 → 股（ADR 0008；科创板原生股不换算）
        market_cap = _num(_at(f, 45))  # 亿 → 万元（ADR 0006）
        float_cap = _num(_at(f, 44))
        rows.append(
            {
                "stock_code": code,
                "stock_name": _at(f, 1) or None,
                "latest_price": _num(_at(f, 3)),
                "change_percent": _num(_at(f, 32)),
                "change_amount": _num(_at(f, 31)),
                "amplitude": _num(_at(f, 43)),
                "high": _num(_at(f, 33)),
                "low": _num(_at(f, 34)),
                "open": _num(_at(f, 5)),
                "pre_close": _num(_at(f, 4)),
                "volume_ratio": _num(_at(f, 49)),
                "turnover_rate": _num(_at(f, 38)),
                "pe_ratio": _num(_at(f, 39)),
                "pb_ratio": _num(_at(f, 46)),
                "volume": volume,
                "amount": _num(_at(f, 37)),  # 原生万元（ADR 0006）
                "market_cap": None if market_cap is None else market_cap * 1e4,
                "float_cap": None if float_cap is None else float_cap * 1e4,
            }
        )
    rows.sort(key=lambda r: r["stock_code"])
    return rows, (max(stamps) if stamps else None)


def fetch_qt_quotes(symbols: list[str]) -> str:
    """请求一批（≤QT_BATCH_SIZE）带市场前缀代码的批量报价，返回 GBK 解码文本。"""
    url = QT_QUOTES_URL + ",".join(symbols)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return resp.read().decode("gbk", errors="replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"腾讯批量报价 HTTP 错误：status={e.code}") from e
    except Exception as e:  # noqa: BLE001 - URLError/超时/TLS 断连等传输错误
        raise FetchError(f"腾讯批量报价传输错误：{e}") from e


class TencentSnapshotAdapter:
    """全市场快照主源（issue #116）：qt.gtimg.cn 批量报价，18 字段全覆盖。

    需股票清单驱动：`codes_provider` 返回 6 位代码列表（缺省
    `_snapshot_codes.default_snapshot_codes`：akshare 交易所官网清单 → 库内
    stock_list 兜底）。清单为空视为该源失败（转 rank 榜单），不返回空结果——
    空结果在 fallback 链里是"上游无数据"成功语义，与清单缺失语义不同。
    """

    name = "tencent"

    def __init__(self, codes_provider=None) -> None:
        self._codes_provider = codes_provider

    def _codes(self) -> list[str]:
        provider = self._codes_provider
        if provider is None:
            from ._snapshot_codes import default_snapshot_codes

            provider = default_snapshot_codes
        codes = [to_market_code(c) for c in provider()]
        if not codes:
            raise FetchError("腾讯批量报价快照：股票清单为空")
        return sorted(set(codes))

    def fetch_market_snapshot(self) -> dict:
        rows: list[dict] = []
        stamps: list[str] = []
        codes = self._codes()
        for i in range(0, len(codes), QT_BATCH_SIZE):
            if i:
                sleep(QT_BATCH_PAUSE_S)
            batch_rows, trade_date = parse_qt_quotes(
                fetch_qt_quotes(codes[i : i + QT_BATCH_SIZE])
            )
            rows.extend(batch_rows)
            if trade_date:
                stamps.append(trade_date)
        return {
            "trade_date": max(stamps) if stamps else None,
            "rows": rows,
        }


def parse_rank_page(payload: dict) -> list[dict]:
    """腾讯 rank 榜单单页 JSON → SNAPSHOT_FIELDS 行（纯函数）。

    字段经 2026-10-20 盘中核对（tests/fixtures/tencent/rank_page1.json 实录，
    200 行/页含沪深京）：

        code 带市场前缀  zxj 最新价  zd 涨跌额  zdf 涨跌幅%  zf 振幅%
        hsl 换手率%  lb 量比  pe_ttm 市盈率(TTM)  pn 市净率
        ltsz/zsz 流通/总市值(亿)  turnover 成交额(万元)  volume 成交量(手)

    缺 open/high/low/pre_close（榜单不带 OHLC）置 None 不伪造；涨跌家数口径
    由工具层改按 zdf 判定（repository 聚合 SQL，issue #116）。
    """
    data = payload.get("data") or {}
    raw_rows = data.get("rank_list")
    if not isinstance(raw_rows, list):
        raise FetchError("腾讯 rank 榜单响应缺少 rank_list")
    rows: list[dict] = []
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get("code") or "")
        code = code[2:] if code[:2] in ("sh", "sz", "bj") else code
        if not code:
            continue
        volume = _num(r.get("volume"))  # 手 → 股（ADR 0008）
        market_cap = _num(r.get("zsz"))  # 亿 → 万元（ADR 0006）
        float_cap = _num(r.get("ltsz"))
        rows.append(
            {
                "stock_code": code,
                "stock_name": r.get("name"),
                "latest_price": _num(r.get("zxj")),
                "change_percent": _num(r.get("zdf")),
                "change_amount": _num(r.get("zd")),
                "amplitude": _num(r.get("zf")),
                "high": None,
                "low": None,
                "open": None,
                "pre_close": None,
                "volume_ratio": _num(r.get("lb")),
                "turnover_rate": _num(r.get("hsl")),
                "pe_ratio": _num(r.get("pe_ttm")),
                "pb_ratio": _num(r.get("pn")),
                "volume": None if volume is None else volume * 100,
                "amount": _num(r.get("turnover")),  # 原生万元（ADR 0006）
                "market_cap": None if market_cap is None else market_cap * 1e4,
                "float_cap": None if float_cap is None else float_cap * 1e4,
            }
        )
    return rows


def fetch_rank_page(offset: int) -> dict:
    """请求榜单一页（200 行），返回原始 JSON payload。"""
    params = urllib.parse.urlencode(
        {
            "_appver": "11.17.0",
            "board_code": "aStock",
            "sort_type": "price",
            "direct": "down",
            "offset": str(offset),
            "count": str(RANK_PAGE_SIZE),
        }
    )
    req = urllib.request.Request(
        f"{RANK_URL}?{params}", headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise FetchError(f"腾讯 rank 榜单 HTTP 错误：status={e.code}") from e
    except FetchError:
        raise
    except Exception as e:  # noqa: BLE001 - URLError/超时/非法 JSON 等
        raise FetchError(f"腾讯 rank 榜单传输错误：{e}") from e
    if not isinstance(payload, dict):
        raise FetchError("腾讯 rank 榜单返回非 JSON 对象")
    return payload


class TencentRankAdapter:
    """全市场快照 fallback 第 2 位（issue #116）：腾讯沪深京榜单，免清单。

    200 行/页按 data.total 翻页（约 28 页/轮），按 code 去重（上游翻页边界
    实测有重叠风险）；无时间戳字段 → trade_date=None（受 #78 防线限制，
    仅无参调用可落库）。pe_ratio 为 pe_ttm 口径（与东财动态口径切换）。
    """

    name = "tencent_rank"

    def fetch_market_snapshot(self) -> dict:
        payload = fetch_rank_page(0)
        total = int((payload.get("data") or {}).get("total") or 0)
        rows = parse_rank_page(payload)
        for page in range(1, max(1, -(-total // RANK_PAGE_SIZE))):
            if len(rows) >= total:
                break
            sleep(RANK_PAGE_PAUSE_S)
            rows.extend(parse_rank_page(fetch_rank_page(page * RANK_PAGE_SIZE)))
        seen: set[str] = set()
        deduped = []
        for r in rows:
            if r["stock_code"] in seen:
                continue
            seen.add(r["stock_code"])
            deduped.append(r)
        deduped.sort(key=lambda r: r["stock_code"])
        return {"trade_date": None, "rows": deduped}
