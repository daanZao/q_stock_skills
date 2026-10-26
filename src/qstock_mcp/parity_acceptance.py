"""迁移总验收对拍编排（issue #122，spec #111）：五核心面逐面对拍 + 库内
历史连续性检查。纯读 + 逐源直取，全程不落库；固定 CLI 入口
`qstock-scheduler parity`（手册 `docs/parity-cli.md`）。

五个核心面（boards/market_snapshot/indices/lhb/zt_pool）的对拍复用各面
既有 parity 工具：
- boards：sw_board_parity.compare_sw_board_sources（官网 vs 库内 vs 自算）；
- snapshot：snapshot_parity.compare_snapshot_sources（腾讯两源 + 新浪三源）；
- indices：index_parity.compare_index_spot_sources（腾讯 vs 新浪快照）
  并列 tools_index.verify_index_coverage（库内日线覆盖）；
- lhb：tools_lhb.compare_lhb_sources（交易所官方 vs 新浪互补）；
- zt_pool：跨源字段级对拍不可行（东财本机不可达是迁移前提，ADR 0012
  记录），该面以 THS 封禁探测 + 库内历史连续性作为验收证据。

历史连续性（check_history_continuity）：逐交易日查库内表行数，并对照
采集台账归因（就绪检查语义，CONTEXT.md）——有行 = 已覆盖；无行但台账
ok/empty = 上游确认无数据（lhb/zt_pool 等事件驱动面为正常语义）；无行
且无确认 = 断档（gap_kind: error=采集失败 / uncollected=无台账 / 每日
节律面出现 empty 亦记 gap）。indices 面复用 verify_index_coverage 对照
交易日历逐指数列缺口（科创50 回填窗口由此显式呈现）。
"""

import logging
from typing import Callable, Sequence

from .dates import add_days, normalize_date, today_str, trading_days_between
from .db import connect
from .index_parity import compare_index_spot_sources
from .output import error as _error
from .snapshot_parity import compare_snapshot_sources
from .sw_board_parity import compare_sw_board_sources
from .tools_index import verify_index_coverage
from .tools_lhb import compare_lhb_sources

log = logging.getLogger(__name__)

FACES = ("boards", "snapshot", "indices", "lhb", "zt_pool")
DEFAULT_WINDOW_DAYS = 120  # 连续性检查缺省窗口（自然日，约半年交易日）

_ZT_POOL_INFEASIBLE_NOTE = (
    "zt_pool 跨源字段级对拍不可行：东财本机不可达是迁移前提（2026-10-19 事故，"
    "spec #111），原承载 efinance/akshare 东财函数整体不可用；THS 涨停揭秘为"
    "唯一承载源（issue #120，ADR 0012 记录对拍不可行与字段实测冒烟）。本面"
    "验收证据 = THS 封禁探测（页面级，不打 dataapi）+ 库内历史连续性。"
)

# 面的库表与台账 section 映射（表名/列名仅来自本内部常量，不接外部输入）；
# cadence: daily = 每个交易日必有行（全市场面）；event = 无行可为正常
# （上游确认无数据，如 lhb 无榜单日），以台账 ok/empty 为覆盖依据。
_FACE_TABLES: dict[str, dict] = {
    "snapshot": {
        "table": "market_snapshot",
        "sections": ("market_snapshot",),
        "cadence": "daily",
    },
    "boards": {
        "table": "market_boards",
        "sections": ("boards", "market_boards"),
        "cadence": "daily",
    },
    "zt_pool": {
        "table": "zt_pool",
        "sections": ("zt_pool",),
        "cadence": "event",
    },
    "lhb": {
        "table": "lhb_basic",
        "sections": ("lhb",),
        "cadence": "event",
    },
}


def _parse_faces(faces: Sequence[str] | str | None) -> tuple[list[str] | None, str | None]:
    """面集解析与校验（缺省 = 五核心面全集）。"""
    if faces is None:
        return list(FACES), None
    if isinstance(faces, str):
        raw = [f.strip() for f in faces.split(",")]
    else:
        raw = [str(f).strip() for f in faces]
    selected = [f for f in raw if f]
    unknown = [f for f in selected if f not in FACES]
    if unknown:
        return None, f"非法 face: {','.join(unknown)}（可选: {','.join(FACES)}）"
    if not selected:
        return None, "face 列表为空"
    seen: set[str] = set()
    out: list[str] = []
    for f in selected:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out, None


def _table_date_counts(conn, table: str, start: str, end: str) -> dict[str, int]:
    """窗口内库内表逐交易日行数（表名仅来自 _FACE_TABLES 内部常量）。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT trade_date, COUNT(*) FROM {table}"
            " WHERE trade_date BETWEEN %s AND %s GROUP BY trade_date",
            (start, end),
        )
        return {str(row[0]): int(row[1]) for row in cur.fetchall()}


def _ledger_statuses(conn, sections: tuple[str, ...], start: str,
                     end: str) -> dict[str, str]:
    """窗口内逐交易日最新台账状态（多 section 取时间上最新的一行）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, status FROM ("
            " SELECT DISTINCT ON (trade_date) trade_date, status"
            " FROM collection_log"
            " WHERE trade_date BETWEEN %s AND %s AND section = ANY(%s)"
            " ORDER BY trade_date, finished_at DESC"
            ") t",
            (start, end, list(sections)),
        )
        return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def _check_table_continuity(conn, face: str, start: str, end: str) -> dict:
    """单面通用连续性：逐交易日 行数 + 台账归因。"""
    spec = _FACE_TABLES[face]
    counts = _table_date_counts(conn, spec["table"], start, end)
    ledger = _ledger_statuses(conn, spec["sections"], start, end)
    daily = spec["cadence"] == "daily"
    gap_days: list[dict] = []
    empty_confirmed: list[str] = []
    for day in trading_days_between(start, end):
        n = counts.get(day, 0)
        if n > 0:
            continue
        status = ledger.get(day)
        if status in ("ok", "empty") and not daily:
            empty_confirmed.append(day)  # 事件驱动面：上游确认无数据，正常语义
            continue
        gap_days.append(
            {
                "trade_date": day,
                "ledger": status,  # None=无台账（未采集）/ error=采集失败 / empty
            }
        )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM {spec['table']}"
        )
        first_d, last_d, total = cur.fetchone()
    return {
        "table": spec["table"],
        "cadence": spec["cadence"],
        "window": {"start": start, "end": end},
        "trading_days": len(trading_days_between(start, end)),
        "covered_days": sum(1 for d in counts if counts[d] > 0),
        "empty_confirmed_days": empty_confirmed,
        "gap_days": gap_days,
        "table_first_date": str(first_d) if first_d else None,
        "table_last_date": str(last_d) if last_d else None,
        "table_rows": int(total),
        "status": "ok" if not gap_days else "gap",
    }


def _check_indices_continuity(conn, start: str, end: str) -> dict:
    """indices 面连续性：index_daily 逐指数对照交易日历（复用覆盖核验）
    + market_indices 快照表逐日覆盖。"""
    coverage = verify_index_coverage(start=start, end=end)
    snap_counts = _table_date_counts(conn, "market_indices", start, end)
    ledger = _ledger_statuses(conn, ("indices",), start, end)
    gap_days = [
        {"trade_date": d, "ledger": ledger.get(d)}
        for d in trading_days_between(start, end)
        if snap_counts.get(d, 0) == 0 and ledger.get(d) not in ("ok", "empty")
    ]
    return {
        "index_daily": coverage,
        "market_indices": {
            "table": "market_indices",
            "window": {"start": start, "end": end},
            "covered_days": sum(1 for d in snap_counts if snap_counts[d] > 0),
            "gap_days": gap_days,
            "status": "ok" if not gap_days else "gap",
        },
    }


def check_history_continuity(
    faces: Sequence[str] | str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    end: str | None = None,
) -> dict:
    """库内历史连续性检查（纯读）：逐核心面列窗口内交易日覆盖与断档。

    faces 缺省 = 五核心面；window_days 缺省 120 自然日；end 缺省当日。
    逐面返回 covered/empty_confirmed/gap_days（台账归因）；indices 面为
    verify_index_coverage + market_indices 覆盖的并列结构。
    """
    tool = "check_history_continuity"
    params = {"faces": faces, "window_days": window_days, "end": end}
    selected, err = _parse_faces(faces)
    if err:
        return _error(tool, params, err)
    if not isinstance(window_days, int) or window_days <= 0:
        return _error(tool, params, f"window_days 必须为正整数: {window_days!r}")
    try:
        end_n = normalize_date(end) if end else today_str()
    except ValueError as e:
        return _error(tool, params, str(e))
    start_n = add_days(end_n, -window_days)
    conn, conn_err = connect()
    if conn_err:
        return _error(tool, params, conn_err)
    out_faces: dict[str, dict] = {}
    try:
        for face in selected:
            if face == "indices":
                out_faces[face] = _check_indices_continuity(conn, start_n, end_n)
            else:
                out_faces[face] = _check_table_continuity(
                    conn, face, start_n, end_n
                )
    finally:
        conn.close()

    def _face_status(entry: dict) -> str:
        if "market_indices" in entry:
            sts = [entry["index_daily"].get("status"),
                   entry["market_indices"].get("status")]
            return "ok" if all(s == "ok" for s in sts) else "gap"
        return entry.get("status", "gap")

    summary = {f: _face_status(e) for f, e in out_faces.items()}
    return {
        "status": "ok" if all(s == "ok" for s in summary.values()) else "gap",
        "tool": tool,
        "params": params,
        "window": {"start": start_n, "end": end_n},
        "faces": out_faces,
        "summary": summary,
    }


def run_parity_acceptance(
    trade_date: str | None = None,
    faces: Sequence[str] | str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int | None = None,
    probe: Callable[[], dict] | None = None,
    sw_adapters: Sequence | None = None,
    snapshot_adapters: Sequence | None = None,
    index_adapters: Sequence | None = None,
    lhb_exchange=None,
    lhb_sina=None,
) -> dict:
    """五核心面逐面对拍 + 历史连续性（全程不落库）。

    逐面复用各面 parity 工具（见模块头）；zt_pool 对拍不可行（ADR 0012），
    以 probe（THS 封禁探测，可注入；None = 跳过探测）+ 连续性为证据。
    probe 传 None 时 zt_pool 面探测记 skipped（离线/测试环境）。
    """
    tool = "run_parity_acceptance"
    params = {
        "trade_date": trade_date, "faces": faces, "window_days": window_days,
        "limit": limit,
    }
    selected, err = _parse_faces(faces)
    if err:
        return _error(tool, params, err)
    try:
        effective = normalize_date(trade_date) if trade_date else today_str()
    except ValueError as e:
        return _error(tool, params, str(e))
    if limit is not None and (not isinstance(limit, int) or limit <= 0):
        return _error(tool, params, f"limit 必须为正整数: {limit!r}")

    parts: dict[str, dict] = {}
    if "boards" in selected:
        parts["boards"] = compare_sw_board_sources(
            trade_date=effective, level="all", limit=limit, adapters=sw_adapters
        )
    if "snapshot" in selected:
        parts["snapshot"] = compare_snapshot_sources(
            adapters=snapshot_adapters, codes_limit=limit
        )
    if "indices" in selected:
        parts["indices"] = {
            "spot": compare_index_spot_sources(adapters=index_adapters),
            "coverage": verify_index_coverage(
                start=add_days(effective, -window_days), end=effective
            ),
        }
    if "lhb" in selected:
        parts["lhb"] = compare_lhb_sources(
            trade_date=effective, exchange=lhb_exchange, sina=lhb_sina
        )
    if "zt_pool" in selected:
        zt_part: dict = {"status": "infeasible", "note": _ZT_POOL_INFEASIBLE_NOTE}
        if probe is None:
            zt_part["probe"] = {"skipped": True,
                                "reason": "未提供 probe（离线/测试环境跳过探测）"}
        else:
            try:
                zt_part["probe"] = probe()
            except Exception as e:  # noqa: BLE001 - 探测失败只记该面
                zt_part["probe"] = {"reachable": False, "banned": False,
                                    "detail": f"探测异常: {e}"}
        parts["zt_pool"] = zt_part

    continuity = check_history_continuity(
        faces=selected, window_days=window_days, end=effective
    )

    def _parity_status(face: str) -> str:
        part = parts.get(face) or {}
        if face == "indices":
            return part.get("spot", {}).get("status", "error")
        if face == "zt_pool":
            return "infeasible"  # 已记录决策（ADR 0012），非失败
        return part.get("status", "error")

    parity_summary = {f: _parity_status(f) for f in selected}
    cont_summary = continuity.get("summary") or {}
    hard_errors = [
        f for f, s in parity_summary.items()
        if s in ("error", "infeasible") and f != "zt_pool"
    ]
    return {
        "status": "ok" if not hard_errors else "error",
        "tool": tool,
        "params": params,
        "trade_date": effective,
        "parity": parts,
        "parity_summary": parity_summary,
        "continuity": continuity,
        "continuity_summary": cont_summary,
        "error": None if not hard_errors else f"对拍源失败: {','.join(hard_errors)}",
    }
