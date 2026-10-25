"""stock_daily tradestatus 列 + query_daily 停牌标注（issue #80，spec #71/W2 MCP 侧）。

- stock_daily 加 tradestatus 列（DDL 迁移幂等、现库不丢数据）；
- baostock 路径写入权威停牌状态（0 停牌/1 正常），腾讯/新浪/akshare 路径为 null；
- query_daily 输出的停牌标注 tradestatus 优先、启发式（OHLC 全等且 volume/
  change_percent 为 null）兜底，停牌占位行与数据缺失行在数据层可区分。
"""

import psycopg2

from qstock_mcp.adapters.baostock_adapter import map_baostock_rows
from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_daily import query_daily

from fakes import FakeAdapter


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _row(date, close=10.0, volume=100.0, change_percent=0.5, tradestatus=None):
    """单行日线 fake（OHLC 全等 + volume/change_percent None 即停牌占位形态）。"""
    return {
        "trade_date": date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "amount": 1000.0,
        "amplitude": 1.0,
        "change_percent": change_percent,
        "change_amount": 0.05,
        "turnover_rate": 1.0,
        "tradestatus": tradestatus,
    }


# ---------------------------------------------------------------- DDL 迁移


def test_schema_has_tradestatus_column(pg_test):
    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_name = 'stock_daily' AND column_name = 'tradestatus'"
        )
        assert cur.fetchone()[0] == "integer"
    conn.close()


def test_existing_db_migration_keeps_data(pg_test):
    """现库迁移：旧形态表（无 tradestatus 列）加列不丢数据，幂等重跑。"""
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE stock_daily (
                id bigserial PRIMARY KEY,
                stock_code text NOT NULL,
                trade_date text NOT NULL,
                open real, high real, low real, close real,
                volume bigint, amount double precision,
                amplitude real, change_percent real, change_amount real,
                turnover_rate real,
                adj text NOT NULL DEFAULT 'qfq',
                source text,
                created_at timestamp DEFAULT CURRENT_TIMESTAMP,
                updated_at timestamp DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT stock_daily_code_date_adj_key
                    UNIQUE (stock_code, trade_date, adj)
            )
            """
        )
        cur.execute(
            "INSERT INTO stock_daily (stock_code, trade_date, close, adj, source)"
            " VALUES ('600519', '20261013', 1700.0, 'qfq', 'akshare')"
        )
    conn.commit()
    ensure_schema(conn)  # 幂等迁移：加列
    ensure_schema(conn)  # 重跑幂等
    with conn.cursor() as cur:
        cur.execute(
            "SELECT close, tradestatus FROM stock_daily"
            " WHERE stock_code = '600519' AND trade_date = '20261013'"
        )
        close, tradestatus = cur.fetchone()
    conn.close()
    assert close == 1700.0
    assert tradestatus is None


# ---------------------------------------------------------------- baostock 权威停牌状态


def test_baostock_mapping_tradestatus():
    fields = "date,open,high,low,close,volume,amount,pctChg,tradestatus".split(",")
    rows = [
        ["2026-10-13", "10.0", "10.5", "9.8", "10.2", "12345.0", "125000.5", "2.0", "1"],
        ["2026-10-14", "10.2", "10.2", "10.2", "10.2", "0", "0", "0", "0"],
    ]
    bars = map_baostock_rows(fields, rows)
    assert bars[0]["tradestatus"] == 1
    assert bars[1]["tradestatus"] == 0


def test_baostock_mapping_without_tradestatus_field():
    """旧字段集（无 tradestatus 列）映射为 None，不伪造。"""
    fields = "date,open,high,low,close,volume,amount,pctChg".split(",")
    rows = [["2026-10-13", "10.0", "10.5", "9.8", "10.2", "12345.0", "125000.5", "2.0"]]
    (bar,) = map_baostock_rows(fields, rows)
    assert bar["tradestatus"] is None


# ---------------------------------------------------------------- query_daily 停牌标注


def test_query_daily_marks_suspended_by_tradestatus(pg_test):
    """tradestatus 权威标注：0 → suspended:true（basis tradestatus），1 → false。"""
    _init(pg_test)
    rows = [
        _row("20261013", tradestatus=1),
        _row("20261014", volume=0.0, change_percent=0.0, tradestatus=0),
    ]
    out = query_daily(
        "600519", start="20261013", end="20261014",
        adapters=[FakeAdapter("bsfake", rows=rows)],
    )
    assert out["status"] == "ok"
    normal, suspended = out["rows"]
    assert normal["suspended"] is False
    assert normal["suspended_basis"] == "tradestatus"
    assert suspended["suspended"] is True
    assert suspended["suspended_basis"] == "tradestatus"
    assert suspended["tradestatus"] == 0


def test_query_daily_heuristic_fallback(pg_test):
    """tradestatus 缺失（东财系路径）→ 启发式兜底：OHLC 全等且 volume/
    change_percent 为 null 的行标注 suspended:true（basis heuristic）。"""
    _init(pg_test)
    rows = [
        _row("20261013"),
        _row("20261014", volume=None, change_percent=None),  # 停牌占位形态
    ]
    out = query_daily(
        "600519", start="20261013", end="20261014",
        adapters=[FakeAdapter("effake", rows=rows)],
    )
    normal, placeholder = out["rows"]
    assert normal["suspended"] is False
    assert normal["suspended_basis"] is None
    assert placeholder["suspended"] is True
    assert placeholder["suspended_basis"] == "heuristic"
    assert placeholder["tradestatus"] is None
