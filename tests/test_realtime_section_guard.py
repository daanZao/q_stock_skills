"""服务端防线（issue #78，spec #71/W1 + #45 根治）：历史日期拒绝 + 盘中禁写。

- 实时语义 section（boards/indices）对历史日期调用：section 级 unsupported
  （detail=realtime_section_no_backfill），零落库；
- indices 段落库前校验行级日期==请求日期，不符即 section error（date_mismatch）零落库；
- snapshot API 生效日期≠传入日期：status error + detail=date_mismatch，零落库；
- 交易时段内（交易日 09:30–15:00）stock_daily/index_daily 写路径跳过当日行落库
  （响应带 note），盘中 fetch/query 正常返回数据；盘后行为不变。
"""

from datetime import datetime, timezone, timedelta

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_board import fetch_board_snapshot
from qstock_mcp.tools_daily import fetch_daily, query_daily
from qstock_mcp.tools_init import init_database
from qstock_mcp.tools_snapshot import fetch_market_snapshot

from fakes import (
    INDEX_ROWS,
    FakeAdapter,
    FakeBoardAdapter,
    FakeInitAdapter,
    FakeSnapshotAdapter,
    trading_rows,
)

_TZ8 = timezone(timedelta(hours=8))
# 2026-10-16 为周五交易日；10:30 盘中 / 16:00 盘后
_INTRADAY = datetime(2026, 10, 16, 10, 30, tzinfo=_TZ8)
_AFTER_CLOSE = datetime(2026, 10, 16, 16, 0, tzinfo=_TZ8)
_TODAY = "20261016"


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _count(pg_test, table: str, where: str = "") -> int:
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} {where}")
        n = cur.fetchone()[0]
    conn.close()
    return n


def _guard_ledger(pg_test, section: str) -> list[dict]:
    """某 section 的台账行（按写入顺序）：拒收落账断言用。"""
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, section, status, error_kind, detail"
            " FROM collection_log WHERE section = %s ORDER BY id",
            (section,),
        )
        cols = ["trade_date", "section", "status", "error_kind", "detail"]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------- 实时 section 历史日期拒绝


def test_realtime_section_historical_date_rejected(pg_test):
    """boards/indices 对历史日期：unsupported + detail，零落库。"""
    _init(pg_test)
    result = fetch_board_snapshot(
        trade_date="20261014", sections="boards,indices", adapters=[FakeBoardAdapter("ak")]
    )
    assert result["status"] == "error"
    assert result["error_kind"] == "unsupported"
    for name in ("boards", "indices"):
        sec = result["sections"][name]
        assert sec["status"] == "error"
        assert sec["error_kind"] == "unsupported"
        assert sec["detail"] == "realtime_section_no_backfill"
    assert _count(pg_test, "market_boards") == 0
    assert _count(pg_test, "market_indices") == 0
    # 拒收落账（issue #77）：被拒绝的历史回补尝试留审计痕迹
    for name in ("boards", "indices"):
        rows = _guard_ledger(pg_test, name)
        assert len(rows) == 1
        assert rows[0]["status"] == "error"
        assert rows[0]["error_kind"] == "unsupported"
        assert rows[0]["detail"].startswith("guard_reject")


def test_mixed_sections_historical_date_partial(pg_test):
    """历史日期混合请求：实时 section 拒绝，历史语义 section（zt_pool）照常。"""
    _init(pg_test)
    result = fetch_board_snapshot(
        trade_date="20261014", sections="boards,zt_pool", adapters=[FakeBoardAdapter("ak")]
    )
    assert result["status"] == "ok"  # 非全败
    assert result["sections"]["boards"]["error_kind"] == "unsupported"
    assert result["sections"]["zt_pool"]["status"] == "ok"
    assert _count(pg_test, "market_boards") == 0
    assert _count(pg_test, "zt_pool") > 0


def test_same_day_realtime_sections_unchanged(pg_test):
    """当日正常路径行为不变：boards/indices 照常抓取落库。"""
    _init(pg_test)
    result = fetch_board_snapshot(adapters=[FakeBoardAdapter("ak")])  # 缺省当天
    assert result["status"] == "ok"
    assert result["sections"]["boards"]["status"] == "ok"
    assert _count(pg_test, "market_boards") > 0


# ---------------------------------------------------------------- indices 行级日期校验


def test_indices_row_date_mismatch_rejected(pg_test):
    """indices 行级日期 != 请求日期 → section error（date_mismatch），零落库。"""
    _init(pg_test)
    stale = [
        {**row, "trade_date": "20261015"} for row in INDEX_ROWS
    ]  # 行级日期为昨日，请求为今天
    result = fetch_board_snapshot(
        sections="indices", adapters=[FakeBoardAdapter("ak", rows={"indices": stale})]
    )
    sec = result["sections"]["indices"]
    assert sec["status"] == "error"
    assert sec["detail"] == "date_mismatch"
    assert _count(pg_test, "market_indices") == 0
    # 拒收落账（issue #77）
    rows = _guard_ledger(pg_test, "indices")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["error_kind"] == "date_mismatch"


def test_indices_row_date_match_ok(pg_test):
    """行级日期与请求日期一致（源行级日期=当日）正常落库。"""
    _init(pg_test)
    fresh = [{**row, "trade_date": None} for row in INDEX_ROWS]
    result = fetch_board_snapshot(
        sections="indices", adapters=[FakeBoardAdapter("ak", rows={"indices": fresh})]
    )
    assert result["sections"]["indices"]["status"] == "ok"
    assert _count(pg_test, "market_indices") == len(INDEX_ROWS)


# ---------------------------------------------------------------- snapshot 日期不符


def test_snapshot_api_date_mismatch_rejected(pg_test):
    """传入日期与 API 生效日期不符 → status error + detail=date_mismatch，零落库。"""
    _init(pg_test)
    result = fetch_market_snapshot(
        trade_date="20261014",
        adapters=[FakeSnapshotAdapter("s", trade_date="20261016")],
    )
    assert result["status"] == "error"
    assert result["detail"] == "date_mismatch"
    assert _count(pg_test, "market_snapshot") == 0
    # 拒收落账（issue #77）：台账按请求日期记被拒的污染尝试
    rows = _guard_ledger(pg_test, "market_snapshot")
    assert len(rows) == 1
    assert rows[0]["trade_date"] == "20261014"
    assert rows[0]["status"] == "error"
    assert rows[0]["error_kind"] == "date_mismatch"


def test_snapshot_api_date_none_with_passed_date_rejected(pg_test):
    """API 未报告日期而调用方传了日期：无法自证归属 → date_mismatch，零落库。"""
    _init(pg_test)
    result = fetch_market_snapshot(
        trade_date="20261014",
        adapters=[FakeSnapshotAdapter("s", trade_date=None)],
    )
    assert result["status"] == "error"
    assert result["detail"] == "date_mismatch"
    assert _count(pg_test, "market_snapshot") == 0


def test_snapshot_no_param_api_date_accepted(pg_test):
    """不传日期时 API 报告日期优先（如周末取周五快照），行为不变。"""
    _init(pg_test)
    result = fetch_market_snapshot(
        adapters=[FakeSnapshotAdapter("s", trade_date="20261016")]
    )
    assert result["status"] == "ok"
    assert result["trade_date"] == "20261016"
    assert _count(pg_test, "market_snapshot") > 0


# ---------------------------------------------------------------- 盘中禁写


def test_intraday_daily_write_skipped(pg_test):
    """盘中 fetch_daily：当日行不落库、响应带 note；历史行照常落库。"""
    _init(pg_test)
    result = fetch_daily(
        "600519", start="20261013", end=_TODAY,
        adapters=[FakeAdapter("efake", rows_fn=trading_rows)], now=_INTRADAY,
    )
    assert result["status"] == "ok"
    assert "note" in result
    assert _count(pg_test, "stock_daily", "WHERE trade_date = '20261016'") == 0
    assert _count(pg_test, "stock_daily", "WHERE trade_date < '20261016'") > 0


def test_intraday_query_returns_but_not_persisted(pg_test):
    """盘中 query_daily：返回含当日行（标注 note）但不落库，再查不累积。"""
    _init(pg_test)
    out = query_daily(
        "600519", start="20261013", end=_TODAY,
        adapters=[FakeAdapter("efake", rows_fn=trading_rows)], now=_INTRADAY,
    )
    assert out["status"] == "ok"
    assert "note" in out
    dates = [r["trade_date"] for r in out["rows"]]
    assert _TODAY in dates  # 盘中数据正常返回
    assert _count(pg_test, "stock_daily", "WHERE trade_date = '20261016'") == 0


def test_after_close_daily_write_unchanged(pg_test):
    """盘后行为不变：当日行正常落库，无 note。"""
    _init(pg_test)
    result = fetch_daily(
        "600519", start="20261013", end=_TODAY,
        adapters=[FakeAdapter("efake", rows_fn=trading_rows)], now=_AFTER_CLOSE,
    )
    assert result["status"] == "ok"
    assert "note" not in result
    assert _count(pg_test, "stock_daily", "WHERE trade_date = '20261016'") == 1


def test_intraday_init_index_daily_skips_today(pg_test):
    """盘中 init 指数日线：当日行不落库，历史行照常。"""
    _init(pg_test)
    init_database(
        adapters=[FakeInitAdapter("ifake")], now=_INTRADAY,
    )
    assert _count(pg_test, "index_daily", "WHERE trade_date >= '20261016'") == 0
    assert _count(pg_test, "index_daily") > 0
