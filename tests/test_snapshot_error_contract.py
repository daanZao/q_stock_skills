"""fetch_market_snapshot 三态错误契约测试（issue #46，ADR 0007）。

三态：upstream_unreachable（连接级故障：请求未获得任何 HTTP 响应）/
no_data_for_date（上游正常响应但该日期无数据，非失败）/empty_ok（查询条件
合理的空集，本工具无此路径）。全源失败时错误体携带 error_kind、逐源
failure_kind 明细（适配器名 + 原始报错）与重试建议；by-design 不支持的源
（如 baostock 无全市场快照）不算 upstream_unreachable。

fake 适配器模拟：抛连接级异常 / 返回空结果 / 返回正常数据 / 多源混合失败。
只测工具返回结构与错误分类，不测适配器内部重试。
"""

import psycopg2

from qstock_mcp.adapters.baostock_adapter import BaostockAdapter
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_snapshot import fetch_market_snapshot

from fakes import FakeSnapshotAdapter, SNAPSHOT_ROWS


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _conn_exc(source: str) -> FetchError:
    """连接级故障：FetchError 的 __cause__ 为连接重置类异常（模拟 push2 空响应）。"""
    cause = ConnectionResetError("Connection aborted")
    err = FetchError(f"{source} 快照抓取失败: {cause}")
    err.__cause__ = cause
    return err


def _unsupported_exc(source: str) -> FetchError:
    return FetchError(f"{source} 不支持全市场快照", kind="unsupported")


def test_all_connection_failures_classified_upstream_unreachable(pg_test):
    _init(pg_test)
    adapters = [
        FakeSnapshotAdapter("efinance", fail_times=99, exc=_conn_exc("efinance")),
        FakeSnapshotAdapter("akshare", fail_times=99, exc=_conn_exc("akshare")),
        FakeSnapshotAdapter("baostock", fail_times=99, exc=_unsupported_exc("baostock")),
    ]
    result = fetch_market_snapshot(adapters=adapters)
    assert result["status"] == "error"
    assert result["error_kind"] == "upstream_unreachable"
    assert result["retry_advice"]  # 重试建议：稍后重试
    attempted = {a["source"]: a for a in result["attempted_sources"]}
    assert attempted["efinance"]["failure_kind"] == "connection"
    assert attempted["akshare"]["failure_kind"] == "connection"
    # by-design 不支持不算连接级故障，但仍携带原始报错
    assert attempted["baostock"]["failure_kind"] == "unsupported"
    assert "不支持全市场快照" in attempted["baostock"]["error"]
    assert "Connection aborted" in attempted["efinance"]["error"]
    assert "rows_upserted" not in result


def test_any_connection_failure_suffices_for_unreachable(pg_test):
    """多源混合失败：一条源连接级故障 + 一条源其他故障 → upstream_unreachable。"""
    _init(pg_test)
    adapters = [
        FakeSnapshotAdapter("efinance", fail_times=99, exc=_conn_exc("efinance")),
        FakeSnapshotAdapter("akshare", fail_times=99),  # 普通 FetchError("boom")
    ]
    result = fetch_market_snapshot(adapters=adapters)
    assert result["status"] == "error"
    assert result["error_kind"] == "upstream_unreachable"
    kinds = {a["source"]: a["failure_kind"] for a in result["attempted_sources"]}
    assert kinds == {"efinance": "connection", "akshare": "other"}


def test_non_connection_failures_classified_fetch_failed(pg_test):
    """收到响应后的失败（非连接级）不属 upstream_unreachable，无重试建议。"""
    _init(pg_test)
    adapters = [FakeSnapshotAdapter(n, fail_times=99) for n in ("efinance", "akshare")]
    result = fetch_market_snapshot(adapters=adapters)
    assert result["status"] == "error"
    assert result["error_kind"] == "fetch_failed"
    assert "retry_advice" not in result
    assert all(a["failure_kind"] == "other" for a in result["attempted_sources"])


def test_only_unsupported_sources_is_not_unreachable(pg_test):
    """by-design 不支持的源独占失败清单时不算上游不可达。"""
    _init(pg_test)
    adapters = [
        FakeSnapshotAdapter("baostock", fail_times=99, exc=_unsupported_exc("baostock"))
    ]
    result = fetch_market_snapshot(adapters=adapters)
    assert result["status"] == "error"
    assert result["error_kind"] != "upstream_unreachable"


def test_baostock_snapshot_rejection_classified_unsupported(pg_test):
    """真实 baostock 适配器的"不支持全市场快照"分类为 unsupported。"""
    _init(pg_test)
    result = fetch_market_snapshot(adapters=[BaostockAdapter()])
    assert result["status"] == "error"
    assert result["error_kind"] != "upstream_unreachable"
    (attempted,) = result["attempted_sources"]
    assert attempted["source"] == "baostock"
    assert attempted["failure_kind"] == "unsupported"


def test_empty_result_is_no_data_for_date_not_error(pg_test):
    """上游正常响应空结果 → 该交易日无数据（非交易日/盘后未发布），非失败。"""
    _init(pg_test)
    ef = FakeSnapshotAdapter("efinance", rows=[], trade_date="20240105")
    result = fetch_market_snapshot(trade_date="20240105", adapters=[ef])
    assert result["status"] == "ok"
    assert result["rows_upserted"] == 0
    assert result["empty_kind"] == "no_data_for_date"
    assert result["note"]
    assert "error" not in result


def test_empty_result_does_not_fall_back(pg_test):
    """空结果是成功语义：不触发 fallback，后续源不被调用。"""
    _init(pg_test)
    ef = FakeSnapshotAdapter("efinance", rows=[], trade_date="20240105")
    ak = FakeSnapshotAdapter("akshare", trade_date="20240105")
    result = fetch_market_snapshot(adapters=[ef, ak])
    assert result["status"] == "ok"
    assert result["empty_kind"] == "no_data_for_date"
    assert ak.calls == 0


def test_normal_result_has_no_empty_kind(pg_test):
    _init(pg_test)
    ef = FakeSnapshotAdapter("efinance", trade_date="20240105")
    result = fetch_market_snapshot(adapters=[ef])
    assert result["status"] == "ok"
    assert result["rows_upserted"] == len(SNAPSHOT_ROWS)
    assert "empty_kind" not in result


def test_failed_source_detail_visible_on_success(pg_test):
    """fallback 成功时，失败源的连接级分类仍机器可读。"""
    _init(pg_test)
    ef = FakeSnapshotAdapter("efinance", fail_times=99, exc=_conn_exc("efinance"))
    ak = FakeSnapshotAdapter("akshare", trade_date="20240105")
    result = fetch_market_snapshot(adapters=[ef, ak])
    assert result["status"] == "ok"
    assert result["source"] == "akshare"
    (attempted,) = result["attempted_sources"]
    assert attempted["source"] == "efinance"
    assert attempted["failure_kind"] == "connection"
