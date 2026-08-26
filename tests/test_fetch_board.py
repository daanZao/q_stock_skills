"""fetch_board_snapshot 接缝测试：fake 适配器 + 真实 PG 测试库（issue #5）。

覆盖：五 section 全量落库与逐 section 报告、CSV 子集、未知 section 参数错误、
部分失败隔离（单 section 失败不拖垮其他、仅全部失败才整体 error）、
盘中 lhb 空数据语义（rows 0 + note，不算失败）、按业务键 upsert 幂等。
"""

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_board import fetch_board_snapshot

from fakes import BOARD_ROWS, INDEX_ROWS, LHB_ROWS, STRONG_ROWS, ZT_POOL_ROWS, FakeBoardAdapter


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


def test_fetch_all_sections_land_and_report(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare")
    result = fetch_board_snapshot(trade_date="20240105", adapters=[ak])
    assert result["status"] == "ok"
    assert result["tool"] == "fetch_board_snapshot"
    assert result["trade_date"] == "20240105"
    assert result["failed_sections"] == []
    expected_rows = {
        "indices": len(INDEX_ROWS),
        "boards": len(BOARD_ROWS),
        "zt_pool": len(ZT_POOL_ROWS),
        "strong_stocks": len(STRONG_ROWS),
        "lhb": sum(len(v) for v in LHB_ROWS.values()),
    }
    for section, n in expected_rows.items():
        assert result["sections"][section] == {
            "rows": n,
            "source": "akshare",
            "status": "ok",
        }
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)
    assert _count(pg_test, "market_boards") == len(BOARD_ROWS)
    assert _count(pg_test, "zt_pool") == len(ZT_POOL_ROWS)
    assert _count(pg_test, "strong_stocks") == len(STRONG_ROWS)
    assert _count(pg_test, "lhb_basic") == len(LHB_ROWS["lhb_basic"])
    assert _count(pg_test, "lhb_stock_statistic") == len(LHB_ROWS["lhb_stock_statistic"])
    assert _count(pg_test, "lhb_yyb_capital") == len(LHB_ROWS["lhb_yyb_capital"])
    assert _count(pg_test, "lhb_yyb_most") == len(LHB_ROWS["lhb_yyb_most"])


def test_fetch_sections_csv_subset(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare")
    result = fetch_board_snapshot(
        trade_date="20240105", sections="indices, zt_pool", adapters=[ak]
    )
    assert result["status"] == "ok"
    assert sorted(result["sections"]) == ["indices", "zt_pool"]
    assert ak.calls == ["indices", "zt_pool"]
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)
    assert _count(pg_test, "zt_pool") == len(ZT_POOL_ROWS)
    assert _count(pg_test, "lhb_basic") == 0  # 未请求的 section 不动


def test_fetch_unknown_section_is_param_error(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare")
    result = fetch_board_snapshot(sections="indices,foo", adapters=[ak])
    assert result["status"] == "error"
    assert "foo" in result["error"]
    assert "indices" in result["error"]  # 错误信息列出可选值
    assert ak.calls == []  # 参数错误时不触发任何抓取
    assert _count(pg_test, "market_indices") == 0


def test_fetch_rejects_bad_date_param(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare")
    result = fetch_board_snapshot(trade_date="2024-13-45", adapters=[ak])
    assert result["status"] == "error"
    assert ak.calls == []


def test_fetch_empty_sections_is_param_error(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare")
    result = fetch_board_snapshot(sections="", adapters=[ak])
    assert result["status"] == "error"
    assert "为空" in result["error"]  # 参数问题，不能报成"全部 section 抓取失败"
    assert "sections" not in result  # 参数错误不产出任何 section 结果
    assert ak.calls == []


def test_fetch_lhb_partial_failure_is_reported(pg_test):
    _init(pg_test)
    partial = {**LHB_ROWS, "errors": ["lhb_yyb_most: boom"]}
    ak = FakeBoardAdapter("akshare", rows={"lhb": partial})
    result = fetch_board_snapshot(
        trade_date="20240105", sections="lhb", adapters=[ak]
    )
    assert result["status"] == "ok"
    lhb = result["sections"]["lhb"]
    assert lhb["status"] == "ok"  # 子项部分失败不拖垮 section
    assert "lhb_yyb_most: boom" in lhb["partial_error"]  # 但失败原因必须报告
    assert _count(pg_test, "lhb_basic") == len(LHB_ROWS["lhb_basic"])


def test_fetch_section_failure_is_isolated(pg_test):
    _init(pg_test)
    ef = FakeBoardAdapter("efinance", fail_sections=("zt_pool", "lhb"))
    ak = FakeBoardAdapter("akshare", fail_sections=("zt_pool", "lhb"))
    result = fetch_board_snapshot(trade_date="20240105", adapters=[ef, ak])
    assert result["status"] == "ok"  # 单 section 失败不拖垮其他
    assert sorted(result["failed_sections"]) == ["lhb", "zt_pool"]
    assert result["sections"]["zt_pool"]["status"] == "error"
    assert "efinance" in result["sections"]["zt_pool"]["error"]
    assert "akshare" in result["sections"]["zt_pool"]["error"]
    assert result["sections"]["indices"] == {
        "rows": len(INDEX_ROWS),
        "source": "efinance",
        "status": "ok",
    }
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)
    assert _count(pg_test, "zt_pool") == 0
    assert _count(pg_test, "lhb_basic") == 0


def test_fetch_all_sections_failed_returns_error(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_times=99)
    result = fetch_board_snapshot(trade_date="20240105", adapters=[ak])
    assert result["status"] == "error"
    assert sorted(result["failed_sections"]) == sorted(
        ["indices", "boards", "zt_pool", "strong_stocks", "lhb"]
    )
    assert all(s["status"] == "error" for s in result["sections"].values())
    assert _count(pg_test, "market_indices") == 0


def test_fetch_section_falls_back_to_next_source(pg_test):
    _init(pg_test)
    ef = FakeBoardAdapter("efinance", fail_sections=("indices",))
    ak = FakeBoardAdapter("akshare")
    result = fetch_board_snapshot(
        trade_date="20240105", sections="indices", adapters=[ef, ak]
    )
    assert result["status"] == "ok"
    assert result["sections"]["indices"]["source"] == "akshare"
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)


def test_fetch_section_retries_up_to_three_attempts(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_times=2)  # 第 3 次调用才成功
    result = fetch_board_snapshot(
        trade_date="20240105", sections="indices", adapters=[ak]
    )
    assert result["status"] == "ok"
    assert ak.calls == ["indices"] * 3  # 最多重试 2 次（最多 3 次尝试）


def test_fetch_intraday_empty_lhb_is_ok_with_note(pg_test):
    _init(pg_test)
    empty_lhb = {t: [] for t in LHB_ROWS}
    ak = FakeBoardAdapter("akshare", rows={"lhb": empty_lhb})
    result = fetch_board_snapshot(trade_date="20240105", adapters=[ak])
    assert result["status"] == "ok"
    assert result["failed_sections"] == []  # lhb 空数据不算失败
    assert result["sections"]["lhb"] == {
        "rows": 0,
        "source": "akshare",
        "status": "ok",
        "note": "当日无龙虎榜数据（龙虎榜盘后发布）",
    }
    assert _count(pg_test, "lhb_basic") == 0


def test_fetch_repeated_is_idempotent_and_updates_values(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare")
    first = fetch_board_snapshot(trade_date="20240105", adapters=[ak])
    assert first["status"] == "ok"
    # 第二次执行：同一交易日，指数价格变化 → 更新原行，不产生重复行
    changed = [dict(r, latest_price=r["latest_price"] + 1.0) for r in INDEX_ROWS]
    ak2 = FakeBoardAdapter("akshare", rows={"indices": changed})
    second = fetch_board_snapshot(trade_date="20240105", adapters=[ak2])
    assert second["status"] == "ok"
    for table, n in [
        ("market_indices", len(INDEX_ROWS)),
        ("market_boards", len(BOARD_ROWS)),
        ("zt_pool", len(ZT_POOL_ROWS)),
        ("strong_stocks", len(STRONG_ROWS)),
        ("lhb_basic", len(LHB_ROWS["lhb_basic"])),
        ("lhb_stock_statistic", len(LHB_ROWS["lhb_stock_statistic"])),
        ("lhb_yyb_capital", len(LHB_ROWS["lhb_yyb_capital"])),
        ("lhb_yyb_most", len(LHB_ROWS["lhb_yyb_most"])),
    ]:
        assert _count(pg_test, table) == n
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT latest_price FROM market_indices "
            "WHERE trade_date = '20240105' AND index_code = '000001'"
        )
        assert cur.fetchone()[0] == 3001.0
    conn.close()
