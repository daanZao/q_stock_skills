"""真实上游分层抽样验收：代表股 90 日 + 板块成分全集 10 日。

只在 qstock_test* 隔离库运行。抽样结果只代表本轮 Universe，不构成全市场验收。
"""

from collections import Counter
from itertools import combinations
import os
import random
import time
from typing import Callable, Sequence
from urllib.parse import unquote, urlparse

from ._sw_classify import load_sw_industry_meta_seed
from .adapters import default_adapters
from .dates import trading_day_window, trading_days_between
from .db import connect, ensure_schema, get_dsn
from .fetch_chain import AllSourcesFailed, fetch_stock_list_with_fallback
from .output import error as _error
from .repository import upsert_stock_list
from .testdb import TestRunnerConflict, reset_schema, test_runner_lock
from .tools_coverage import verify_history_coverage
from .tools_daily import query_daily
from .tools_init import refresh_sw_dict

MARKET_GROUPS = {
    "sh_main": ("600", "601", "603", "605"),
    "sz_main": ("000", "001", "002", "003"),
    "star": ("688", "689"),
    "chinext": ("300", "301"),
    "bse": ("43", "83", "87", "920"),
}
LONG_SUSPENSION_DAYS = 20
# 性能验证（2026-10-21，ADR 0010）：腾讯连发 5–6 请求会断连；live 沿用
# 已验证的串行 1 秒/只节奏，不启用并发。最小安全间隔与 TPS 上限仍待验证。
DEFAULT_THROTTLE_S = 1.0


def _market_group(code: str) -> str | None:
    return next(
        (name for name, prefixes in MARKET_GROUPS.items() if code.startswith(prefixes)),
        None,
    )


def _representative_selection(rows: list[dict], seed: int) -> dict:
    names = {}
    candidates: dict[str, list[str]] = {name: [] for name in MARKET_GROUPS}
    for row in rows:
        code = row.get("stock_code")
        group = _market_group(code or "")
        if group and code and row.get("stock_name"):
            names[code] = row["stock_name"]
            if code not in candidates[group]:
                candidates[group].append(code)
    rng = random.Random(seed)
    selected = {}
    for group, codes in candidates.items():
        shuffled = sorted(codes)
        rng.shuffle(shuffled)
        selected[group] = {
            "initial_codes": shuffled[:3],
            "remaining_codes": shuffled[3:],
        }
    return {"groups": selected, "names": names}


def _longest_streak(days: set[str], calendar: list[str]) -> int:
    longest = current = 0
    for day in calendar:
        current = current + 1 if day in days else 0
        longest = max(longest, current)
    return longest


def _query_stock(
    code: str,
    start: str,
    end: str,
    expected_days: int,
    adapters: Sequence | None,
    throttle: float,
    sleep: Callable[[float], None],
) -> dict:
    started = time.monotonic()
    raw = query_daily(code, start=start, end=end, adapters=adapters)
    duration = round(time.monotonic() - started, 3)
    if throttle:
        sleep(throttle)
    base = {
        "stock_code": code,
        "duration_s": duration,
        "range": {"start": start, "end": end, "trading_days": expected_days},
    }
    if raw.get("status") != "ok":
        return {
            **base,
            "status": "error",
            "error": raw.get("error"),
            "attempted_sources": raw.get("attempted_sources", []),
            "sources": [],
            "sample_limits": [],
        }

    rows = raw.get("rows", [])
    calendar = trading_days_between(start, end)
    dates = {row["trade_date"] for row in rows}
    missing_days = [day for day in calendar if day not in dates]
    suspended_days = {row["trade_date"] for row in rows if row.get("suspended")}
    marked_streak = _longest_streak(suspended_days, calendar)
    observed_calendar = (
        [day for day in calendar if rows[0]["trade_date"] <= day <= rows[-1]["trade_date"]]
        if rows else []
    )
    missing_streak = _longest_streak(set(missing_days), observed_calendar)
    limits = []
    if not rows:
        limits.append("no_data")
    else:
        if rows[0]["trade_date"] > start:
            limits.extend(["new_stock", "history_insufficient"])
        if rows[-1]["trade_date"] < end:
            limits.append("history_insufficient")
        if max(marked_streak, missing_streak) >= LONG_SUSPENSION_DAYS:
            limits.append("long_suspension")
    sources = {segment["source"] for segment in raw.get("healed", [])}
    sources.update(row["source"] for row in rows if row.get("source"))
    return {
        **base,
        "status": "sample_limited" if limits else "ok",
        "count": len(rows),
        "active_days": len(rows) - len(suspended_days),
        "suspended_days": len(suspended_days),
        "missing_days": missing_days,
        "suspension_basis": (
            "marked_rows" if marked_streak >= LONG_SUSPENSION_DAYS
            else "missing_rows" if missing_streak >= LONG_SUSPENSION_DAYS
            else None
        ),
        "first_date": rows[0]["trade_date"] if rows else None,
        "last_date": rows[-1]["trade_date"] if rows else None,
        "sources": sorted(sources),
        "sample_limits": list(dict.fromkeys(limits)),
    }


def _run_representatives(
    selection: dict,
    start: str,
    end: str,
    adapters: Sequence | None,
    throttle: float,
    sleep: Callable[[float], None],
) -> dict:
    groups = {}
    for name, chosen in selection["groups"].items():
        initial = chosen["initial_codes"]
        attempts = [
            _query_stock(code, start, end, 90, adapters, throttle, sleep)
            for code in initial
        ]
        supplement = None
        limited = next((item for item in attempts if item["sample_limits"]), None)
        if limited and chosen["remaining_codes"]:
            code = chosen["remaining_codes"][0]
            supplement = _query_stock(code, start, end, 90, adapters, throttle, sleep)
            supplement["replacement_for"] = limited["stock_code"]
            supplement["replacement_reasons"] = limited["sample_limits"]
            attempts.append(supplement)
        effective = list(initial)
        if supplement and supplement["status"] == "ok" and limited is not None:
            effective[effective.index(limited["stock_code"])] = supplement["stock_code"]
        groups[name] = {
            "initial_codes": initial,
            "effective_codes": effective,
            "supplement": supplement,
            "attempts": attempts,
            "sample_limit": (
                "replacement_still_limited"
                if supplement and supplement["status"] == "sample_limited"
                else "no_replacement_available"
                if limited and not chosen["remaining_codes"]
                else None
            ),
        }
    return {
        "range": {"start": start, "end": end, "trading_days": 90},
        "groups": groups,
    }


def _board_candidates(conn, end: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH pit AS (
                SELECT DISTINCT ON (stock_code) stock_code, board_code
                FROM sw_industry_class
                WHERE source_system = 'sw'
                  AND effective_date <= to_date(%s, 'YYYYMMDD')
                ORDER BY stock_code, effective_date DESC
            )
            SELECT stock_code, board_code FROM pit ORDER BY stock_code, board_code
            """,
            (end,),
        )
        rows = cur.fetchall()
    members: dict[tuple[str, int], list[str]] = {}
    for stock_code, board_code in rows:
        for level, code in (
            (1, board_code[:2] + "0000"),
            (2, board_code[:4] + "00"),
        ):
            members.setdefault((code, level), []).append(stock_code)
    names = {row["industry_code"]: row["name"] for row in load_sw_industry_meta_seed()}
    return [
        {
            "board_code": code,
            "board_name": names.get(code),
            "level": level,
            "constituent_count": len(stock_codes),
            "stock_codes": sorted(stock_codes),
        }
        for (code, level), stock_codes in members.items()
    ]


def _select_boards(conn, end: str, seed: int) -> tuple[list[dict], bool]:
    candidates = _board_candidates(conn, end)
    random.Random(seed).shuffle(candidates)
    for size in (2, 3):
        for chosen in combinations(candidates, size):
            total = sum(board["constituent_count"] for board in chosen)
            if 100 <= total <= 300:
                return list(chosen), True
    return candidates[:2], False


def _reset_test_schema() -> None:
    dsn = get_dsn()
    database = unquote(urlparse(dsn).path.rsplit("/", 1)[-1])
    if not database.startswith("qstock_test"):
        raise ValueError("live 抽样只允许 qstock_test* 隔离库，拒绝清库")
    reset_schema(dsn)
    conn, err = connect()
    if err or conn is None:
        raise RuntimeError(err)
    try:
        ensure_schema(conn)
    finally:
        conn.close()


def _replace_stock_list(rows: list[dict], source: str) -> None:
    conn, err = connect()
    if err or conn is None:
        raise RuntimeError(err)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM stock_list")
        conn.commit()
        upsert_stock_list(conn, rows, source)
    finally:
        conn.close()


def _run_live_sample(
    seed: int = 134,
    end: str | None = None,
    daily_adapters: Sequence | None = None,
    list_adapters: Sequence | None = None,
    sw_adapters: Sequence | None = None,
    throttle: float = DEFAULT_THROTTLE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """执行可复现 live 抽样；异常每组最多补抽一次，采集失败不补抽。"""
    params = {"seed": seed, "end": end, "throttle": throttle}
    try:
        rep_start, last = trading_day_window(90, end)
        board_start, _ = trading_day_window(10, last)
        _reset_test_schema()
        listed = fetch_stock_list_with_fallback(list_adapters or default_adapters())
        dictionary = refresh_sw_dict(adapters=sw_adapters)
    except (ValueError, RuntimeError, AllSourcesFailed) as exc:
        return {
            **_error("live_sample", params, str(exc)),
            "scope": "live_sample",
        }
    if dictionary.get("status") != "ok":
        return {
            **_error("live_sample", params, dictionary.get("error", "申万字典刷新失败")),
            "scope": "live_sample",
            "dictionary": dictionary,
        }

    started = time.monotonic()
    selection = _representative_selection(listed["rows"], seed)
    representatives = _run_representatives(
        selection, rep_start, last, daily_adapters, throttle, sleep
    )

    conn, err = connect()
    if err or conn is None:
        return {
            **_error("live_sample", params, err or "数据库连接失败"),
            "scope": "live_sample",
        }
    try:
        boards, size_target_met = _select_boards(conn, last, seed)
    finally:
        conn.close()
    if len(boards) < 2:
        return {
            **_error("live_sample", params, "申万 point-in-time 候选板块不足 2 个"),
            "scope": "live_sample",
            "representatives": representatives,
        }
    board_codes = sorted({code for board in boards for code in board["stock_codes"]})
    board_attempts = [
        _query_stock(code, board_start, last, 10, daily_adapters, throttle, sleep)
        for code in board_codes
    ]

    representative_codes = {
        attempt["stock_code"]
        for group in representatives["groups"].values()
        for attempt in group["attempts"]
    }
    universe = sorted(representative_codes | set(board_codes))
    names = selection["names"]
    _replace_stock_list(
        [
            {"stock_code": code, "stock_name": names.get(code, code)}
            for code in universe
        ],
        listed["source"],
    )
    coverage = verify_history_coverage(board_start, last)
    coverage_stocks = coverage.get("sections", {}).get("stock_daily", [])
    coverage_status = (
        "error"
        if "sections" not in coverage
        or len(coverage_stocks) != len(universe)
        or any(item["status"] != "ok" for item in coverage_stocks)
        else "ok"
    )
    all_attempts = board_attempts + [
        attempt
        for group in representatives["groups"].values()
        for attempt in group["attempts"]
    ]
    errors = sum(attempt["status"] == "error" for attempt in all_attempts)
    limited = sum(attempt["status"] == "sample_limited" for attempt in all_attempts)
    source_metrics = Counter(
        source for attempt in all_attempts for source in attempt["sources"]
    )
    status = "error" if errors == len(all_attempts) else "partial_error" if errors else "ok"
    return {
        "status": status,
        "tool": "live_sample",
        "scope": "live_sample",
        "params": params,
        "seed": seed,
        "representatives": representatives,
        "boards": {
            "range": {"start": board_start, "end": last, "trading_days": 10},
            "selected": [
                {key: value for key, value in board.items() if key != "stock_codes"}
                for board in boards
            ],
            "size_target_met": size_target_met,
            "stock_codes": board_codes,
            "attempts": board_attempts,
        },
        "universe": {"stock_codes": universe, "stock_count": len(universe)},
        "coverage": {
            "scope": "stock_daily_in_universe",
            "status": coverage_status,
            "calendar_days": coverage.get("calendar_days"),
            "stocks": coverage_stocks,
        },
        "summary": {
            "attempts": len(all_attempts),
            "errors": errors,
            "sample_limited": limited,
            "duration_s": round(time.monotonic() - started, 3),
            "sources": {
                "stock_list": listed["source"],
                "sw_dict": dictionary.get("source"),
                "daily": dict(sorted(source_metrics.items())),
            },
        },
    }


def run_live_sample(
    seed: int = 134,
    end: str | None = None,
    daily_adapters: Sequence | None = None,
    list_adapters: Sequence | None = None,
    sw_adapters: Sequence | None = None,
    throttle: float = DEFAULT_THROTTLE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """持有共享 runner 锁执行 live 抽样，避免与 pytest 并发清库。"""
    maintenance = os.environ.get(
        "QSTOCK_TEST_MAINT_DSN", "postgresql://localhost/postgres"
    )
    try:
        with test_runner_lock(maintenance):
            return _run_live_sample(
                seed=seed,
                end=end,
                daily_adapters=daily_adapters,
                list_adapters=list_adapters,
                sw_adapters=sw_adapters,
                throttle=throttle,
                sleep=sleep,
            )
    except TestRunnerConflict as exc:
        return {
            **_error("live_sample", {"seed": seed, "end": end}, str(exc)),
            "scope": "live_sample",
        }
