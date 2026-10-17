"""fetch_board_snapshot 核心 section fail-fast 与 lhb 三态测试（issue #47，ADR 0007）。

核心 section 集显式声明为 indices/boards/zt_pool/strong_stocks（lhb 永不进核心集）。
任一核心 section 因 upstream_unreachable（连接级故障）失败 → 工具整体 error，
响应携带 per-section 明细（成败/已落库行数/三态分类 + 逐源原始报错）；
fetch_failed（非连接级）与 no_data_for_date（上游正常响应空结果）不触发；
lhb 失败按三态结构化返回（与"盘后未发布"可区分）但不触发整体 error。
中途失败不回滚：per-section 独立 commit，已落库 section 保留，
调用方可用 sections 参数只重试失败分段（幂等）。

日期口径（issue #78 服务端防线）：boards/indices 为实时语义 section，历史日期
拒收——本文件一律用当日日期。
"""

from datetime import date

import psycopg2

from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_board import CORE_SECTIONS, fetch_board_snapshot

from fakes import BOARD_ROWS, INDEX_ROWS, LHB_ROWS, FakeBoardAdapter

TODAY = date.today().strftime("%Y%m%d")


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _count(pg_test, table: str) -> int:
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        n = cur.fetchone()[0]
    conn.close()
    return n


def _conn_exc(source: str) -> FetchError:
    """连接级故障：FetchError 的 __cause__ 为连接重置类异常（模拟 push2 空响应）。"""
    cause = ConnectionResetError("Connection aborted")
    err = FetchError(f"{source} 抓取失败: {cause}")
    err.__cause__ = cause
    return err


def _unsupported_exc(source: str) -> FetchError:
    return FetchError(f"{source} 不支持该 section", kind="unsupported")


def test_core_section_set_is_explicit_and_excludes_lhb(pg_test):
    assert CORE_SECTIONS == ("indices", "boards", "zt_pool", "strong_stocks")
    assert "lhb" not in CORE_SECTIONS


def test_core_section_unreachable_fails_overall_with_per_section_detail(pg_test):
    """核心 section（boards）连接级故障 → 整体 error + 完整 per-section 明细。"""
    _init(pg_test)
    ef = FakeBoardAdapter("efinance", fail_sections=("boards",), exc=_conn_exc("efinance"))
    ak = FakeBoardAdapter("akshare", fail_sections=("boards",), exc=_conn_exc("akshare"))
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ef, ak])
    assert result["status"] == "error"
    assert result["error_kind"] == "upstream_unreachable"
    assert result["retry_advice"]
    assert "boards" in result["error"]
    assert result["failed_sections"] == ["boards"]
    boards = result["sections"]["boards"]
    assert boards["status"] == "error"
    assert boards["rows"] == 0
    assert boards["error_kind"] == "upstream_unreachable"
    assert boards["retry_advice"]
    attempted = {a["source"]: a for a in boards["attempted_sources"]}
    assert attempted["efinance"]["failure_kind"] == "connection"
    assert attempted["akshare"]["failure_kind"] == "connection"
    assert "Connection aborted" in attempted["efinance"]["error"]
    # 已成功 section 的明细仍在：状态表而非一句"失败了"
    assert result["sections"]["indices"] == {
        "rows": len(INDEX_ROWS),
        "source": "efinance",
        "status": "ok",
    }
    assert result["sections"]["lhb"]["status"] == "ok"


def test_failed_core_section_does_not_roll_back_succeeded(pg_test):
    """中途失败不回滚：boards 失败时 indices/zt_pool/strong_stocks/lhb 已落库保留。"""
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_sections=("boards",), exc=_conn_exc("akshare"))
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    assert result["status"] == "error"
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)
    assert _count(pg_test, "zt_pool") > 0
    assert _count(pg_test, "strong_stocks") > 0
    assert _count(pg_test, "lhb_basic") == len(LHB_ROWS["lhb_basic"])
    assert _count(pg_test, "market_boards") == 0


def test_segmented_retry_after_failfast_is_idempotent(pg_test):
    """分段重试：只重抓失败的 boards 段；成功段不重抓，重跑全量幂等。"""
    _init(pg_test)
    bad = FakeBoardAdapter("akshare", fail_sections=("boards",), exc=_conn_exc("akshare"))
    first = fetch_board_snapshot(trade_date=TODAY, adapters=[bad])
    assert first["status"] == "error"
    ok = FakeBoardAdapter("akshare")
    retry = fetch_board_snapshot(
        trade_date=TODAY, sections="boards", adapters=[ok]
    )
    assert retry["status"] == "ok"
    assert retry["sections"]["boards"]["rows"] == len(BOARD_ROWS)
    assert _count(pg_test, "market_boards") == len(BOARD_ROWS)
    again = fetch_board_snapshot(trade_date=TODAY, adapters=[FakeBoardAdapter("akshare")])
    assert again["status"] == "ok"
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)
    assert _count(pg_test, "market_boards") == len(BOARD_ROWS)


def test_core_section_non_connection_failure_keeps_overall_ok(pg_test):
    """核心 section 非连接级失败（收到响应后的错误）不属 upstream_unreachable，
    保持现有语义：单 section 失败不拖垮整体。"""
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_sections=("boards",))  # 普通 FetchError
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    assert result["status"] == "ok"
    boards = result["sections"]["boards"]
    assert boards["status"] == "error"
    assert boards["error_kind"] == "fetch_failed"
    assert "retry_advice" not in boards
    assert boards["attempted_sources"][0]["failure_kind"] == "other"


def test_unsupported_sources_not_counted_as_unreachable(pg_test):
    """by-design 不支持的源不算抓取故障：与连接级故障混合时仍判 unreachable，
    独占失败清单时判 fetch_failed（不触发整体 error）。"""
    _init(pg_test)
    ef = FakeBoardAdapter(
        "efinance", fail_sections=("boards",), exc=_unsupported_exc("efinance")
    )
    ak = FakeBoardAdapter("akshare", fail_sections=("boards",), exc=_conn_exc("akshare"))
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ef, ak])
    assert result["status"] == "error"
    attempted = {a["source"]: a for a in result["sections"]["boards"]["attempted_sources"]}
    assert attempted["efinance"]["failure_kind"] == "unsupported"
    assert "不支持" in attempted["efinance"]["error"]

    _init(pg_test)
    only_unsupported = FakeBoardAdapter(
        "efinance", fail_sections=("boards",), exc=_unsupported_exc("efinance")
    )
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[only_unsupported])
    assert result["status"] == "ok"
    assert result["sections"]["boards"]["error_kind"] == "fetch_failed"


def test_empty_core_section_is_no_data_not_failfast(pg_test):
    """核心 section 上游正常响应空结果 → no_data_for_date（status ok），不触发整体 error。"""
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", rows={"zt_pool": []})
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    assert result["status"] == "ok"
    zt = result["sections"]["zt_pool"]
    assert zt["rows"] == 0
    assert zt["status"] == "ok"
    assert zt["empty_kind"] == "no_data_for_date"
    assert result["failed_sections"] == []


def test_lhb_connection_failure_structured_but_not_overall_error(pg_test):
    """lhb 永不进核心集：连接级故障按三态结构化返回（与"未发布"可区分），
    但不触发整体 error。"""
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_sections=("lhb",), exc=_conn_exc("akshare"))
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    assert result["status"] == "ok"
    lhb = result["sections"]["lhb"]
    assert lhb["status"] == "error"
    assert lhb["error_kind"] == "upstream_unreachable"
    assert lhb["retry_advice"]
    assert lhb["attempted_sources"][0]["failure_kind"] == "connection"
    assert "note" not in lhb  # 与"盘后未发布"（status ok + note）语义分离
    assert result["failed_sections"] == ["lhb"]
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)


def test_lhb_only_selected_and_fails_is_overall_error(pg_test):
    """仅请求 lhb 且失败 = 请求的 section 全部失败，保持现有整体 error 语义。"""
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_sections=("lhb",), exc=_conn_exc("akshare"))
    result = fetch_board_snapshot(
        trade_date=TODAY, sections="lhb", adapters=[ak]
    )
    assert result["status"] == "error"
    assert result["error_kind"] == "upstream_unreachable"
    assert result["sections"]["lhb"]["status"] == "error"


def test_all_sections_unreachable_reports_error_kind(pg_test):
    """全 section 连接级失败：整体 error + error_kind + 逐 section 明细。"""
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_times=99, exc=_conn_exc("akshare"))
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    assert result["status"] == "error"
    assert result["error_kind"] == "upstream_unreachable"
    assert result["retry_advice"]
    assert len(result["failed_sections"]) == 5
    assert all(
        s["error_kind"] == "upstream_unreachable"
        for s in result["sections"].values()
    )
    assert _count(pg_test, "market_indices") == 0
