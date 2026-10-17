"""volume 存量迁移：手 → 股（issue #79，spec #71/W3，ADR 0008 的一次性运维动作）。

用法（一次性运维，不入 MCP 工具面）：
    python -m qstock_mcp.migrate_volume --audit            # 机检报告（只读，不落库）
    python -m qstock_mcp.migrate_volume --apply            # 分批迁移（判定"手"的行 ×100）

机检口径：amount（万元，ADR 0006）×1万 ÷ volume 反推单价，与价格列比值 ≈1 判股、
≈100 判手，与 source 列互证（efinance/akshare 迁移前为手、baostock 原生为股；
market_indices 无 source 列——盘面 section 实为东财系单源，按东财系计）。
互证矛盾（source 说手口径但数据像股，或反之）与比值落不出区间的行进异常清单；
缺列/零值行无法判定归 unknown。迁移只动判定为"手"且互证一致的行，已迁移行
复核为股不再命中，天然幂等。生产执行前人工确认机检清单（spec #71 Testing）。
"""

import argparse
import json
import logging

from .db import connect

log = logging.getLogger(__name__)

# 含 volume 的表：(表名, 代码列, 价格列)；market_indices 无 source 列（盘面表 DDL 决策）
_VOLUME_TABLES = (
    ("stock_daily", "stock_code", "close"),
    ("index_daily", "index_code", "close"),
    ("market_snapshot", "stock_code", "latest_price"),
    ("market_indices", "index_code", "latest_price"),
)

# 东财系源：迁移前 volume 为手口径；baostock 原生为股
_EM_SOURCES = ("efinance", "akshare")

# 反推单价 / 价格 的比值区间：≈1 判股、≈100 判手（区间留足价格日内波动余量）
_GU_RANGE = (0.5, 2.0)
_SHOU_RANGE = (50.0, 200.0)

# 异常清单截断：报告是可审摘要，全量异常行数在 anomaly 计数里
_ANOMALY_SAMPLE = 100

_BATCH = 5000


def classify_unit(volume, amount_wan, price) -> str:
    """反推 volume 单位：gu（股）/ shou（手）/ unknown（缺列或零值）/ anomaly。

    implied_price = amount（万元）×1e4 ÷ volume；implied/price ≈1 → 股，
    ≈100 → 手，落不出区间 → anomaly。
    """
    if not volume or amount_wan is None or not price:
        return "unknown"
    ratio = (amount_wan * 1e4 / volume) / price
    if _GU_RANGE[0] <= ratio <= _GU_RANGE[1]:
        return "gu"
    if _SHOU_RANGE[0] <= ratio <= _SHOU_RANGE[1]:
        return "shou"
    return "anomaly"


def _scan_table(conn, table: str, code_col: str, price_col: str) -> list[dict]:
    """全量扫描一张表的 volume 行，逐行分类并给出处置判定。"""
    # market_indices 无 source 列（盘面表 DDL 决策）：以 NULL 占位，按东财系计
    source_expr = "source" if table != "market_indices" else "NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, {code_col}, trade_date, volume, amount, {price_col},"
            f" {source_expr} FROM {table} ORDER BY id"
        )
        rows = []
        for rid, code, trade_date, volume, amount, price, source in cur.fetchall():
            unit = classify_unit(volume, amount, price)
            # 期望口径（迁移前）：东财系=手；baostock=股；无 source（盘面表）按东财系
            expected = "gu" if source == "baostock" else "shou"
            if unit == "unknown":
                decision = "unknown"
            elif unit == expected == "shou":
                decision = "migrate"
            elif unit == expected == "gu":
                decision = "ok"
            else:
                decision = "anomaly"  # 互证矛盾或比值异常
            rows.append(
                {
                    "id": rid,
                    "code": code,
                    "trade_date": trade_date,
                    "volume": volume,
                    "amount": amount,
                    "price": price,
                    "source": source,
                    "unit": unit,
                    "decision": decision,
                }
            )
    return rows


def _report(rows: list[dict]) -> dict:
    """按处置判定计数：migrate（东财系手口径）/ ok（股口径一致）/ unknown / anomaly。"""
    counts = {"total": len(rows), "gu": 0, "shou": 0, "unknown": 0, "anomaly": 0, "migrate": 0}
    anomalies: list[dict] = []
    for r in rows:
        decision = r["decision"]
        if decision == "migrate":
            counts["shou"] += 1
            counts["migrate"] += 1
        elif decision == "ok":
            counts["gu"] += 1
        elif decision == "unknown":
            counts["unknown"] += 1
        else:
            counts["anomaly"] += 1
            if len(anomalies) < _ANOMALY_SAMPLE:
                anomalies.append(
                    {
                        "stock_code": r["code"],
                        "trade_date": r["trade_date"],
                        "source": r["source"],
                        "volume": r["volume"],
                        "amount": r["amount"],
                        "price": r["price"],
                        "ratio": round(r["amount"] * 1e4 / r["volume"] / r["price"], 2),
                    }
                )
    counts["anomaly_rows_sampled"] = len(anomalies)
    return {**counts, "anomalies": anomalies}


def audit_volume_units(conn) -> dict:
    """全量机检（只读）：逐表分类计数 + 异常清单（截断采样）。"""
    report = {}
    for table, code_col, price_col in _VOLUME_TABLES:
        report[table] = _report(_scan_table(conn, table, code_col, price_col))
    return report


def migrate_volume_to_shares(conn, batch_size: int = _BATCH) -> dict:
    """分批迁移：只 UPDATE 判定为 migrate 的行（×100），逐表逐批 commit。

    天然幂等：已迁移行复核为股（gu）不再命中；互证矛盾行（anomaly）绝不动。
    """
    result = {}
    for table, code_col, price_col in _VOLUME_TABLES:
        rows = [r for r in _scan_table(conn, table, code_col, price_col) if r["decision"] == "migrate"]
        migrated = 0
        for i in range(0, len(rows), batch_size):
            ids = [r["id"] for r in rows[i : i + batch_size]]
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {table} SET volume = volume * 100 WHERE id = ANY(%s)",
                    (ids,),
                )
                migrated += cur.rowcount
            conn.commit()
            log.info("%s 迁移批次 %d 行", table, len(ids))
        result[table] = {"migrated": migrated}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="volume 存量迁移：手 → 股（issue #79）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audit", action="store_true", help="机检报告（只读）")
    group.add_argument("--apply", action="store_true", help="分批迁移（判定'手'的行 ×100）")
    parser.add_argument("--batch-size", type=int, default=_BATCH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn, err = connect()
    if err:
        print(json.dumps({"status": "error", "error": err}, ensure_ascii=False))
        return 1
    try:
        out = audit_volume_units(conn) if args.audit else migrate_volume_to_shares(conn, args.batch_size)
    finally:
        conn.close()
    print(json.dumps({"status": "ok", "mode": "audit" if args.audit else "apply", "report": out}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
