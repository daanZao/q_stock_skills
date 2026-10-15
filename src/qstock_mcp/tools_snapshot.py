"""fetch/query 能力面核心：fetch_market_snapshot 全市场快照落库，query_snapshot 库内查询。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake 适配器 +
真实 PG 测试库（见 tests/）。快照契约（issue #4）：单次全市场调用，
API 返回的最新交易日优先于传入日期；按 (trade_date, stock_code) 业务键
幂等 upsert；全失败时报错并给出 attempted_sources，绝不伪造数据。
三态错误契约（issue #46，ADR 0007）：全源失败按逐源 failure_kind 聚合
error_kind——任一抓取源连接级故障（请求未获得 HTTP 响应）即
upstream_unreachable（带 retry_advice），否则 fetch_failed；by-design 不
支持的源（unsupported，如 baostock）不参与判定。上游正常响应空结果为
no_data_for_date（status ok + empty_kind + note），语义正常非失败。
query_snapshot 服务端聚合与排序取前 N（issue #48）：aggregate=breadth,amount
返回涨跌家数（含沪/深/北分市场；最新价或昨收为 NULL 的停牌行计入 no_price
桶，up+down+flat+no_price = 过滤后总行数）与成交额合计（万元）；order_by +
limit 排序取前 N，行字段不裁剪；order_by 必须配 limit（排序后全表是大结果
通道，按参数错误拒绝）；均下推 SQL，不做通用 offset 分页。
"""

import logging
from datetime import date
from typing import Sequence

from .adapters import SnapshotAdapter, default_adapters
from .dates import normalize_date
from .db import connect
from .fetch_chain import (
    UPSTREAM_RETRY_ADVICE,
    AllSourcesFailed,
    aggregate_error_kind,
    fetch_snapshot_with_fallback,
)
from .output import error as _error
from .repository import (
    SNAPSHOT_ORDER_COLUMNS,
    select_latest_snapshot_date,
    select_snapshot,
    select_snapshot_aggregate,
    upsert_snapshot,
)

log = logging.getLogger(__name__)


def fetch_market_snapshot(
    trade_date: str | None = None,
    adapters: Sequence[SnapshotAdapter] | None = None,
) -> dict:
    """抓取全市场快照并落库：单次调用，按业务键 upsert（幂等）。"""
    tool = "fetch_market_snapshot"
    params = {"trade_date": trade_date}
    passed = None
    if trade_date is not None:
        try:
            passed = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    if adapters is None:
        adapters = default_adapters()
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        try:
            result = fetch_snapshot_with_fallback(adapters)
        except AllSourcesFailed as e:
            if aggregate_error_kind(e.attempted) == "upstream_unreachable":
                return _error(
                    tool,
                    params,
                    "上游不可达：全部数据源失败（连接级故障，请求未获得 HTTP 响应）",
                    error_kind="upstream_unreachable",
                    retry_advice=UPSTREAM_RETRY_ADVICE,
                    attempted_sources=e.attempted,
                )
            return _error(
                tool,
                params,
                "全部数据源失败（各源错误见 attempted_sources）",
                error_kind="fetch_failed",
                attempted_sources=e.attempted,
            )
        if result["trade_date"] is not None:
            effective, origin = result["trade_date"], "api"
        elif passed is not None:
            effective, origin = passed, "param"
        else:
            effective, origin = date.today().strftime("%Y%m%d"), "today"
        n = upsert_snapshot(conn, effective, result["rows"], result["source"])
    finally:
        conn.close()
    out = {
        "status": "ok",
        "tool": tool,
        "params": params,
        "trade_date": effective,
        "trade_date_origin": origin,
        "source": result["source"],
        "rows_upserted": n,
        "attempted_sources": result["attempted_sources"],
    }
    if not result["rows"]:
        out["empty_kind"] = "no_data_for_date"
        out["note"] = (
            "上游正常响应但该日期无快照数据（非交易日或盘后未发布），"
            "语义为正常而非失败"
        )
    return out


# 聚合模式（issue #48）可选聚合项；环比由调用方对当日/前一日各调一次完成
_SNAPSHOT_AGGREGATE_KINDS = ("breadth", "amount")


def query_snapshot(
    trade_date: str | None = None,
    stock_code: str | None = None,
    aggregate: str | None = None,
    order_by: str | None = None,
    order: str = "desc",
    limit: int | None = None,
) -> dict:
    """库内快照查询：按日期/代码过滤；日期缺省取库内最新交易日。

    aggregate（逗号分隔，可选 breadth,amount）为服务端聚合模式：返回涨跌
    家数（最新价 vs 昨收，含沪/深/北分市场口径；最新价或昨收为 NULL 的停牌
    行计入 no_price 桶，up+down+flat+no_price = 过滤后总行数）与两市成交额
    合计（万元，ADR 0006），不返回行；order_by/limit/order 在聚合模式下
    不生效。order_by（快照列）+ limit 为排序取前 N（order asc/desc，缺省
    desc），行字段与整表查询一致；order_by 必须配 limit（不带 limit 的排序
    会退化为排序后全表，按参数校验错误拒绝）。limit 单独使用时按 stock_code
    升序截断。不传新参数时行为与整表查询完全一致。
    """
    tool = "query_snapshot"
    params: dict = {"trade_date": trade_date, "stock_code": stock_code}
    kinds: tuple[str, ...] = ()
    if aggregate is not None:
        kinds = tuple(k.strip() for k in aggregate.split(","))
        unknown = [k for k in kinds if k not in _SNAPSHOT_AGGREGATE_KINDS]
        if not any(kinds) or unknown:
            return _error(
                tool,
                params,
                f"未知聚合项: {aggregate}（可选: {', '.join(_SNAPSHOT_AGGREGATE_KINDS)}）",
            )
        params["aggregate"] = aggregate
    if order_by is not None:
        params["order_by"] = order_by
        params["order"] = order
    if limit is not None:
        params["limit"] = limit
    if order_by is not None and order_by not in SNAPSHOT_ORDER_COLUMNS:
        return _error(
            tool,
            params,
            f"未知快照排序列: {order_by}（可选: {', '.join(SNAPSHOT_ORDER_COLUMNS)}）",
        )
    if aggregate is None and order_by is not None and limit is None:
        return _error(
            tool,
            params,
            f"order_by 需配合 limit 使用（排序取前 N；不带 limit 会退化为排序后全表）: {order_by}",
        )
    if order not in ("asc", "desc"):
        return _error(tool, params, f"order 只能是 asc/desc: {order!r}")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        return _error(tool, params, f"limit 必须为正整数: {limit!r}")
    if trade_date is not None:
        try:
            trade_date = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        if trade_date is None:
            trade_date = select_latest_snapshot_date(conn)
        if aggregate is not None:
            aggregates = select_snapshot_aggregate(conn, trade_date, stock_code)
            return {
                "status": "ok",
                "tool": tool,
                "params": params,
                "trade_date": trade_date,
                "aggregates": {k: aggregates[k] for k in kinds},
            }
        rows = select_snapshot(conn, trade_date, stock_code, order_by, order, limit)
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "trade_date": trade_date,
        "count": len(rows),
        "rows": rows,
    }
