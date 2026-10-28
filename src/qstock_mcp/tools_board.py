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
单日实时 lhb 子项部分失败落已得数据并报 partial_error；历史回溯则保留旧日。

indices 段白名单定点补漏（issue #49）：生产链腾讯批量报价口径已全量覆盖
7 大指数白名单（含科创50 000688），全命中时零补抓请求；补漏机制为口径性
缺失的兜底保留。indices 段抓取后对照 MAJOR_INDICES 白名单（复用 tools_init
清单，单一事实来源）检查覆盖，缺谁按单 code 补抓补落库
（fetch_index_spot_with_fallback）。补漏失败不静默也不触发核心 section
fail-fast——section 维持 ok，失败按三态分类进 patch failed 明细（复用
ADR 0007 命名：upstream_unreachable 带 retry_advice / fetch_failed /
全源口径不含该指数 no_data_for_date）。

承载源（issue #117/#121，spec #111）：indices section 由腾讯批量报价承载
（唯一覆盖量比字段、含 000688），fallback 链 腾讯 → 新浪
（default_index_spot_adapters）；东财口径已随 #121 摘除，白名单补漏机制保留
（腾讯 7 指数全命中时零补抓请求）。注入适配器时（测试）全 section 沿用注入。

服务端防线（issue #78，ADR 0007 补记）：boards/indices 为实时语义 section
（REALTIME_SECTIONS），对历史日期调用返回 section 级 unsupported
（detail=realtime_section_no_backfill）零落库；indices 段落库前校验行级日期
==请求日期（不符 detail=date_mismatch 零落库）。防线拒收同样落采集台账
（issue #77：被拒的污染尝试最值得审计）。

boards 行业段申万承接（issue #118，spec #111）：boards section 的 fallback
链独立解析为申万官网单源（default_boards_adapters，一级 31 + 二级 124 双级
实时快照，board_type=industry_l1/industry_l2），东财 industry/concept 口径
退役、不再入链（口径断代不混排）；申万专属能力（板块清单/历史/分析报表/
分类文件）经 SwSResearchAdapter 申万专属协议供调度 CLI 调用。申万不发布的
量比/领涨股/资金流三字段按 #113 第四态语义废弃：PLANNED_ABSENT_BOARD_FIELDS
机器可读声明进 boards 响应与台账 detail，列恒 NULL 不报故障；板块历史回补
（1999 起）走 scheduler backfill --source sw，申万官网故障时复盘降级用库内
自算 query_sw_board_daily（daily-review skill 板块节降级节）。

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

from .adapters import (
    BoardAdapter,
    default_adapters,
    default_boards_adapters,
    default_index_spot_adapters,
    default_lhb_adapters,
)
from .adapters._bse_codes import market_of
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
    recompute_lhb_statistic,
    replace_lhb_basic_day,
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

# 板块段字段级第四态（issue #118，spec #111/#113 语义）：申万官网不发布、
# 按决策永久废弃的 boards 字段 → 废弃原因。申万承接后 market_boards 的该列
# 恒 NULL 是决策性缺席而非数据缺口：响应以 planned_absent_fields 机器可读
# 声明（不误报故障、不进 data_gaps），daily-review 报告不设对应栏目。
# 资金流不在表列（原东财资金接口承载的报表栏目，#113 已从报告删除）。
PLANNED_ABSENT_BOARD_FIELDS: dict[str, str] = {
    "fund_flow": (
        "主力资金净流入：申万官网不发布板块资金流，随东财链路永久弃用"
        "（spec #111/#113，issue #118）；报告不设该栏目、不入 data_gaps"
    ),
    "volume_ratio": "量比：申万官网接口不提供板块量比，永久弃用（issue #118）",
    "leading_stock": (
        "领涨股：申万官网接口不发布板块领涨股，永久弃用；可选增强为"
        "成分×全市场快照派生（spec #111 列为演进项，本期不实施）"
    ),
    "leading_change": "领涨股涨跌幅：随领涨股字段一并弃用（issue #118）",
}

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


_LHB_MARKETS = ("szse", "sse", "bse")


def _lhb_market(stock_code: str) -> str:
    return {"sh": "sse", "sz": "szse", "bj": "bse"}[market_of(stock_code)]


def _merge_lhb_basic_fallback(data: dict, primary_source: str, fallback, trade_date: str) -> dict:
    errors = list(data.get("errors") or [])
    failed_markets = [
        error.split(":", 1)[0] for error in errors
        if error.split(":", 1)[0] in _LHB_MARKETS
    ]
    if not failed_markets:
        failed_markets = [
            market for market in _LHB_MARKETS
            if any(
                error.startswith("lhb_basic:") and f"{market}:" in error
                for error in errors
            )
        ]
    if not failed_markets:
        return data
    merged = dict(data)
    try:
        fallback_rows = data.get("ths_basic_rows")
        if fallback_rows is None:
            fallback_rows = fallback.fetch_lhb_basic(trade_date)
    except Exception as exc:  # noqa: BLE001 - 后备失败保留官方错误并中止当日历史写入
        merged["errors"] = errors + [f"ths: {exc}"]
        return merged
    try:
        mismatched_dates = sorted(
            {
                row.get("trade_date") for row in fallback_rows
                if row.get("trade_date") != trade_date
            }
        )
        if mismatched_dates:
            merged["errors"] = errors + [
                f"ths: 后备行日期不符 {mismatched_dates} != {trade_date}"
            ]
            return merged
        rows = list(data["lhb_basic"])
        keys = {(row["stock_code"], row.get("lhb_reason") or "") for row in rows}
        patched_markets = []
        patched_rows = 0
        for market in failed_markets:
            additions = [
                row for row in fallback_rows
                if _lhb_market(row["stock_code"]) == market
                and row.get("trade_date") == trade_date
            ]
            if not additions:
                continue
            patched_markets.append(market)
            for row in additions:
                key = (row["stock_code"], row.get("lhb_reason") or "")
                if key not in keys:
                    rows.append(row)
                    keys.add(key)
                    patched_rows += 1
    except Exception as exc:  # noqa: BLE001 - 合并异常同样不得污染旧日
        merged["errors"] = errors + [f"ths: 后备合并异常: {exc}"]
        return merged
    merged["lhb_basic"] = rows
    resolved = set(patched_markets)
    merged["errors"] = [
        error for error in errors
        if error.split(":", 1)[0] not in resolved
        and not (
            error.startswith("lhb_basic:")
            and not any(
                f"{market}:" in error and market not in resolved
                for market in _LHB_MARKETS
            )
        )
    ]
    if patched_markets:
        merged["basic_fallback"] = {
            "source": fallback.name,
            "markets": patched_markets,
            "rows": patched_rows,
        }
        merged["primary_source"] = primary_source
    return merged


def _lhb_all_markets_failure_result(exc: AllSourcesFailed) -> dict:
    return {
        "source": exc.attempted[0]["source"] if exc.attempted else "exchange",
        "attempted_sources": exc.attempted,
        "data": {
            "lhb_basic": [],
            "lhb_yyb_capital": [],
            "lhb_yyb_most": [],
            "errors": [f"{market}: {exc}" for market in _LHB_MARKETS],
        },
    }


def fetch_board_snapshot(
    trade_date: str | None = None,
    sections: str | Sequence[str] | None = None,
    adapters: Sequence[BoardAdapter] | None = None,
    lhb_adapters: Sequence[BoardAdapter] | None = None,
    lhb_history: bool = False,
    lhb_staging_rows: list[dict] | None = None,
    lhb_fallback=None,
) -> dict:
    """抓取盘面快照并落库：各 section 独立 fallback 重试、独立成败报告。

    lhb section 专用链（issue #114/#121，spec #111 定稿）：默认走
    default_lhb_adapters()（交易所官方单源，东财 legacy 兜底已摘除），其余
    section 用 adapters。调用方显式传 adapters 而未传 lhb_adapters 时，lhb 随
    adapters（测试 fake 注入的既有接缝保持兼容）；显式传 lhb_adapters 时 lhb 以它为准。
    """
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
    # adapters 为 None 保留到循环内按 section 解析生产链（indices 走腾讯→新浪，
    # issue #117；boards 行业段走申万官网单源链，issue #118；lhb 走交易所官方链，
    # issue #114/#121；zt_pool/strong_stocks 生产路径走 THS 低频纪律与库内自算，
    # issue #120，见循环内分支；其余走 default_adapters——清单链，issue #121
    # 定稿后盘面 section 均已各有专用链，本链仅兜底未来新增 section）；
    # 注入适配器（测试）时全 section 沿用注入
    injected = adapters is not None  # 测试注入走原 fetch 路径；生产路径见下
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
            # 生产链按 section 解析：indices 走腾讯→新浪指数快照链（issue #117），
            # boards 行业段走申万官网单源链（issue #118，申万故障时 daily-review
            # 降级用库内自算 query_sw_board_daily），lhb 走交易所官方单源链
            # （issue #114/#121）；注入适配器（测试）时全 section 沿用注入，显式传
            # lhb_adapters 时 lhb 以它为准
            if name == "zt_pool" and not injected:
                # 生产路径：THS 涨停揭秘低频纪律抓取（issue #120）
                results[name] = _zt_pool_section(conn, effective, started)
                continue
            if name == "strong_stocks" and not injected:
                # 生产路径：库内自算（issue #120）
                results[name] = _strong_stocks_section(conn, effective, started)
                continue
            if adapters is not None:
                chain = (
                    lhb_adapters
                    if name == "lhb" and lhb_adapters is not None
                    else adapters
                )
            elif name == "indices":
                chain = default_index_spot_adapters()
            elif name == "boards":
                chain = default_boards_adapters()
            elif name == "lhb":
                chain = (
                    lhb_adapters
                    if lhb_adapters is not None
                    else default_lhb_adapters()
                )
            else:
                chain = default_adapters()
            try:
                r = fetch_section_with_fallback(chain, name, effective)
            except AllSourcesFailed as e:
                if name == "lhb" and lhb_history and lhb_fallback is not None:
                    r = _lhb_all_markets_failure_result(e)
                else:
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
                    source = None
                    detail = summarize_attempted(e.attempted)
                    source_meta = next(
                        (
                            a.get("source_meta") for a in e.attempted
                            if a.get("source_meta", {}).get("szse_primary")
                        ),
                        None,
                    )
                    if source_meta:
                        primary = source_meta["szse_primary"]
                        source = e.attempted[0]["source"]
                        attempted_primary = (
                            primary["requested"]
                            if primary["source"] != primary["requested"]
                            else primary["source"]
                        )
                        if attempted_primary != source:
                            source = f"{attempted_primary}+{source}"
                        entry["source"] = source
                        entry["source_detail"] = {"szse_primary": primary}
                        note = f"szse_primary={primary['requested']}"
                        if primary["source"] != primary["requested"]:
                            note += f"->{primary['source']}"
                        note += f"({primary['rows']}行)"
                        if primary.get("error"):
                            note += f":{primary['error']}"
                        detail = f"{detail}; {note}" if detail else note
                    results[name] = entry
                    record_collection(
                        conn,
                        trade_date=effective,
                        section=name,
                        status="error",
                        source=source,
                        error_kind=kind,
                        detail=detail,
                        started_at=started,
                    )
                    continue
            data = r["data"]
            if name == "lhb" and lhb_history and lhb_fallback is not None:
                data = _merge_lhb_basic_fallback(
                    data, r["source"], lhb_fallback, effective
                )
            source = r["source"]
            szse_primary = data.get("szse_primary") if name == "lhb" else None
            if szse_primary and szse_primary["source"] != r["source"]:
                source = f"{szse_primary['source']}+{source}"
            basic_fallback = data.get("basic_fallback") if name == "lhb" else None
            if basic_fallback and basic_fallback["source"] not in source.split("+"):
                source = f"{source}+{basic_fallback['source']}"
            patch = None
            if name == "indices":
                data, patch = _patch_major_indices(chain, data)
                # 行级日期校验（issue #78）：行级日期与请求日期不符即拒收零落库，
                # 防止上游返回陈旧交易日数据被静默计入请求日
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
            if name == "lhb" and lhb_history and data.get("errors"):
                message = "; ".join(data["errors"])
                error_detail = message
                source_detail = {}
                if szse_primary:
                    source_detail["szse_primary"] = szse_primary
                    note = f"szse_primary={szse_primary['requested']}"
                    if szse_primary["source"] != szse_primary["requested"]:
                        note += f"->{szse_primary['source']}"
                    note += f"({szse_primary['rows']}行)"
                    error_detail += f"; {note}"
                if basic_fallback:
                    source_detail.update({
                        "primary": data["primary_source"],
                        "fallback": basic_fallback,
                    })
                    error_detail += (
                        f"; basic_fallback={basic_fallback['source']}:"
                        f"{','.join(basic_fallback['markets'])}"
                        f"({basic_fallback['rows']}行)"
                    )
                record_collection(
                    conn, trade_date=effective, section=name, status="error",
                    source=source, rows=0, error_kind="partial_error",
                    detail=error_detail, started_at=started,
                )
                results[name] = {
                    "rows": 0, "status": "error", "error": message,
                    "error_kind": "partial_error", "source": source,
                }
                if source_detail:
                    results[name]["source_detail"] = source_detail
                continue
            statistic_written = 0
            if name == "lhb" and lhb_history:
                written, statistic_written = replace_lhb_basic_day(
                    conn, effective, data["lhb_basic"], lhb_staging_rows
                )
            else:
                written = _upsert_section(conn, name, data, effective)
            res: dict = {
                "rows": written,
                "source": source,
                "status": "ok",
            }
            if szse_primary:
                res["source_detail"] = {"szse_primary": szse_primary}
            if basic_fallback:
                res.setdefault("source_detail", {}).update({
                    "primary": data["primary_source"],
                    "fallback": basic_fallback,
                })
            if name == "boards":
                # 字段级第四态（issue #118）：申万不发布的三字段机器可读声明
                # （量比重申万官网不发布、领涨股不发布、资金流永久弃用），
                # 消费方按 planned_absent 语义处理——不报故障、不入 data_gaps
                res["planned_absent_fields"] = dict(PLANNED_ABSENT_BOARD_FIELDS)
            if patch:
                res["patch"] = patch
            if r["attempted_sources"]:
                res["attempted_sources"] = r["attempted_sources"]
            if name == "lhb":
                if data.get("errors"):
                    # 子项部分失败：拿到什么落什么，但失败原因必须报告
                    res["partial_error"] = "; ".join(data["errors"])
                if not data["lhb_basic"]:
                    res["empty_kind"] = "no_data_for_date"
                    res["note"] = _LHB_EMPTY_NOTE
                # statistic 子表不在适配器承载（issue #114 定稿，spec 冲突修正）：
                # lhb_basic 落库后由库内近 60 日历日窗口滚动自算（DELETE+INSERT
                # 幂等，窗口滑出/上游修订自愈）；新浪 ggtj 回归 --check 对拍校验位
                res["rows"] += (
                    statistic_written
                    if lhb_history
                    else recompute_lhb_statistic(conn, effective)
                )
            elif not data:
                res["empty_kind"] = "no_data_for_date"
            detail = summarize_attempted(r["attempted_sources"])
            if szse_primary:
                note = f"szse_primary={szse_primary['requested']}"
                if szse_primary["source"] != szse_primary["requested"]:
                    note += f"->{szse_primary['source']}"
                note += f"({szse_primary['rows']}行)"
                if szse_primary.get("error"):
                    note += f":{szse_primary['error']}"
                detail = f"{detail}; {note}" if detail else note
            if basic_fallback:
                note = (
                    f"basic_fallback={basic_fallback['source']}:"
                    f"{','.join(basic_fallback['markets'])}({basic_fallback['rows']}行)"
                )
                detail = f"{detail}; {note}" if detail else note
            if name == "lhb" and data.get("errors"):
                note = "partial_error=" + "; ".join(data["errors"])
                detail = f"{detail}; {note}" if detail else note
            if name == "boards":
                note = "planned_absent_fields=" + ",".join(
                    PLANNED_ABSENT_BOARD_FIELDS
                )
                detail = f"{detail} {note}" if detail else note
            record_collection(
                conn,
                trade_date=effective,
                section=name,
                status="ok" if res["rows"] > 0 else "empty",
                source=source,
                rows=res["rows"],
                error_kind=None if res["rows"] > 0 else "no_data_for_date",
                detail=detail,
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
