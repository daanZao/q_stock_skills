"""fetch_board_snapshot indices 段 MAJOR_INDICES 白名单定点补漏测试（issue #49）。

主源 efinance「沪深系列指数」口径系统性排除科创板系列指数（缺 000688），备选
akshare「沪深重要指数」含 000688 但主源成功时轮不到。定点补漏：indices 段抓取
后对照 tools_init.MAJOR_INDICES 白名单（单一事实来源，不复制清单）检查覆盖，
缺谁按单 code 补抓补落库，保证 7 大指数每日齐整。

补漏失败语义（与 #47 fail-fast 兼容）：不静默（进 patch failed 明细 + 三态分类）
也不把整个 indices 段拖进 upstream_unreachable——补漏源与主源不同，section
维持 ok、不触发整体 error；连接级故障 error_kind=upstream_unreachable 带
retry_advice，全源口径不含该指数为 no_data_for_date，其余 fetch_failed。
"""

import psycopg2

from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp import tools_board, tools_init
from qstock_mcp.tools_board import fetch_board_snapshot

from fakes import INDEX_ROWS, FakeBoardAdapter

ROWS_MISSING_KC50 = [r for r in INDEX_ROWS if r["index_code"] != "000688"]


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _index_codes(pg_test, trade_date: str) -> set:
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT index_code FROM market_indices WHERE trade_date = %s",
            (trade_date,),
        )
        codes = {row[0] for row in cur.fetchall()}
    conn.close()
    return codes


def _conn_exc(source: str) -> FetchError:
    cause = ConnectionResetError("Connection aborted")
    err = FetchError(f"{source} 补抓失败: {cause}")
    err.__cause__ = cause
    return err


def test_patch_whitelist_is_single_source_of_truth():
    """白名单复用 tools_init.MAJOR_INDICES，不复制清单。"""
    assert tools_board.MAJOR_INDICES is tools_init.MAJOR_INDICES
    assert ("000688", "科创50") in tools_board.MAJOR_INDICES


def test_missing_major_index_is_patched_and_landed(pg_test):
    """主源口径缺 000688 → 触发单 code 补抓，efinance 口径也不含 → 转到 akshare，
    000688 落库；重跑幂等。"""
    _init(pg_test)
    ef = FakeBoardAdapter("efinance", rows={"indices": ROWS_MISSING_KC50})
    ak = FakeBoardAdapter(
        "akshare", rows={"indices": ROWS_MISSING_KC50}, spot_rows=INDEX_ROWS
    )
    result = fetch_board_snapshot(
        trade_date="20240105", sections="indices", adapters=[ef, ak]
    )
    assert result["status"] == "ok"
    sec = result["sections"]["indices"]
    assert sec["status"] == "ok"
    assert sec["source"] == "efinance"  # 主源结构不变
    assert sec["rows"] == len(INDEX_ROWS)  # 主源 6 行 + 补抓 1 行
    assert sec["patch"]["patched"] == [{"index_code": "000688", "source": "akshare"}]
    assert "failed" not in sec["patch"]
    assert ef.spot_calls == ["000688"]
    assert ak.spot_calls == ["000688"]
    assert _index_codes(pg_test, "20240105") == {r["index_code"] for r in INDEX_ROWS}
    again = fetch_board_snapshot(
        trade_date="20240105",
        sections="indices",
        adapters=[
            FakeBoardAdapter("efinance", rows={"indices": ROWS_MISSING_KC50}),
            FakeBoardAdapter(
                "akshare", rows={"indices": ROWS_MISSING_KC50}, spot_rows=INDEX_ROWS
            ),
        ],
    )
    assert again["sections"]["indices"]["rows"] == len(INDEX_ROWS)
    assert len(_index_codes(pg_test, "20240105")) == len(INDEX_ROWS)


def test_full_whitelist_coverage_triggers_zero_patch(pg_test):
    """白名单全命中 → 零补抓请求，section 结果无 patch 键。"""
    _init(pg_test)
    ak = FakeBoardAdapter("akshare")
    result = fetch_board_snapshot(
        trade_date="20240105", sections="indices", adapters=[ak]
    )
    assert result["sections"]["indices"] == {
        "rows": len(INDEX_ROWS),
        "source": "akshare",
        "status": "ok",
    }
    assert ak.spot_calls == []


def test_patch_connection_failure_keeps_section_ok_no_failfast(pg_test):
    """补漏全源连接级故障：不静默（patch failed 明细 + upstream_unreachable +
    retry_advice + 逐源 failure_kind），但不触发 #47 fail-fast——section 与整体
    均维持 ok，其他 section 正常落库。"""
    _init(pg_test)
    ef = FakeBoardAdapter(
        "efinance", rows={"indices": ROWS_MISSING_KC50}, spot_exc=_conn_exc("efinance")
    )
    ak = FakeBoardAdapter(
        "akshare", rows={"indices": ROWS_MISSING_KC50}, spot_exc=_conn_exc("akshare")
    )
    result = fetch_board_snapshot(trade_date="20240105", adapters=[ef, ak])
    assert result["status"] == "ok"
    assert result["failed_sections"] == []
    sec = result["sections"]["indices"]
    assert sec["status"] == "ok"
    assert sec["rows"] == len(ROWS_MISSING_KC50)
    assert sec["patch"]["patched"] == []
    failed = sec["patch"]["failed"]["000688"]
    assert failed["error_kind"] == "upstream_unreachable"
    assert failed["retry_advice"]
    attempted = {a["source"]: a for a in failed["attempted_sources"]}
    assert attempted["efinance"]["failure_kind"] == "connection"
    assert attempted["akshare"]["failure_kind"] == "connection"
    assert "Connection aborted" in attempted["efinance"]["error"]
    assert result["sections"]["boards"]["status"] == "ok"
    assert "000688" not in _index_codes(pg_test, "20240105")


def test_patch_all_sources_empty_is_no_data_for_date(pg_test):
    """补漏全源口径不含该指数（空结果）→ no_data_for_date（正常语义非失败），
    不带 retry_advice；section 维持 ok。"""
    _init(pg_test)
    ef = FakeBoardAdapter("efinance", rows={"indices": ROWS_MISSING_KC50})
    ak = FakeBoardAdapter("akshare", rows={"indices": ROWS_MISSING_KC50})
    result = fetch_board_snapshot(
        trade_date="20240105", sections="indices", adapters=[ef, ak]
    )
    assert result["status"] == "ok"
    sec = result["sections"]["indices"]
    assert sec["status"] == "ok"
    failed = sec["patch"]["failed"]["000688"]
    assert failed["error_kind"] == "no_data_for_date"
    assert "retry_advice" not in failed
    assert all(a["failure_kind"] == "empty" for a in failed["attempted_sources"])
    assert "000688" not in _index_codes(pg_test, "20240105")


def test_patch_non_connection_failure_is_fetch_failed(pg_test):
    """补漏非连接级失败（收到响应后的错误）→ fetch_failed，不带 retry_advice。"""
    _init(pg_test)
    ak = FakeBoardAdapter(
        "akshare",
        rows={"indices": ROWS_MISSING_KC50},
        spot_exc=FetchError("akshare 补抓返回异常"),
    )
    result = fetch_board_snapshot(
        trade_date="20240105", sections="indices", adapters=[ak]
    )
    assert result["status"] == "ok"
    failed = result["sections"]["indices"]["patch"]["failed"]["000688"]
    assert failed["error_kind"] == "fetch_failed"
    assert "retry_advice" not in failed
    assert failed["attempted_sources"][0]["failure_kind"] == "other"


def test_patch_not_attempted_when_section_fetch_fails(pg_test):
    """indices 段主抓取失败时不做补漏（无覆盖基线），section 走原有 error 路径。"""
    _init(pg_test)
    ak = FakeBoardAdapter(
        "akshare", fail_sections=("indices",), spot_rows=INDEX_ROWS
    )
    result = fetch_board_snapshot(
        trade_date="20240105", sections="indices", adapters=[ak]
    )
    assert result["sections"]["indices"]["status"] == "error"
    assert "patch" not in result["sections"]["indices"]
    assert ak.spot_calls == []
