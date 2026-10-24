"""fetch/query 能力面核心：fetch_board_snapshot 盘面快照落库，query_board_data 库内查询。

工具函数层（server.py）只做薄包装；本层是测试接缝：注入 fake 适配器 +
真实 PG 测试库（见 tests/）。盘面快照契约（issue #5）：按 section
（indices/boards/zt_pool/strong_stocks/lhb）独立 fallback 重试、独立成败报告，
各表按业务键 upsert，重复执行幂等。核心 section fail-fast（issue #47，
ADR 0007）：CORE_SECTIONS 显式声明核心 section 集，任一核心 section 因
upstream_unreachable（连接级故障）失败 → 工具整体 error，响应携带
per-section 明细（成败/已落库行数/error_kind/逐源 attempted_sources）；
fetch_failed（非连接级）与 no_data_for_date（上游正常响应空结果）不触发。
中途失败不回滚：per-section 独立 commit，已落库 section 保留，
调用方可用 sections 参数只重试失败分段（幂等）。lhb 永不进核心集：
盘中 lhb_basic 为空返回 rows:0 + note + empty_kind=no_data_for_date
（龙虎榜盘后发布是正常语义），lhb 抓取失败按三态结构化返回
（status error + error_kind + 逐源明细），与"未发布"可区分；
lhb 子项部分失败落已得数据并报 partial_error。

indices 段白名单定点补漏（issue #49）：主源 efinance「沪深系列指数」口径系统性
排除科创板系列指数（缺 000688），备选 akshare 含 000688 但主源成功时轮不到。
indices 段抓取后对照 MAJOR_INDICES 白名单（复用 tools_init 清单，单一事实来源）
检查覆盖，缺谁按单 code 补抓补落库（fetch_index_spot_with_fallback）。补漏失败
不静默也不触发核心 section fail-fast——section 维持 ok，失败按三态分类进
patch failed 明细（复用 ADR 0007 命名：upstream_unreachable 带 retry_advice /
fetch_failed / 全源口径不含该指数 no_data_for_date）。

服务端防线（issue #78，ADR 0007 补记）：boards/indices 为实时语义 section
（REALTIME_SECTIONS），对历史日期调用返回 section 级 unsupported
（detail=realtime_section_no_backfill）零落库；indices 段落库前校验行级日期
==请求日期（不符 detail=date_mismatch 零落库）。防线拒收同样落采集台账
（issue #77：被拒的污染尝试最值得审计）。

第四态 planned_absent（issue #113，spec #111/#102，ADR 0007 补记）：已定方向
待接线的 section 在 PLANNED_ABSENT_SECTIONS 登记后以第四态占位——循环内最先
判定，不抓取、零落库、不计 failed_sections、不触发核心 section fail-fast、
全占位时整体仍 ok；每运行落一条 planned_absent 占位台账（历史模式就绪归因
由此区分"计划缺席"与"未采集"）。永久弃用面不占位：直接从 ALL_SECTIONS /
调度采集清单删除（如概念维度资金流字段，见 daily-review skill）。

zt_pool/strong_stocks 承载切换（issue #120，spec #111，ADR 0012）——仅生产
路径（调用方未注入适配器）生效，注入 fake 的测试走原 fetch 路径：

- zt_pool → 同花顺涨停揭秘 ThsAdapter 单源：台账"每日一次"守卫（当日已
  ok/empty 即跳过上游）+ fetch_section_with_fallback(max_retries=0)（低频
  纪律：不进入同源重试）+ 封禁探测命中按 upstream_unreachable 语义上报
  （THS IP 级封禁视同连接级故障，触发核心 section 中止线）。单池失败、
  他池成功时拿到的池照常落库并以 partial_error 报告（lhb 部分失败同构）。
- strong_stocks → 库内自算（tools_strong）：stock_daily 60 日新高 ∪
  zt_pool 近 10 日多次涨停；输入缺失（当日无日线）按 no_data_for_date
  正常语义跳过，非故障不中止。
"""

import logging
from datetime import datetime, timezone
from typing import Sequence

from .adapters import BoardAdapter, default_adapters
from .dates import normalize_date, today_str
from .db import connect
from .fetch_chain import (
    UPSTREAM_RETRY_ADVICE,
    AllSourcesFailed,
    aggregate_error_kind,
    aggregate_patch_error_kind,
    fetch_index_spot_with_fallback,
    fetch_section_with_fallback,
)
from .ledger import already_collected, record_collection, summarize_attempted
from .output import error as _error
from .repository import (
    BOARD_QUERY_TABLES,
    BOARD_SECTION_TABLES,
    select_board_rows,
    select_latest_board_date,
    upsert_board_rows,
)
from .tools_init import MAJOR_INDICES

log = logging.getLogger(__name__)

ALL_SECTIONS = ("indices", "boards", "zt_pool", "strong_stocks", "lhb")

# 核心 section 集（issue #47）：任一核心 section 因 upstream_unreachable 失败
# 即整体 error。lhb 永不进核心集（盘中"未发布"是常态）；新增 section 默认非核心，
# 入核心集需显式更新本常量。
CORE_SECTIONS = ("indices", "boards", "zt_pool", "strong_stocks")

# 实时语义 section（issue #78，ADR 0007 补记）：上游接口只有当日实时口径，
# 历史日期回补不支持——对历史日期调用返回 unsupported + detail，零落库。
REALTIME_SECTIONS = ("indices", "boards")
REALTIME_NO_BACKFILL = "realtime_section_no_backfill"

# 第四态登记处（issue #113，spec #111/#102）：已定方向待接线的
# section → 缺席原因（决策依据）。在册 section 占位不抓：响应 status=
# "planned_absent"，落 planned_absent 占位台账，不计失败、不进异常报告。
# 接线完成即从本字典删除该 section 即恢复采集；测试用 monkeypatch 替换本字典。
PLANNED_ABSENT_SECTIONS: dict[str, str] = {}

_LHB_EMPTY_NOTE = "当日无龙虎榜数据（龙虎榜盘后发布）"

# THS 封禁恢复建议（实测恢复 >5 分钟，issue #120 调研）：覆盖通用重试建议
_THS_RETRY_ADVICE = (
    "同花顺 IP 级封禁恢复时间 >5 分钟（2026-10-20 实测），今日勿再重试，"
    "等次日采集窗口；可用 qstock-scheduler limitpool --probe 探测解封状态"
)


def _zt_pool_section(conn, trade_date: str, started) -> dict:
    """zt_pool 段（issue #120）：同花顺涨停揭秘低频纪律抓取 + 落库 + 落账。

    每日一次守卫：当日台账已有 ok/empty 行即跳过上游（THS 风控极敏感，
    重复执行不重复消耗配额）；error 不在守卫之列（失败允许修复后当日重跑）。
    封禁探测命中（FetchError kind=connection）按 upstream_unreachable 上报，
    触发核心 section 中止线（spec #111"新源故障含 THS 风控仍触发中止线"）。
    """
    from .adapters.ths_adapter import ThsAdapter

    if already_collected(conn, trade_date, "zt_pool"):
        n = len(select_board_rows(conn, "zt_pool", "trade_date", "stock_code",
                                  trade_date))
        record_collection(
            conn, trade_date=trade_date, section="zt_pool", status="ok", rows=n,
            detail="skip: 每日一次纪律（当日已采集，跳过上游请求）",
            started_at=started,
        )
        return {
            "rows": n,
            "status": "ok",
            "note": "当日已采集（每日一次纪律），跳过上游请求",
        }
    adapter = ThsAdapter()
    try:
        r = fetch_section_with_fallback([adapter], "zt_pool", trade_date,
                                        max_retries=0)
    except AllSourcesFailed as e:
        kind = aggregate_error_kind(e.attempted)
        entry = {
            "rows": 0,
            "status": "error",
            "error": str(e),
            "error_kind": kind,
            "attempted_sources": e.attempted,
        }
        if kind == "upstream_unreachable":
            entry["retry_advice"] = _THS_RETRY_ADVICE
        record_collection(
            conn, trade_date=trade_date, section="zt_pool", status="error",
            error_kind=kind, detail=summarize_attempted(e.attempted),
            started_at=started,
        )
        return entry
    data = r["data"]
    res = {
        "rows": _upsert_section(conn, "zt_pool", data, trade_date),
        "source": r["source"],
        "status": "ok",
    }
    partial = getattr(adapter, "pool_errors", None)
    if partial:
        # 单池失败他池成功：拿到的池照常落库，失败原因必须报告（lhb 同构）
        res["partial_error"] = "; ".join(f"{k}: {v}" for k, v in partial.items())
    if not data:
        res["empty_kind"] = "no_data_for_date"
    record_collection(
        conn,
        trade_date=trade_date,
        section="zt_pool",
        status="ok" if res["rows"] > 0 else "empty",
        source=r["source"],
        rows=res["rows"],
        error_kind=None if res["rows"] > 0 else "no_data_for_date",
        detail="; ".join(f"{k}: {v}" for k, v in partial.items())
        if partial
        else summarize_attempted(r["attempted_sources"]),
        started_at=started,
    )
    return res


def _strong_stocks_section(conn, trade_date: str, started) -> dict:
    """strong_stocks 段（issue #120）：库内自算（tools_strong）+ 落库 + 落账。

    自算输入缺失（stock_daily 当日无数据）按 no_data_for_date 正常语义跳过：
    非故障、不中止——先经 daily 入口补当日日线后重跑本段即可。
    """
    from .tools_strong import compute_strong_rows

    rows = compute_strong_rows(conn, trade_date)
    if rows is None:
        note = ("stock_daily 当日无数据，跳过自算"
                "（先经 daily 入口补当日日线后重跑）")
        record_collection(
            conn, trade_date=trade_date, section="strong_stocks",
            status="empty", source="selfcalc", rows=0,
            error_kind="no_data_for_date",
            detail="selfcalc_skip: stock_daily 无当日行", started_at=started,
        )
        return {
            "rows": 0,
            "status": "ok",
            "source": "selfcalc",
            "empty_kind": "no_data_for_date",
            "note": note,
        }
    n = _upsert_section(conn, "strong_stocks", rows, trade_date)
    record_collection(
        conn, trade_date=trade_date, section="strong_stocks", status="ok",
        source="selfcalc", rows=n,
        detail="selfcalc: stock_daily 60日新高 ∪ zt_pool 近10日多次涨停",
        started_at=started,
    )
    res: dict = {"rows": n, "source": "selfcalc", "status": "ok"}
    if not rows:
        res["empty_kind"] = "no_data_for_date"
    return res



def _parse_sections(sections) -> list[str]:
    """None → 全部；CSV 字符串或列表 → 子集。未知 section 抛 ValueError。"""
    if sections is None:
        return list(ALL_SECTIONS)
    if isinstance(sections, str):
        selected = [s.strip() for s in sections.split(",") if s.strip()]
    else:
        selected = list(sections)
    unknown = [s for s in selected if s not in ALL_SECTIONS]
    if unknown:
        raise ValueError(f"未知 section: {unknown}（可选: {', '.join(ALL_SECTIONS)}）")
    if not selected:
        raise ValueError(f"sections 为空（可选: {', '.join(ALL_SECTIONS)}）")
    return selected


def _upsert_section(conn, section: str, data, trade_date: str) -> int:
    """把一个 section 的抓取结果落库，返回总行数。"""
    total = 0
    for table, date_col, key_cols, fields in BOARD_SECTION_TABLES[section]:
        rows = data[table] if section == "lhb" else data
        total += upsert_board_rows(
            conn, table, date_col, key_cols, fields, trade_date, rows
        )
    return total


def _patch_major_indices(
    adapters: Sequence[BoardAdapter], data: list[dict]
) -> tuple[list[dict], dict | None]:
    """indices 段白名单定点补漏（issue #49）：对照 MAJOR_INDICES 检查覆盖，
    缺者按单 code 补抓，返回 (含补抓行的行列表, patch 报告)。

    白名单全命中返回 (原数据, None)——零补抓请求。补抓失败不抛错（不拖垮
    section、不触发 fail-fast），按三态分类记入报告 failed 明细并 log；
    全源口径不含该指数（empty）归 no_data_for_date——正常语义，但覆盖目标
    未达成，同样列入 failed 明细。
    """
    present = {row.get("index_code") for row in data}
    missing = [code for code, _ in MAJOR_INDICES if code not in present]
    if not missing:
        return data, None
    rows = list(data)
    patched = []
    failed = {}
    for code in missing:
        try:
            r = fetch_index_spot_with_fallback(adapters, code)
        except AllSourcesFailed as e:
            kind = aggregate_patch_error_kind(e.attempted)
            entry = {
                "error": str(e),
                "error_kind": kind,
                "attempted_sources": e.attempted,
            }
            if kind == "upstream_unreachable":
                entry["retry_advice"] = UPSTREAM_RETRY_ADVICE
            failed[code] = entry
            log.warning("indices 段白名单补漏失败 %s: %s", code, e)
            continue
        rows += r["rows"]
        patched.append({"index_code": code, "source": r["source"]})
    report: dict = {"patched": patched}
    if failed:
        report["failed"] = failed
    return rows, report


def fetch_board_snapshot(
    trade_date: str | None = None,
    sections: str | Sequence[str] | None = None,
    adapters: Sequence[BoardAdapter] | None = None,
) -> dict:
    """抓取盘面快照并落库：各 section 独立 fallback 重试、独立成败报告。"""
    tool = "fetch_board_snapshot"
    params = {"trade_date": trade_date, "sections": sections}
    if trade_date is None:
        effective = today_str()
    else:
        try:
            effective = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    try:
        selected = _parse_sections(sections)
    except ValueError as e:
        return _error(tool, params, str(e))
    injected = adapters is not None  # 测试注入走原 fetch 路径；生产路径见下
    if adapters is None:
        adapters = default_adapters()
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    results = {}
    try:
        today = today_str()
        for name in selected:
            started = datetime.now(timezone.utc)
            if name in PLANNED_ABSENT_SECTIONS:
                # 第四态占位（issue #113）：已定方向待接线，不抓取零落库、不计
                # 失败；占位落账供历史模式就绪归因（计划缺席 ≠ 未采集 ≠ 故障）
                reason = PLANNED_ABSENT_SECTIONS[name]
                record_collection(
                    conn,
                    trade_date=effective,
                    section=name,
                    status="planned_absent",
                    rows=0,
                    detail=f"planned_absent: {reason}",
                    started_at=started,
                )
                results[name] = {
                    "rows": 0,
                    "status": "planned_absent",
                    "note": f"计划缺席（已定方向待接线）：{reason}",
                }
                continue
            if effective < today and name in REALTIME_SECTIONS:
                # 服务端防线（issue #78）：实时语义 section 不可回补历史日期，零落库
                # 拒收也落账（issue #77）：被拒绝的历史回补尝试最值得审计
                record_collection(
                    conn,
                    trade_date=effective,
                    section=name,
                    status="error",
                    error_kind="unsupported",
                    detail=f"guard_reject: {name} 为实时语义 section，拒绝历史日期回补",
                    started_at=started,
                )
                results[name] = {
                    "rows": 0,
                    "status": "error",
                    "error": f"{name} 为实时语义 section，不支持历史日期回补（{effective}）",
                    "error_kind": "unsupported",
                    "detail": REALTIME_NO_BACKFILL,
                }
                continue
            if name == "zt_pool" and not injected:
                # 生产路径：THS 涨停揭秘低频纪律抓取（issue #120）
                results[name] = _zt_pool_section(conn, effective, started)
                continue
            if name == "strong_stocks" and not injected:
                # 生产路径：库内自算（issue #120）
                results[name] = _strong_stocks_section(conn, effective, started)
                continue
            try:
                r = fetch_section_with_fallback(adapters, name, effective)
            except AllSourcesFailed as e:
                kind = aggregate_error_kind(e.attempted)
                entry = {
                    "rows": 0,
                    "status": "error",
                    "error": str(e),
                    "error_kind": kind,
                    "attempted_sources": e.attempted,
                }
                if kind == "upstream_unreachable":
                    entry["retry_advice"] = UPSTREAM_RETRY_ADVICE
                results[name] = entry
                record_collection(
                    conn,
                    trade_date=effective,
                    section=name,
                    status="error",
                    error_kind=kind,
                    detail=summarize_attempted(e.attempted),
                    started_at=started,
                )
                continue
            data = r["data"]
            patch = None
            if name == "indices":
                data, patch = _patch_major_indices(adapters, data)
                # 行级日期校验（issue #78）：行级日期与请求日期不符即拒收零落库，
                # 防止上游返回陈旧交易日数据被静默计入请求日（efinance 行级"最新交易日"）
                mismatched = sorted(
                    {
                        row["trade_date"]
                        for row in data
                        if row.get("trade_date") and row["trade_date"] != effective
                    }
                )
                if mismatched:
                    # 拒收也落账（issue #77）：被拒绝的污染尝试最值得审计
                    record_collection(
                        conn,
                        trade_date=effective,
                        section=name,
                        status="error",
                        error_kind="date_mismatch",
                        detail=f"guard_reject: indices 行级日期 {mismatched} != {effective}",
                        started_at=started,
                    )
                    results[name] = {
                        "rows": 0,
                        "status": "error",
                        "error": f"indices 行级日期与请求日期不符: {mismatched} != {effective}",
                        "detail": "date_mismatch",
                    }
                    continue
            res = {
                "rows": _upsert_section(conn, name, data, effective),
                "source": r["source"],
                "status": "ok",
            }
            if patch:
                res["patch"] = patch
            if r["attempted_sources"]:
                res["attempted_sources"] = r["attempted_sources"]
            if name == "lhb":
                if r["data"].get("errors"):
                    # 子项部分失败：拿到什么落什么，但失败原因必须报告
                    res["partial_error"] = "; ".join(r["data"]["errors"])
                if not r["data"]["lhb_basic"]:
                    res["empty_kind"] = "no_data_for_date"
                    res["note"] = _LHB_EMPTY_NOTE
            elif not data:
                res["empty_kind"] = "no_data_for_date"
            record_collection(
                conn,
                trade_date=effective,
                section=name,
                status="ok" if res["rows"] > 0 else "empty",
                source=r["source"],
                rows=res["rows"],
                error_kind=None if res["rows"] > 0 else "no_data_for_date",
                detail=summarize_attempted(r["attempted_sources"]),
                started_at=started,
            )
            results[name] = res
    finally:
        conn.close()
    failed = [
        k for k, v in results.items() if v["status"] not in ("ok", "planned_absent")
    ]
    payload = {
        "tool": tool,
        "params": params,
        "trade_date": effective,
        "sections": results,
        "failed_sections": failed,
    }
    unreachable_core = [
        k
        for k in failed
        if k in CORE_SECTIONS
        and results[k].get("error_kind") == "upstream_unreachable"
    ]
    if unreachable_core:
        return {
            "status": "error",
            "error": f"核心 section 上游不可达: {', '.join(unreachable_core)}",
            "error_kind": "upstream_unreachable",
            "retry_advice": UPSTREAM_RETRY_ADVICE,
            **payload,
        }
    if len(failed) == len(results):
        if all(results[k].get("error_kind") == "unsupported" for k in failed):
            # 全部为防线拒收（实时 section 历史回补不支持，ADR 0007 补记）
            kind = "unsupported"
        else:
            kind = (
                "upstream_unreachable"
                if any(
                    results[k].get("error_kind") == "upstream_unreachable" for k in failed
                )
                else "fetch_failed"
            )
        out = {
            "status": "error",
            "error": f"全部 section 抓取失败: {', '.join(failed)}",
            "error_kind": kind,
            **payload,
        }
        if kind == "upstream_unreachable":
            out["retry_advice"] = UPSTREAM_RETRY_ADVICE
        return out
    return {"status": "ok", **payload}


def query_board_data(
    table: str,
    trade_date: str | None = None,
    code: str | None = None,
) -> dict:
    """库内盘面数据查询：按表/日期/代码过滤；日期缺省取该表库内最新日期。

    lhb_yyb_* 两表的日期列为 fetch_date（抓取日语义），参数仍叫 trade_date。
    """
    tool = "query_board_data"
    params = {"table": table, "trade_date": trade_date, "code": code}
    if table not in BOARD_QUERY_TABLES:
        return _error(
            tool,
            params,
            f"未知表: {table}（可选: {', '.join(BOARD_QUERY_TABLES)}）",
        )
    if trade_date is not None:
        try:
            trade_date = normalize_date(trade_date)
        except ValueError as e:
            return _error(tool, params, str(e))
    date_col, code_col = BOARD_QUERY_TABLES[table]
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        if trade_date is None:
            trade_date = select_latest_board_date(conn, table, date_col)
        rows = (
            select_board_rows(conn, table, date_col, code_col, trade_date, code)
            if trade_date is not None
            else []
        )
    finally:
        conn.close()
    return {
        "status": "ok",
        "tool": tool,
        "params": params,
        "table": table,
        "trade_date": trade_date,
        "count": len(rows),
        "rows": rows,
    }
