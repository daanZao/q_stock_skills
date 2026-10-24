"""全市场快照主源（腾讯批量报价）的默认股票清单提供者（issue #116）。

qt.gtimg.cn 批量报价是"按清单报价"接口，需外部供给全市场代码。提供顺序：

1. akshare `stock_info_a_code_name`——交易所官网源（非东财），始终含当日
   最新上市/北交所 920 新码，是调研报告推荐输入（snapshot-alternatives §6）；
2. 库内 `stock_list` 兜底——akshare 未安装/不可达时，用 init 轻量初始化
   已沉淀的清单（可能滞后于最新上市，降级语义记日志）。

两者皆不可得抛 FetchError，由 fallback 链转到腾讯 rank 榜单（免清单）。
"""

import logging

from .base import FetchError, import_akshare

log = logging.getLogger(__name__)


def _codes_from_akshare() -> list[str]:
    ak = import_akshare()
    df = ak.stock_info_a_code_name()
    if df is None or df.empty:
        raise FetchError("akshare 股票清单返回为空")
    return [str(r["code"]) for r in df.to_dict("records")]


def _codes_from_db() -> list[str]:
    from ..db import connect
    from ..repository import select_stock_codes

    conn, err = connect()
    if err:
        raise FetchError(f"库内清单不可用：{err}")
    try:
        codes = select_stock_codes(conn)
    finally:
        conn.close()
    if not codes:
        raise FetchError("库内 stock_list 为空")
    return codes


def default_snapshot_codes() -> list[str]:
    """→ 6 位全市场代码列表；akshare 交易所官网清单优先，库内 stock_list 兜底。"""
    ak_error: str | None = None
    try:
        codes = _codes_from_akshare()
        if codes:
            return codes
        ak_error = "akshare 股票清单返回为空"
    except Exception as e:  # noqa: BLE001 - akshare 任何失败都降级到库内清单
        ak_error = str(e)
        log.warning("akshare 股票清单失败，降级库内 stock_list: %s", e)
    try:
        codes = _codes_from_db()
    except FetchError as e:
        raise FetchError(
            f"快照股票清单双路皆不可用（akshare: {ak_error}; 库内: {e}）"
        ) from e
    log.info("快照股票清单取自库内 stock_list（akshare 不可用降级）")
    return codes
