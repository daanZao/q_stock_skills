"""复权新模型切换与回滚的固定入口（issue #166）。"""

import json
import os
from datetime import datetime, timezone

from .adjustment_lock import lock_adjustment_facts
from .db import connect, ensure_schema


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cur.fetchone()[0] is not None


def _fact_watermark(cur):
    cur.execute(
        "SELECT"
        " (SELECT max(updated_at) FROM stock_daily_raw),"
        " (SELECT max(updated_at) FROM stock_adjust_factor),"
        " (SELECT max(rebuilt_at) FROM stock_daily_hfq),"
        " (SELECT max(last_rebuilt_at) FROM stock_factor_state)"
    )
    values = [value for value in cur.fetchone() if value is not None]
    return max(values) if values else None


_REPAIR_PRICE_SOURCES = {
    "none": ("", ["r.open", "r.high", "r.low", "r.close"]),
    "hfq": (
        "JOIN stock_daily_hfq h"
        " ON h.stock_code=r.stock_code AND h.trade_date=r.trade_date",
        ["h.open", "h.high", "h.low", "h.close"],
    ),
    "qfq": (
        "JOIN stock_daily_hfq h"
        " ON h.stock_code=r.stock_code AND h.trade_date=r.trade_date"
        " JOIN stock_factor_state s ON s.stock_code=r.stock_code"
        " AND s.factor_latest IS NOT NULL",
        [f"h.{field} / s.factor_latest" for field in ("open", "high", "low", "close")],
    ),
}


def _repair_legacy(cur) -> dict[str, int]:
    window_start = os.environ.get("QSTOCK_ADJ_WINDOW_START", "20230101").replace("-", "")
    cur.execute(
        "DELETE FROM stock_daily d WHERE d.adj='qfq' AND d.trade_date>=%s"
        " AND EXISTS (SELECT 1 FROM stock_daily_raw r WHERE r.stock_code=d.stock_code)",
        (window_start,),
    )
    counts = {}
    for adj, (join_sql, prices) in _REPAIR_PRICE_SOURCES.items():
        cur.execute(
            f"""
            INSERT INTO stock_daily (
                stock_code, trade_date, open, high, low, close, volume, amount,
                amplitude, change_percent, change_amount, turnover_rate,
                adj, source, tradestatus, updated_at
            )
            SELECT r.stock_code, r.trade_date, {', '.join(prices)},
                   r.volume, r.amount, r.amplitude, r.change_percent, r.change_amount,
                   r.turnover_rate, %s, r.source, r.tradestatus, CURRENT_TIMESTAMP
            FROM stock_daily_raw r
            {join_sql}
            ON CONFLICT (stock_code, trade_date, adj) DO UPDATE SET
                open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                close=EXCLUDED.close, volume=EXCLUDED.volume, amount=EXCLUDED.amount,
                amplitude=EXCLUDED.amplitude, change_percent=EXCLUDED.change_percent,
                change_amount=EXCLUDED.change_amount, turnover_rate=EXCLUDED.turnover_rate,
                source=EXCLUDED.source, tradestatus=EXCLUDED.tradestatus,
                updated_at=CURRENT_TIMESTAMP
            """,
            (adj,),
        )
        counts[adj] = cur.rowcount
    return counts


def _verify_legacy(cur) -> dict[str, int]:
    checks = {}
    for adj, source_sql in (
        ("none", "stock_daily_raw r"),
        ("hfq", "stock_daily_hfq r"),
        (
            "qfq",
            "stock_daily_hfq r JOIN stock_factor_state s"
            " ON s.stock_code=r.stock_code AND s.factor_latest IS NOT NULL",
        ),
    ):
        cur.execute(
            f"SELECT COUNT(*) FROM {source_sql}"
            " LEFT JOIN stock_daily d"
            " ON d.stock_code=r.stock_code AND d.trade_date=r.trade_date AND d.adj=%s"
            " WHERE d.stock_code IS NULL",
            (adj,),
        )
        checks[f"missing_{adj}"] = cur.fetchone()[0]
    window_start = os.environ.get("QSTOCK_ADJ_WINDOW_START", "20230101").replace("-", "")
    cur.execute(
        "SELECT COUNT(*) FROM stock_daily d"
        " WHERE d.adj='qfq' AND d.trade_date>=%s"
        " AND EXISTS (SELECT 1 FROM stock_daily_raw r WHERE r.stock_code=d.stock_code)"
        " AND NOT EXISTS ("
        " SELECT 1 FROM stock_daily_hfq h"
        " JOIN stock_factor_state s ON s.stock_code=h.stock_code"
        " AND s.factor_latest IS NOT NULL"
        " WHERE h.stock_code=d.stock_code AND h.trade_date=d.trade_date"
        " )",
        (window_start,),
    )
    checks["stale_qfq"] = cur.fetchone()[0]
    if any(checks.values()):
        raise RuntimeError(f"legacy 修复完整性验证失败: {checks}")
    return checks


def _write_report(report_dir: str | None, action: str, result: dict) -> None:
    if report_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_dir = os.path.join("files", f"adj-switch-{action}_{stamp}")
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "summary.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
    result["report"] = path


def run_adjustment_switch(action: str, *, report_dir: str | None = None) -> dict:
    params = {"action": action, "report_dir": report_dir}
    if action not in ("status", "cutover", "rollback", "roll-forward"):
        return {"status": "error", "tool": "adj-switch", "params": params,
                "error": f"未知 action: {action}"}
    conn, err = connect()
    if err:
        return {"status": "error", "tool": "adj-switch", "params": params, "error": err}
    try:
        if (
            action != "status"
            and not conn.info.dbname.startswith("qstock_test")
            and os.environ.get("QSTOCK_ADJ_SWITCH_ENABLE_PRODUCTION") != "true"
        ):
            return {
                "status": "error",
                "tool": "adj-switch",
                "params": params,
                "error_kind": "production_not_enabled",
                "error": (
                    "非 qstock_test 数据库仅允许 #184 在预检完成后显式设置 "
                    "QSTOCK_ADJ_SWITCH_ENABLE_PRODUCTION=true"
                ),
            }
        ensure_schema(conn)
        lock_adjustment_facts(conn)
        repair = {"none": 0, "hfq": 0, "qfq": 0}
        verification = {
            "missing_none": 0,
            "missing_hfq": 0,
            "missing_qfq": 0,
            "stale_qfq": 0,
        }
        repair_required = False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mode, fact_watermark FROM stock_adjustment_switch_state"
                " WHERE id=1 FOR UPDATE"
            )
            mode, watermark = cur.fetchone()
            if action == "status":
                conn.rollback()
                return {"status": "ok", "tool": "adj-switch", "params": params, "mode": mode}
            if action in ("cutover", "roll-forward"):
                if mode != "legacy" or not _table_exists(cur, "stock_daily"):
                    conn.rollback()
                    return {"status": "error", "tool": "adj-switch", "params": params,
                            "error": f"{action} 要求 legacy 模式且 stock_daily 存在"}
                if _table_exists(cur, "stock_daily_legacy"):
                    conn.rollback()
                    return {"status": "error", "tool": "adj-switch", "params": params,
                            "error": "stock_daily_legacy 已存在，拒绝覆盖"}
                watermark = _fact_watermark(cur)
                cur.execute("ALTER TABLE stock_daily RENAME TO stock_daily_legacy")
                target_mode = "new"
            else:
                if mode != "new" or not _table_exists(cur, "stock_daily_legacy"):
                    conn.rollback()
                    return {"status": "error", "tool": "adj-switch", "params": params,
                            "error": "rollback 要求 new 模式且 stock_daily_legacy 存在"}
                if _table_exists(cur, "stock_daily"):
                    conn.rollback()
                    return {"status": "error", "tool": "adj-switch", "params": params,
                            "error": "stock_daily 已存在，拒绝覆盖"}
                current_watermark = _fact_watermark(cur)
                repair_required = (
                    current_watermark is not None
                    and (watermark is None or current_watermark > watermark)
                )
                cur.execute("ALTER TABLE stock_daily_legacy RENAME TO stock_daily")
                if repair_required:
                    repair = _repair_legacy(cur)
                    verification = _verify_legacy(cur)
                target_mode = "legacy"
            cur.execute(
                "UPDATE stock_adjustment_switch_state"
                " SET mode=%s, fact_watermark=%s,"
                " switched_at=CASE WHEN %s='new' THEN CURRENT_TIMESTAMP ELSE switched_at END,"
                " updated_at=CURRENT_TIMESTAMP WHERE id=1",
                (target_mode, watermark, target_mode),
            )
        conn.commit()
        result = {
            "status": "ok",
            "tool": "adj-switch",
            "params": params,
            "mode": target_mode,
            "required_env": {"QSTOCK_PRICE_SOURCE": target_mode},
            "upstream_requests": 0,
        }
        if action == "rollback":
            result["repair_required"] = repair_required
            result["repair"] = repair
            result["verification"] = verification
        try:
            _write_report(report_dir, action, result)
        except Exception as exc:  # noqa: BLE001 - 提交后工件失败必须保留已提交事实
            result.update({
                "status": "partial_error",
                "committed": True,
                "error": f"数据库切换已提交，但执行工件写入失败: {exc}",
            })
        return result
    except Exception as exc:  # noqa: BLE001 - CLI 核心层收敛为结构化错误
        conn.rollback()
        return {"status": "error", "tool": "adj-switch", "params": params,
                "error": f"复权切换失败: {exc}"}
    finally:
        conn.close()
