"""volume 存量迁移机检与迁移（issue #79，ADR 0008）：手 → 股一次性运维动作。

机检（audit）：amount（万元）×1万 ÷ volume 反推单价，与价格列比值 ≈1 判股、
≈100 判手，与 source 列互证（efinance/akshare 手、baostock 股；market_indices
无 source 列按东财系计）。互证矛盾与缺列行进异常/unknown 清单。
迁移（apply）：只动判定为"手"且 source 互证一致的行，×100 分批执行；
已迁移行复核为股不再命中，天然幂等。
"""

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.migrate_volume import (
    audit_volume_units,
    classify_unit,
    migrate_volume_to_shares,
)


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


# ---------------------------------------------------------------- 单位反推分类（纯函数）


def test_classify_unit():
    # 股口径：amount(万元)*1e4 / volume(股) ≈ price
    assert classify_unit(volume=100000, amount_wan=100.0, price=10.0) == "gu"
    # 手口径：volume(手) 小 100 倍，反推单价 ≈ 100 × price
    assert classify_unit(volume=1000, amount_wan=100.0, price=10.0) == "shou"
    # 缺列/零值无法判定
    assert classify_unit(volume=None, amount_wan=100.0, price=10.0) == "unknown"
    assert classify_unit(volume=1000, amount_wan=None, price=10.0) == "unknown"
    assert classify_unit(volume=1000, amount_wan=100.0, price=0) == "unknown"
    assert classify_unit(volume=0, amount_wan=100.0, price=10.0) == "unknown"
    # 比值落不出区间 → anomaly
    assert classify_unit(volume=5000, amount_wan=100.0, price=10.0) == "anomaly"


# ---------------------------------------------------------------- PG 机检 + 迁移


def _seed_stock_daily(pg_test):
    """混源 seed：efinance 手口径、baostock 股口径、efinance 源但股口径（互证矛盾）、
    volume 缺失行。"""
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO stock_daily"
            " (stock_code, trade_date, close, volume, amount, adj, source)"
            " VALUES (%s, %s, %s, %s, %s, 'qfq', %s)",
            [
                # close=10, volume=1000手, amount=100万元 → 反推 100×close → 手
                ("600519", "20261013", 10.0, 1000, 100.0, "efinance"),
                # close=10, volume=100000股, amount=100万元 → 反推 =close → 股
                ("600519", "20261014", 10.0, 100000, 100.0, "baostock"),
                # efinance 源但股口径 → 互证矛盾 → 异常清单
                ("000001", "20261014", 10.0, 100000, 100.0, "efinance"),
                # volume 缺失 → unknown
                ("000001", "20261013", 10.0, None, None, "akshare"),
            ],
        )
    conn.commit()
    conn.close()


def _volumes(pg_test) -> dict:
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, trade_date, volume FROM stock_daily ORDER BY 1, 2"
        )
        rows = {(r[0], r[1]): r[2] for r in cur.fetchall()}
    conn.close()
    return rows


def test_audit_classifies_mixed_sources(pg_test):
    """机检对混源 seed 输出正确分类与异常清单。"""
    _init(pg_test)
    _seed_stock_daily(pg_test)
    conn = psycopg2.connect(pg_test)
    report = audit_volume_units(conn)
    conn.close()
    sd = report["stock_daily"]
    assert sd["total"] == 4
    assert sd["shou"] == 1
    assert sd["gu"] == 1
    assert sd["unknown"] == 1
    assert sd["migrate"] == 1  # 仅 efinance 手口径行
    assert sd["anomaly"] == 1
    anomaly = sd["anomalies"][0]
    assert anomaly["stock_code"] == "000001"
    assert anomaly["source"] == "efinance"
    assert "ratio" in anomaly


def test_migrate_only_shou_rows_and_idempotent(pg_test):
    """迁移只动互证一致的"手"行；baostock 股行与异常行不动；重跑零命中。"""
    _init(pg_test)
    _seed_stock_daily(pg_test)
    conn = psycopg2.connect(pg_test)
    result = migrate_volume_to_shares(conn, batch_size=2)
    conn.close()
    assert result["stock_daily"]["migrated"] == 1
    vols = _volumes(pg_test)
    assert vols[("600519", "20261013")] == 100000  # 手 → 股
    assert vols[("600519", "20261014")] == 100000  # baostock 原生股，不动
    assert vols[("000001", "20261014")] == 100000  # 异常行不动（值未 ×100）
    assert vols[("000001", "20261013")] is None
    # 重跑幂等：已迁移行复核为股，不再命中
    conn = psycopg2.connect(pg_test)
    again = migrate_volume_to_shares(conn, batch_size=2)
    conn.close()
    assert again["stock_daily"]["migrated"] == 0
    assert _volumes(pg_test) == vols


def test_audit_covers_all_volume_tables(pg_test):
    """机检覆盖全部含 volume 的表（stock_daily/index_daily/market_snapshot/market_indices）。"""
    _init(pg_test)
    conn = psycopg2.connect(pg_test)
    report = audit_volume_units(conn)
    conn.close()
    assert set(report) == {
        "stock_daily", "index_daily", "market_snapshot", "market_indices",
    }
    assert all(v["total"] == 0 for v in report.values())
