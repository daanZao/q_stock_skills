"""proxy 能力面核心：get_fundamentals 基本面数据透传（issue #6）。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake 适配器（见
tests/）。透传契约：原样回传上游 payload（{section: 原始记录}，键名/字段名不动，
仅做 JSON 安全化），不规格化、不连接数据库、不落任何库表；输出自描述 JSON
（含实际数据源 source）；全部数据源失败时报 status:error 并给出
attempted_sources，绝不伪造数据。
"""

import logging
from typing import Sequence

from .adapters import FundamentalsAdapter, default_fundamentals_adapters
from .fetch_chain import AllSourcesFailed, fetch_fundamentals_with_fallback
from .output import error as _error

log = logging.getLogger(__name__)


def get_fundamentals(
    stock_code: str,
    adapters: Sequence[FundamentalsAdapter] | None = None,
) -> dict:
    """基本面数据透传：按个股代码返回上游原始数据，不规格化、不落库。"""
    tool = "get_fundamentals"
    params = {"stock_code": stock_code}
    if adapters is None:
        adapters = default_fundamentals_adapters()
    try:
        result = fetch_fundamentals_with_fallback(adapters, stock_code)
    except AllSourcesFailed as e:
        return _error(
            tool,
            params,
            "全部数据源失败（各源错误见 attempted_sources）",
            attempted_sources=e.attempted,
        )
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "source": result["source"],
        "data": result["data"],
        "attempted_sources": result["attempted_sources"],
    }
