"""fetch_board_snapshot 接缝测试：fake 适配器 + 真实 PG 测试库（issue #5）。

覆盖：五 section 全量落库与逐 section 报告、CSV 子集、未知 section 参数错误、
部分失败隔离（单 section 失败不拖垮其他、仅全部失败才整体 error）、
盘中 lhb 空数据语义（rows 0 + note，不算失败）、按业务键 upsert 幂等。

日期口径（issue #78 服务端防线）：boards/indices 为实时语义 section，历史日期
拒收——本文件验证抓取/落库语义，一律用当日日期（fake 适配器不感知日期）。
历史日期拒绝与盘中禁写在 test_realtime_section_guard.py。
"""

from datetime import date

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_board import fetch_board_snapshot

from fakes import BOARD_ROWS, INDEX_ROWS, LHB_ROWS, STRONG_ROWS, ZT_POOL_ROWS, FakeBoardAdapter

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


def test_fetch_all_sections_land_and_report(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare")
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    assert result["status"] == "ok"
    assert result["tool"] == "fetch_board_snapshot"
    assert result["trade_date"] == TODAY
    assert result["failed_sections"] == []
    expected_rows = {
        "indices": len(INDEX_ROWS),
        "boards": len(BOARD_ROWS),
        "zt_pool": len(ZT_POOL_ROWS),
        "strong_stocks": len(STRONG_ROWS),
        # lhb rows 保持 basic/yyb/statistic 口径，详情由 detail_rows 单列
        "lhb": sum(
            len(v) for k, v in LHB_ROWS.items() if k != "lhb_stock_detail"
        ) + 1,
    }
    for section, n in expected_rows.items():
        expected = {
            "rows": n,
            "source": "akshare",
            "status": "ok",
        }
        if section == "boards":
            # 字段级第四态声明（issue #118）：boards 段响应额外携带
            # planned_absent_fields（申万不发布的三字段，见 tools_board）
            assert result["sections"][section].pop("planned_absent_fields")
        if section == "lhb":
            expected.update({
                "detail_rows": len(LHB_ROWS["lhb_stock_detail"]),
                "detail_status": "ok", "detail_missing": 0,
                "detail_source": "akshare",
            })
        assert result["sections"][section] == expected
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)
    assert _count(pg_test, "market_boards") == len(BOARD_ROWS)
    assert _count(pg_test, "zt_pool") == len(ZT_POOL_ROWS)
    assert _count(pg_test, "strong_stocks") == len(STRONG_ROWS)
    assert _count(pg_test, "lhb_basic") == len(LHB_ROWS["lhb_basic"])
    # statistic 库内自算（issue #114 定稿）：basic 1 股入窗 → 1 快照行
    assert _count(pg_test, "lhb_stock_statistic") == 1
    assert _count(pg_test, "lhb_yyb_capital") == len(LHB_ROWS["lhb_yyb_capital"])
    assert _count(pg_test, "lhb_yyb_most") == len(LHB_ROWS["lhb_yyb_most"])


def test_fetch_sections_csv_subset(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare")
    result = fetch_board_snapshot(
        trade_date=TODAY, sections="indices, zt_pool", adapters=[ak]
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
        trade_date=TODAY, sections="lhb", adapters=[ak]
    )
    assert result["status"] == "ok"
    lhb = result["sections"]["lhb"]
    assert lhb["status"] == "ok"  # 子项部分失败不拖垮 section
    assert "lhb_yyb_most: boom" in lhb["partial_error"]  # 但失败原因必须报告
    assert _count(pg_test, "lhb_basic") == len(LHB_ROWS["lhb_basic"])


def test_fetch_section_failure_is_isolated(pg_test):
    _init(pg_test)
    ef = FakeBoardAdapter("tencent", fail_sections=("zt_pool", "lhb"))
    ak = FakeBoardAdapter("akshare", fail_sections=("zt_pool", "lhb"))
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ef, ak])
    assert result["status"] == "ok"  # 单 section 失败不拖垮其他
    assert sorted(result["failed_sections"]) == ["lhb", "zt_pool"]
    assert result["sections"]["zt_pool"]["status"] == "error"
    assert "tencent" in result["sections"]["zt_pool"]["error"]
    assert "akshare" in result["sections"]["zt_pool"]["error"]
    assert result["sections"]["indices"] == {
        "rows": len(INDEX_ROWS),
        "source": "tencent",
        "status": "ok",
    }
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)
    assert _count(pg_test, "zt_pool") == 0
    assert _count(pg_test, "lhb_basic") == 0


def test_fetch_all_sections_failed_returns_error(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_times=99)
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    assert result["status"] == "error"
    assert sorted(result["failed_sections"]) == sorted(
        ["indices", "boards", "zt_pool", "strong_stocks", "lhb"]
    )
    assert all(s["status"] == "error" for s in result["sections"].values())
    assert _count(pg_test, "market_indices") == 0


def test_fetch_section_falls_back_to_next_source(pg_test):
    _init(pg_test)
    ef = FakeBoardAdapter("tencent", fail_sections=("indices",))
    ak = FakeBoardAdapter("akshare")
    result = fetch_board_snapshot(
        trade_date=TODAY, sections="indices", adapters=[ef, ak]
    )
    assert result["status"] == "ok"
    assert result["sections"]["indices"]["source"] == "akshare"
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)


def test_fetch_section_retries_up_to_three_attempts(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare", fail_times=2)  # 第 3 次调用才成功
    result = fetch_board_snapshot(
        trade_date=TODAY, sections="indices", adapters=[ak]
    )
    assert result["status"] == "ok"
    assert ak.calls == ["indices"] * 3  # 最多重试 2 次（最多 3 次尝试）


def test_fetch_intraday_empty_lhb_is_ok_with_note(pg_test):
    _init(pg_test)
    empty_lhb = {t: [] for t in LHB_ROWS}
    ak = FakeBoardAdapter("akshare", rows={"lhb": empty_lhb})
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    assert result["status"] == "ok"
    assert result["failed_sections"] == []  # lhb 空数据不算失败
    assert result["sections"]["lhb"] == {
        "rows": 0,
        "source": "akshare",
        "status": "ok",
        "empty_kind": "no_data_for_date",
        "note": "当日无龙虎榜数据（龙虎榜盘后发布）",
        "detail_rows": 0,
        "detail_status": "ok",
        "detail_missing": 0,
        "detail_source": "akshare",
    }
    assert _count(pg_test, "lhb_basic") == 0


def test_fetch_repeated_is_idempotent_and_updates_values(pg_test):
    _init(pg_test)
    ak = FakeBoardAdapter("akshare")
    first = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])
    assert first["status"] == "ok"
    # 第二次执行：同一交易日，指数价格变化 → 更新原行，不产生重复行
    changed = [dict(r, latest_price=r["latest_price"] + 1.0) for r in INDEX_ROWS]
    ak2 = FakeBoardAdapter("akshare", rows={"indices": changed})
    second = fetch_board_snapshot(trade_date=TODAY, adapters=[ak2])
    assert second["status"] == "ok"
    for table, n in [
        ("market_indices", len(INDEX_ROWS)),
        ("market_boards", len(BOARD_ROWS)),
        ("zt_pool", len(ZT_POOL_ROWS)),
        ("strong_stocks", len(STRONG_ROWS)),
        ("lhb_basic", len(LHB_ROWS["lhb_basic"])),
        # statistic 库内自算：basic 1 股入窗 → 1 快照行（重跑 DELETE+INSERT 仍 1）
        ("lhb_stock_statistic", 1),
        ("lhb_yyb_capital", len(LHB_ROWS["lhb_yyb_capital"])),
        ("lhb_yyb_most", len(LHB_ROWS["lhb_yyb_most"])),
    ]:
        assert _count(pg_test, table) == n
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT latest_price FROM market_indices "
            "WHERE trade_date = %s AND index_code = '000001'",
            (TODAY,),
        )
        assert cur.fetchone()[0] == 3001.0
    conn.close()


def test_lhb_basic_multi_reason_same_stock_lands(pg_test):
    """同股当日多榜单：上游按 (股票, 上榜原因) 返回多行，业务键含 lhb_reason，
    两行都落库且重跑幂等（回归：键仅 (trade_date, stock_code) 时单条多行
    INSERT ... ON CONFLICT 撞键，CardinalityViolation 整批回滚）。"""
    _init(pg_test)
    base = LHB_ROWS["lhb_basic"][0]
    lhb = {
        **LHB_ROWS,
        "lhb_basic": [
            base,
            dict(base, lhb_reason="换手率达20%的证券", net_buy_amount=5.0e7),
        ],
    }
    ak = FakeBoardAdapter("akshare", rows={"lhb": lhb})
    result = fetch_board_snapshot(
        trade_date=TODAY, sections="lhb", adapters=[ak]
    )
    assert result["status"] == "ok"
    assert result["sections"]["lhb"]["status"] == "ok"
    assert _count(pg_test, "lhb_basic") == 2
    # 重跑幂等：同业务键 upsert，不产生重复行
    again = fetch_board_snapshot(
        trade_date=TODAY, sections="lhb",
        adapters=[FakeBoardAdapter("akshare", rows={"lhb": lhb})],
    )
    assert again["sections"]["lhb"]["status"] == "ok"
    assert _count(pg_test, "lhb_basic") == 2
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT net_buy_amount FROM lhb_basic "
            "WHERE trade_date = %s AND stock_code = '600519' "
            "AND lhb_reason = '换手率达20%%的证券'",  # %% 为 psycopg2 参数化转义
            (TODAY,),
        )
        assert cur.fetchone()[0] == 5.0e7
    conn.close()


def test_boards_upstream_dup_key_last_wins(pg_test, caplog):
    """上游板块同代码重名（两行价格微差，东财行业接口实测）：upsert 按业务键
    last-wins 去重落一行并记 warning，不再 CardinalityViolation 整批回滚。"""
    _init(pg_test)
    dup = BOARD_ROWS + [
        dict(BOARD_ROWS[0], latest_price=BOARD_ROWS[0]["latest_price"] + 0.01)
    ]
    ak = FakeBoardAdapter("akshare", rows={"boards": dup})
    with caplog.at_level("WARNING"):
        result = fetch_board_snapshot(
            trade_date=TODAY, sections="boards", adapters=[ak]
        )
    assert result["sections"]["boards"]["status"] == "ok"
    assert result["sections"]["boards"]["rows"] == len(BOARD_ROWS)
    assert _count(pg_test, "market_boards") == len(BOARD_ROWS)
    assert "业务键重复行" in caplog.text
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT latest_price FROM market_boards "
            "WHERE trade_date = %s AND board_type = 'industry' "
            "AND board_name = '酿酒行业'",
            (TODAY,),
        )
        assert cur.fetchone()[0] == 2000.01  # last-wins
    conn.close()
