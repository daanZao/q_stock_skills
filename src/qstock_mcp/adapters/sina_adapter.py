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
"""

import logging

from ..dates import add_days
from ._bse_codes import to_market_code
from ._daily_common import ADJUST_PARAMS, derive_change_fields
from ._eastmoney import _int, _num
from .base import FetchError

log = logging.getLogger(__name__)

_PRE_CLOSE_LOOKBACK_DAYS = 7  # 覆盖长假，保证区间首日前收可取


def _ak():
    try:
        import akshare as ak
    except ImportError as e:
        raise FetchError("akshare 未安装：pip install qstock-mcp[sources]") from e
    return ak


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
        if adj not in ADJUST_PARAMS:
            raise FetchError(f"新浪不支持复权口径: {adj}", kind="unsupported")
        ak = _ak()
        symbol = to_market_code(stock_code)
        fetch_start = add_days(start, -_PRE_CLOSE_LOOKBACK_DAYS)
        try:
            df = ak.stock_zh_a_daily(
                symbol=symbol,
                start_date=fetch_start,
                end_date=end,
                adjust=ADJUST_PARAMS[adj],
            )
        except Exception as e:  # noqa: BLE001 - 上游任何失败都收敛为该源失败
            raise FetchError(f"新浪日线抓取失败（{symbol}）: {e}") from e
        if df is None or df.empty:
            return []
        bars = map_sina_rows(df.to_dict("records"))
        return [b for b in bars if start <= b["trade_date"] <= end]
