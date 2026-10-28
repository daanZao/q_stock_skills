"""龙虎榜固定入口核心层（issue #114，spec #111）：单日抓取 / 历史回溯 / 新浪对拍。

CLI（qstock-scheduler lhb，手册 docs/lhb-source-cli.md）直调本层，不经 MCP
协议层：
- fetch_lhb_day：单日抓取落库——复用 fetch_board_snapshot 的 lhb 专用链
  （三态契约、台账、业务键 upsert 全继承），幂等可重跑。
- fetch_lhb_backfill：区间历史回溯（逐交易日；沪深实测回溯到 2015，北交所
  到 2021-11 开市当周，#97/#107）；单日失败不中断、逐日落台账，进度经
  logging 走 stderr（issue #89 双阈值模式）。深圳默认同花顺主采、官方保留，
  沪深北其他市场仅在显式 fallback=ths 时补缺（issue #128，ADR 0016）。
- compare_lhb_sources：新浪互补对拍（可手动触发，不落库）——交易所官方
  主源 vs 新浪独立转载面，逐股比对股票集合/全日成交额/席位净买额；
  容差沿用 daily_parity 口径（成交额 rel 0.005；净买 rel 0.005，万元）。
"""

import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Sequence

from .adapters import BoardAdapter, default_lhb_adapters, default_lhb_history_adapters
from .adapters.lhb_exchange_adapter import ExchangeLhbAdapter
from .adapters.lhb_ths_adapter import ThsLhbAdapter
from .adapters._lhb_sina import SinaLhbClient, seat_net
from .dates import add_days, is_trading_day, normalize_date, today_str
from .daily_parity import _within_tolerance
from .db import connect
from .fetch_chain import (
    AllSourcesFailed, aggregate_error_kind, fetch_section_with_fallback,
)
from .ledger import record_collection, select_collection_log, summarize_attempted
from .output import aggregate_batch_status
from .output import error as _error
from .repository import (
    LHB_STATISTIC_WINDOW_DAYS, select_board_rows, select_lhb_statistic,
)
from .tools_board import (
    _lhb_all_markets_failure_result,
    _merge_lhb_basic_fallback,
    fetch_board_snapshot,
)

log = logging.getLogger(__name__)

PROGRESS_LOG_EVERY = 20  # 回溯每 N 日报告一次进度
PROGRESS_LOG_INTERVAL_S = 60  # 距上条 ≥N 秒也报（慢路径可观测，issue #89）

_AMOUNT_TOL = ("rel", 0.005)  # 成交额/净买额相对容差（daily_parity 同口径）
_PARITY_FIELDS = ("day_amount", "net_buy_amount")

# statistic 对拍字段（issue #114 定稿：库内自算 vs 新浪 ggtj 校验位）——
# 上榜次数精确对齐；金额万元 rel 0.005（daily_parity 同口径）。buy/sell_seat_count
# 自算恒 NULL（官方汇总无席位计数来源，不伪造），不参与比对
_STATISTIC_PARITY_FIELDS: dict[str, tuple[str, float]] = {
    "appear_count_3m": ("abs", 0.0),
    "buy_amount_3m": ("rel", 0.005),
    "sell_amount_3m": ("rel", 0.005),
    "net_buy_3m": ("rel", 0.005),
}
_STATISTIC_NOTE = (
    "自算快照窗口=[交易日-60日历日, 交易日]，ggtj 窗口锚定采集执行日——"
    "非同日对拍差异含窗口滑移成分，供人工核对；buy/sell_seat_count 自算恒 "
    "NULL（官方汇总无席位计数来源）不参与比对"
)


def _progress_due(i: int, total: int, last_logged: float, now_m: float) -> bool:
    return (
        i % PROGRESS_LOG_EVERY == 0
        or i == total
        or now_m - last_logged >= PROGRESS_LOG_INTERVAL_S
    )


def _latest_lhb_success(conn, trade_date: str) -> dict | None:
    for entry in select_collection_log(conn, trade_date=trade_date, section="lhb"):
        if entry["status"] in ("ok", "empty"):
            return entry
    return None


def _is_complete_lhb_day(entry: dict) -> bool:
    source = entry.get("source") or ""
    return (
        any(known in source for known in ("exchange", "ths"))
        and "partial_error=" not in (entry.get("detail") or "")
    )


def _is_official_lhb_day(entry: dict) -> bool:
    if not _is_complete_lhb_day(entry) or "exchange" not in entry["source"]:
        return False
    detail = entry.get("detail") or ""
    for raw_part in detail.split(";"):
        part = raw_part.strip()
        if part.startswith("basic_fallback="):
            markets = part.split(":", 1)[-1].split("(", 1)[0].split(",")
            if "szse" in markets:
                return False
        if part.startswith("szse_primary="):
            value = part.split("=", 1)[1].split("(", 1)[0]
            if "exchange" not in value.split("->")[-1]:
                return False
    return True


def _existing_lhb_inputs(
    conn, start: str, end: str
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    reusable: dict[str, list[dict]] = {}
    official: dict[str, dict] = {}
    d = start
    while d <= end:
        rows = select_board_rows(conn, "lhb_basic", "trade_date", "stock_code", d)
        entry = _latest_lhb_success(conn, d)
        if entry is not None and _is_complete_lhb_day(entry) and (
            (rows and entry["status"] == "ok")
            or (not rows and entry["status"] == "empty")
        ):
            reusable[d] = rows
            if _is_official_lhb_day(entry):
                official[d] = entry
        d = add_days(d, 1)
    return reusable, official


def fetch_lhb_day(
    trade_date: str | None = None,
    lhb_adapters: Sequence[BoardAdapter] | None = None,
    history: bool = False,
    staging_rows: list[dict] | None = None,
    lhb_fallback=None,
) -> dict:
    """单日龙虎榜抓取落库；history=True 时仅 basic 按日完整替换。"""
    result = fetch_board_snapshot(
        trade_date=trade_date, sections=["lhb"], lhb_adapters=lhb_adapters,
        lhb_history=history, lhb_staging_rows=staging_rows,
        lhb_fallback=lhb_fallback if history else None,
    )
    if result.get("sections") is None:
        return result  # 参数级 error 契约原样上抛
    return {
        "tool": "fetch_lhb_day",
        "status": result["status"],
        "trade_date": result["trade_date"],
        "lhb": result["sections"]["lhb"],
        "failed_sections": result["failed_sections"],
    }


def _lhb_staging_rows(
    start: str, end: str, adapters: Sequence[BoardAdapter], lhb_fallback=None,
    existing_rows: dict[str, list[dict]] | None = None,
) -> tuple[list[dict], dict | None]:
    rows: list[dict] = []
    d = start
    while d <= end:
        if is_trading_day(d):
            if existing_rows is not None and d in existing_rows:
                rows.extend(existing_rows[d])
                d = add_days(d, 1)
                continue
            try:
                result = fetch_section_with_fallback(adapters, "lhb", d)
            except AllSourcesFailed as e:
                if lhb_fallback is not None:
                    result = _lhb_all_markets_failure_result(e)
                else:
                    return [], {
                        "error": f"统计前置抓取失败 {d}: {e}",
                        "error_kind": aggregate_error_kind(e.attempted),
                        "detail": summarize_attempted(e.attempted),
                    }
            data = result["data"]
            if lhb_fallback is not None:
                data = _merge_lhb_basic_fallback(
                    data, result["source"], lhb_fallback, d
                )
            if data.get("errors"):
                message = "; ".join(data["errors"])
                return [], {
                    "error": f"统计前置抓取不完整 {d}: {message}",
                    "error_kind": "partial_error", "detail": message,
                }
            rows.extend(data["lhb_basic"])
        d = add_days(d, 1)
    return rows, None


def fetch_lhb_backfill(
    start: str,
    end: str,
    lhb_adapters: Sequence[BoardAdapter] | None = None,
    fallback: str | None = None,
    lhb_fallback=None,
    force: bool = False,
) -> dict:
    """区间历史回溯：逐交易日抓取，单日失败不中断；逐日结果+汇总台账由
    fetch_board_snapshot 内联记录（本层不重复落账）。"""
    tool = "fetch_lhb_backfill"
    params: dict = {"start": start, "end": end}
    if force:
        params["force"] = True
    try:
        start_d, end_d = normalize_date(start), normalize_date(end)
    except ValueError as e:
        return _error(tool, params, str(e))
    if start_d > end_d:
        return _error(tool, params, f"start({start_d}) 晚于 end({end_d})")
    if fallback not in (None, "ths"):
        return _error(tool, params, f"未知 fallback: {fallback}（可选: ths）")
    if fallback == "ths":
        params["fallback"] = "ths"
        if lhb_fallback is None:
            lhb_fallback = ThsLhbAdapter()
    other_markets_fallback = lhb_fallback
    adapters: Sequence[BoardAdapter]
    if lhb_adapters is None:
        adapters = default_lhb_history_adapters(lhb_fallback or ThsLhbAdapter())
    else:
        adapters = lhb_adapters
    days = []
    d = start_d
    while d <= end_d:
        if is_trading_day(d):
            days.append(d)
        d = add_days(d, 1)
    if not days:
        return _error(tool, params, f"区间 {start_d}~{end_d} 内无交易日")
    lookback_start = add_days(start_d, -LHB_STATISTIC_WINDOW_DAYS)
    lookback_end = add_days(start_d, -1)
    conn, db_error = connect()
    if db_error:
        return _error(tool, params, db_error)
    try:
        reusable_rows, official_days = _existing_lhb_inputs(
            conn, lookback_start, end_d
        )
        official_statistic = {
            d: len(select_lhb_statistic(conn, d)) for d in official_days
        }
    finally:
        conn.close()
    staging_rows, staging_error = _lhb_staging_rows(
        lookback_start, lookback_end, adapters,
        lhb_fallback=other_markets_fallback, existing_rows=reusable_rows,
    )
    if staging_error:
        conn, db_error = connect()
        if not db_error:
            try:
                record_collection(
                    conn, trade_date=start_d, section="lhb", status="error",
                    error_kind=staging_error["error_kind"],
                    detail=staging_error["detail"],
                )
            finally:
                conn.close()
        return {
            "tool": tool, "status": "error", "params": params,
            "error": staging_error["error"],
            "error_kind": staging_error["error_kind"], "results": [],
        }
    total = len(days)
    results = []
    last_logged = monotonic()
    for i, d in enumerate(days, 1):
        if not force and d in official_days:
            results.append({
                "trade_date": d,
                "status": "ok",
                "rows": len(reusable_rows[d]) + official_statistic[d],
                "source": official_days[d].get("source"),
                "empty_kind": (
                    "no_data_for_date" if not reusable_rows[d] else None
                ),
                "error_kind": None,
                "error": None,
                "partial_error": None,
                "preserved": True,
                "source_detail": {"preserved_official": True},
            })
            now_m = monotonic()
            if _progress_due(i, total, last_logged, now_m):
                log.info("lhb 回溯进度 %d/%d（%s，保留官方）", i, total, d)
                last_logged = now_m
            continue
        r = fetch_lhb_day(
            d, lhb_adapters=adapters, history=True, staging_rows=staging_rows,
            lhb_fallback=other_markets_fallback,
        )
        lhb = r.get("lhb") or {}
        entry = {
            "trade_date": r.get("trade_date", d),
            "status": lhb.get("status", "error"),
            "rows": lhb.get("rows", 0),
            "source": lhb.get("source"),
            "empty_kind": lhb.get("empty_kind"),
            "error_kind": lhb.get("error_kind"),
            "error": lhb.get("error"),
            "partial_error": lhb.get("partial_error"),
        }
        if lhb.get("source_detail"):
            entry["source_detail"] = lhb["source_detail"]
        results.append(entry)
        now_m = monotonic()
        if _progress_due(i, total, last_logged, now_m):
            log.info("lhb 回溯进度 %d/%d（%s）", i, total, d)
            last_logged = now_m
    failed = [r for r in results if r["status"] != "ok"]
    status = aggregate_batch_status(total, len(failed))
    return {
        "tool": tool,
        "status": status,
        "params": {**params, "start": start_d, "end": end_d},
        "statistic_lookback": {
            "start": lookback_start, "days": LHB_STATISTIC_WINDOW_DAYS,
        },
        "total_days": total,
        "succeeded": total - len(failed),
        "failed": len(failed),
        "empty_days": sum(1 for r in results if r.get("empty_kind")),
        "results": results,
    }


def _compare_one(code, ex, si, seat_net_wan):
    """单股对拍：全日成交额（双方；累计类情形跳过——上游口径不同）+ 席位净买额。"""
    entry = {
        "stock_code": code,
        "exchange": ex,
        "sina": si,
        "diffs": [],
    }
    if ex is None or si is None:
        entry["status"] = "single_source"
        return entry
    # 异常期间/三日累计类情形：交易所 day_amount 为单日成交金额，新浪成交额
    # 列为期间累计（实测 ~3 倍差），口径不同不比，note 标注
    if "累计" in (ex.get("lhb_reason") or ""):
        entry["day_amount_note"] = "累计类情形：交易所为单日额、新浪为期间累计，跳过比对"
    else:
        ex_amt, si_amt = ex.get("day_amount"), si.get("amount_wan")
        if ex_amt is not None and si_amt is not None and not _within_tolerance(
            ex_amt, si_amt, *_AMOUNT_TOL
        ):
            entry["diffs"].append(
                {"field": "day_amount", "exchange": ex_amt, "sina": si_amt}
            )
    if seat_net_wan is not None and ex.get("net_buy_amount") is not None:
        if not _within_tolerance(ex["net_buy_amount"], seat_net_wan, *_AMOUNT_TOL):
            entry["diffs"].append(
                {
                    "field": "net_buy_amount",
                    "exchange": ex["net_buy_amount"],
                    "sina_seat_net": seat_net_wan,
                }
            )
    entry["status"] = "mismatch" if entry["diffs"] else "ok"
    return entry


def _compare_statistic_snapshot(trade_date: str, sina) -> tuple[dict, int]:
    """statistic 校验位对拍（不落库）：库内 lhb_basic 自算快照 vs 新浪 ggtj。

    issue #114 定稿：statistic 由交易所官方数据（lhb_basic）派生自算，新浪
    ggtj 回归 `--check` 对拍校验位。库侧只读（连库失败只记该侧，不判整体
    失败）；ggtj 缺失（旧版对拍客户端未实现）记 unsupported。返回
    (报告, 差异股数)；集合差信息级（窗口锚点不同会制造边界差），不算失败。
    """
    report: dict = {
        "compared_fields": list(_STATISTIC_PARITY_FIELDS),
        "note": _STATISTIC_NOTE,
    }
    db_by_code: dict[str, dict] = {}
    conn, err = connect()
    if err:
        report["db"] = {"status": "error", "error": err}
    else:
        try:
            for row in select_lhb_statistic(conn, trade_date):
                db_by_code[row["stock_code"]] = row
            report["db"] = {"status": "ok", "row_count": len(db_by_code)}
        except Exception as e:  # noqa: BLE001 - 对拍库侧失败只记该侧
            report["db"] = {"status": "error", "error": str(e)}
            db_by_code = {}
        finally:
            conn.close()
    si_by_code: dict[str, dict] = {}
    ggtj_fn = getattr(sina, "ggtj", None)
    if ggtj_fn is None:
        report["sina_ggtj"] = {"status": "unsupported",
                               "note": "对拍客户端未实现 ggtj（旧版 fake 客户端）"}
    else:
        try:
            for row in ggtj_fn():
                si_by_code[row["stock_code"]] = row
            report["sina_ggtj"] = {"status": "ok", "row_count": len(si_by_code)}
        except Exception as e:  # noqa: BLE001 - 对拍单源失败只记该源
            report["sina_ggtj"] = {"status": "error", "error": str(e)}
    db_codes, si_codes = set(db_by_code), set(si_by_code)
    report["stock_set_diff"] = {
        "db_only": sorted(db_codes - si_codes),
        "sina_only": sorted(si_codes - db_codes),
    }
    results = []
    mismatched = 0
    for code in sorted(db_codes & si_codes):
        db_r, si_r = db_by_code[code], si_by_code[code]
        entry: dict = {"stock_code": code, "db": db_r, "sina": si_r, "diffs": []}
        for field, (kind, tol) in _STATISTIC_PARITY_FIELDS.items():
            dv, sv = db_r.get(field), si_r.get(field)
            if dv is None or sv is None:
                continue
            if not _within_tolerance(float(dv), float(sv), kind, tol):
                entry["diffs"].append({"field": field, "db": dv, "sina": sv})
        entry["status"] = "mismatch" if entry["diffs"] else "ok"
        if entry["diffs"]:
            mismatched += 1
        results.append(entry)
    report["results"] = results
    report["mismatch_count"] = mismatched
    report["single_source_count"] = len(db_codes ^ si_codes)
    return report, mismatched


def compare_lhb_sources(
    trade_date: str | None = None,
    exchange: ExchangeLhbAdapter | None = None,
    sina: SinaLhbClient | None = None,
) -> dict:
    """新浪互补对拍（不落库）：交易所官方 vs 新浪转载面。

    逐股比对股票集合（双向差集报 info 级）、全日成交额（双方披露口径一致，
    rel 0.005）、席位净买额（新浪 seat JSONP 去重后 Σnet，可得时）；另有
    statistic 校验位对拍（库内自算快照 vs 新浪 ggtj，见
    _compare_statistic_snapshot）。对拍失败/无数据只记该侧，不中断另一侧；
    两侧 basic 均无数据才 error。
    """
    tool = "compare_lhb_sources"
    params = {"trade_date": trade_date}
    try:
        effective = normalize_date(trade_date) if trade_date else today_str()
    except ValueError as e:
        return _error(tool, params, str(e))
    if exchange is None:
        exchange = ExchangeLhbAdapter()
    if sina is None:
        sina = SinaLhbClient()

    stat_report, stat_mismatched = _compare_statistic_snapshot(effective, sina)

    ex_entry: dict = {"source": exchange.name, "status": None, "basic_count": 0}
    ex_by_code: dict[str, dict] = {}
    ex_rows_by_code: dict[str, int] = {}
    try:
        data = exchange.fetch_lhb(effective)
        for row in data.get("lhb_basic") or []:
            ex_rows_by_code[row["stock_code"]] = ex_rows_by_code.get(row["stock_code"], 0) + 1
            ex_by_code.setdefault(row["stock_code"], row)
        ex_entry["basic_count"] = len(ex_by_code)
        if data.get("errors"):
            ex_entry["partial_error"] = "; ".join(data["errors"])
        ex_entry["status"] = "ok"
    except Exception as e:  # noqa: BLE001 - 对拍单源失败只记该源
        ex_entry = {"source": exchange.name, "status": "error", "error": str(e)}

    si_entry: dict = {"source": sina.name, "status": None, "row_count": 0}
    si_by_code: dict[str, dict] = {}
    si_rows_by_code: dict[str, int] = {}
    try:
        for row in sina.daily_detail(effective):
            si_rows_by_code[row["stock_code"]] = si_rows_by_code.get(row["stock_code"], 0) + 1
            si_by_code.setdefault(row["stock_code"], row)
        si_entry["row_count"] = len(si_by_code)
        si_entry["status"] = "ok"
    except Exception as e:  # noqa: BLE001
        si_entry = {"source": sina.name, "status": "error", "error": str(e)}

    if ex_entry["status"] != "ok" and si_entry["status"] != "ok":
        return {
            "tool": tool,
            "status": "error",
            "params": {"trade_date": effective},
            "error": "交易所与新浪两侧均抓取失败",
            "exchange": ex_entry,
            "sina": si_entry,
            "statistic": stat_report,
        }

    ex_codes, si_codes = set(ex_by_code), set(si_by_code)
    only_ex = sorted(ex_codes - si_codes)
    only_si = sorted(si_codes - ex_codes)
    results = []
    mismatched = 0
    for code in sorted(ex_codes & si_codes):
        seat_net_wan = None
        si = si_by_code[code]
        ex = ex_by_code[code]
        if si.get("seat_type") and sina is not None and si_entry["status"] == "ok":
            try:
                seats = sina.seat_detail(code, effective, si["seat_type"])
                seat_net_wan = seat_net(seats)
            except Exception as e:  # noqa: BLE001 - 单股席位缺失只记 note
                si = {**si, "seat_note": f"席位抓取失败: {e}"}
        entry = _compare_one(code, ex, si, seat_net_wan)
        # 多榜单同股：两侧各自按首行归并，席位集可能属于不同榜单——
        # 展示双方原值但不计差异（结构性不可比，非数据错误）
        if ex_rows_by_code.get(code, 0) > 1 or si_rows_by_code.get(code, 0) > 1:
            entry = {
                "stock_code": code,
                "exchange": ex,
                "sina": si,
                "status": "ok",
                "note": "多榜单同股：双方各按首行归并，席位可能属不同榜单，不计差异",
            }
        results.append(entry)
        if entry["status"] == "mismatch":
            mismatched += 1
    single = sum(1 for r in results if r["status"] == "single_source")
    status = "mismatch" if (mismatched or stat_mismatched) else "ok"
    return {
        "tool": tool,
        "status": status,
        "params": {"trade_date": effective},
        "compared_fields": list(_PARITY_FIELDS),
        "exchange": ex_entry,
        "sina": si_entry,
        "statistic": stat_report,
        "stock_set_diff": {
            "exchange_only": only_ex,
            "sina_only": only_si,
            "note": "指标文案口径因源而异（交易所披露原文 vs 新浪归类），不逐条比对",
        },
        "results": results,
        "mismatch_count": mismatched,
        "single_source_count": single,
        "note": "对拍不落库；净买额比对依赖新浪席位 JSONP（买/卖各五，万元）",
    }
