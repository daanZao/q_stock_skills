"""第四态 planned_absent 编排契约测试（issue #113，spec #111/#102）。

采集结果三态（ok/empty/error）之外的编排第四态：已定方向待接线的 section 在
tools_board.PLANNED_ABSENT_SECTIONS 登记后占位——不抓取、零落库、不计
failed_sections、不触发核心 section fail-fast、不进异常报告；收盘后批
（qstock-scheduler close）经同一契约正常出批。核心 section 真故障（error）
仍触发整体中止（回归不变）。测试注入 fake 适配器 + monkeypatch 登记处，
只测外部行为。

日期口径（issue #78 服务端防线）：boards/indices 为实时语义 section，本文件
一律用当日日期。
"""

import json
from datetime import date

import psycopg2
import pytest

from qstock_mcp import scheduler, tools_board
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema
from qstock_mcp.ledger import record_collection
from qstock_mcp.tools_board import fetch_board_snapshot
from qstock_mcp.tools_ledger import query_collection_log

from fakes import FakeBoardAdapter, INDEX_ROWS

TODAY = date.today().strftime("%Y%m%d")

REASON_ZT = "已定 THS 涨停揭秘承载（spec #111），接线中"


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


def _registry(monkeypatch, mapping: dict[str, str]):
    monkeypatch.setattr(tools_board, "PLANNED_ABSENT_SECTIONS", mapping)


def _conn_exc(source: str) -> FetchError:
    """连接级故障（模拟新源连接被重置）。"""
    cause = ConnectionResetError("Connection aborted")
    err = FetchError(f"{source} 抓取失败: {cause}")
    err.__cause__ = cause
    return err


# ---------------------------------------------------------------- 第四态占位行为


def test_planned_absent_section_not_fetched_not_failed(pg_test, monkeypatch):
    """在册 section 占位通过：不抓取、零落库、不计失败，其余 section 照常。"""
    _init(pg_test)
    _registry(monkeypatch, {"zt_pool": REASON_ZT})
    fake = FakeBoardAdapter("fake")
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[fake])

    assert result["status"] == "ok"
    assert result["failed_sections"] == []
    zt = result["sections"]["zt_pool"]
    assert zt["status"] == "planned_absent"
    assert zt["rows"] == 0
    assert REASON_ZT in zt["note"]
    assert "error_kind" not in zt  # 非故障，无三态 error_kind
    assert "attempted_sources" not in zt  # 未发生抓取
    assert "zt_pool" not in fake.calls  # 适配器零调用
    assert _count(pg_test, "zt_pool") == 0  # 零落库
    # 其余 section 不受影响，正常落库
    assert result["sections"]["indices"] == {
        "rows": len(INDEX_ROWS),
        "source": "fake",
        "status": "ok",
    }
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)


def test_planned_absent_ledgers_placeholder_row(pg_test, monkeypatch):
    """占位落账：planned_absent 台账行（rows=0、source 空、detail 记原因），
    历史模式就绪归因由此区分"计划缺席"与"未采集"；台账查询可按第四态过滤。"""
    _init(pg_test)
    _registry(monkeypatch, {"zt_pool": REASON_ZT})
    result = fetch_board_snapshot(
        trade_date=TODAY, adapters=[FakeBoardAdapter("fake")]
    )
    assert result["status"] == "ok"

    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, source, rows, error_kind, detail FROM collection_log"
            " WHERE trade_date = %s AND section = 'zt_pool'",
            (TODAY,),
        )
        rows = cur.fetchall()
    conn.close()
    assert len(rows) == 1
    status, source, n, error_kind, detail = rows[0]
    assert status == "planned_absent"
    assert source is None
    assert n == 0
    assert error_kind is None  # 非故障
    assert REASON_ZT in detail

    out = query_collection_log(trade_date=TODAY, status="planned_absent")
    assert out["status"] == "ok"
    assert out["count"] == 1
    assert out["rows"][0]["section"] == "zt_pool"


def test_planned_absent_core_section_does_not_fail_fast(pg_test, monkeypatch):
    """核心 section（boards）在册也不触发 fail-fast——根本不抓取，故障无从谈起。"""
    _init(pg_test)
    _registry(monkeypatch, {"boards": "已定申万行业口径承载（spec #111），接线中"})
    fake = FakeBoardAdapter("fake", fail_sections=("boards",), exc=_conn_exc("fake"))
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[fake])

    assert result["status"] == "ok"
    assert result["sections"]["boards"]["status"] == "planned_absent"
    assert "boards" not in fake.calls
    assert _count(pg_test, "market_boards") == 0


def test_planned_absent_explicit_request_not_all_failed_error(pg_test, monkeypatch):
    """显式只请求在册 section：占位语义下不是"全部抓取失败"，整体 ok。"""
    _init(pg_test)
    _registry(monkeypatch, {"zt_pool": REASON_ZT})
    result = fetch_board_snapshot(
        trade_date=TODAY, sections="zt_pool", adapters=[FakeBoardAdapter("fake")]
    )
    assert result["status"] == "ok"
    assert result["failed_sections"] == []
    assert result["sections"]["zt_pool"]["status"] == "planned_absent"


def test_all_sections_planned_absent_overall_ok(pg_test, monkeypatch):
    """全部 section 在册：整体仍 ok（迁移真空期复盘链路照常出报的前提）。"""
    _init(pg_test)
    _registry(monkeypatch, {name: "接线中" for name in tools_board.ALL_SECTIONS})
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[FakeBoardAdapter("fake")])
    assert result["status"] == "ok"
    assert result["failed_sections"] == []
    assert all(
        s["status"] == "planned_absent" for s in result["sections"].values()
    )


# ---------------------------------------------------------------- 回归：真故障仍中止


def test_real_core_error_still_aborts_with_registry_present(pg_test, monkeypatch):
    """回归不变：在册 planned_absent（lhb）与真故障（boards 连接级）并存时，
    仍按 error 中止——failed_sections 只含 boards，lhb 占位不计入。"""
    _init(pg_test)
    _registry(monkeypatch, {"lhb": "已定交易所官方承载（spec #111），接线中"})
    ak = FakeBoardAdapter("akshare", fail_sections=("boards",), exc=_conn_exc("akshare"))
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])

    assert result["status"] == "error"
    assert result["error_kind"] == "upstream_unreachable"
    assert result["retry_advice"]
    assert result["failed_sections"] == ["boards"]
    assert result["sections"]["lhb"]["status"] == "planned_absent"


def test_registry_does_not_mask_real_failure_of_other_sections(pg_test, monkeypatch):
    """在册 section 之外的真故障不被占位语义掩盖：zt_pool 连接级失败仍
    触发核心 section fail-fast。"""
    _init(pg_test)
    _registry(monkeypatch, {"lhb": "接线中"})
    ak = FakeBoardAdapter("akshare", fail_sections=("zt_pool",), exc=_conn_exc("akshare"))
    result = fetch_board_snapshot(trade_date=TODAY, adapters=[ak])

    assert result["status"] == "error"
    assert result["error_kind"] == "upstream_unreachable"
    assert result["failed_sections"] == ["zt_pool"]
    assert result["sections"]["lhb"]["status"] == "planned_absent"


# ---------------------------------------------------------------- 台账基元


def test_record_collection_planned_absent_accepted_unknown_rejected(pg_test):
    """LEDGER_STATUSES 词表：planned_absent 可落账；词表外值仍 fail-fast。"""
    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    record_collection(
        conn, trade_date=TODAY, section="zt_pool", status="planned_absent", rows=0
    )
    with pytest.raises(ValueError):
        record_collection(conn, trade_date=TODAY, section="zt_pool", status="absent")
    conn.close()


def test_existing_db_three_value_check_migrated_by_016(pg_test):
    """现库旧形态（三值 CHECK）经 ensure_schema 的 sql/016 幂等迁移为四值：
    迁移后 planned_absent 可落账、存量行保留；重复执行不重复迁移。"""
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE collection_log (
                   id bigserial PRIMARY KEY,
                   trade_date char(8) NOT NULL,
                   section text NOT NULL,
                   status text NOT NULL CHECK (status IN ('ok', 'empty', 'error')),
                   source text,
                   rows integer,
                   error_kind text,
                   detail text,
                   started_at timestamptz,
                   finished_at timestamptz NOT NULL DEFAULT now())"""
        )
        cur.execute(
            "INSERT INTO collection_log (trade_date, section, status)"
            " VALUES ('20261020', 'boards', 'ok')"
        )
    conn.commit()

    ensure_schema(conn)  # sql/016：三值 CHECK → 四值
    ensure_schema(conn)  # 幂等：第二次 no-op
    record_collection(
        conn, trade_date=TODAY, section="zt_pool", status="planned_absent", rows=0
    )
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM collection_log ORDER BY id")
        statuses = [r[0] for r in cur.fetchall()]
    conn.close()
    assert statuses == ["ok", "planned_absent"]  # 存量行保留 + 第四态可落账


# ---------------------------------------------------------------- 收盘后批 CLI


def _run_close(capsys, adapters):
    rc = scheduler.main(["close"], adapters=adapters)
    return rc, json.loads(capsys.readouterr().out)


def test_close_batch_treats_planned_absent_as_success(pg_test, monkeypatch, capsys):
    """固定 CLI 入口真实跑通：收盘后批含 planned_absent section 时 exit 0，
    占位 section 出现在出批 JSON 但不计 failed_sections。"""
    _init(pg_test)
    _registry(monkeypatch, {"zt_pool": REASON_ZT})
    rc, out = _run_close(capsys, [FakeBoardAdapter("fake")])

    assert rc == 0
    assert out["status"] == "ok"
    assert out["failed_sections"] == []
    assert out["sections"]["zt_pool"]["status"] == "planned_absent"
    assert out["sections"]["indices"]["status"] == "ok"
    # 占位落账走同一台账
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM collection_log"
            " WHERE trade_date = %s AND section = 'zt_pool' AND status = 'planned_absent'",
            (TODAY,),
        )
        assert cur.fetchone()[0] == 1
    conn.close()
