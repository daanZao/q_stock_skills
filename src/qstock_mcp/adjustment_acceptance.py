"""复权迁移八项硬验收（issue #165，spec #158 §7）。"""

import csv
import json
import math
import os
from datetime import datetime
from decimal import Decimal
from importlib import resources
from time import monotonic

from .adjustment_audit import _hash_text, _raw_sample, _stock_hashes, _validation
from .adjustment_backfill import ADJ_BACKFILL_THROTTLE_S, _Pacer
from .dates import (
    add_days, next_trading_day, prev_trading_day, trading_day_window, trading_days_between,
)
from .baostock_errors import BaostockError
from .db import connect
from .output import error as _error
from .tools_baostock import baostock_error


CHECK_IDS = (
    "factor_baseline",
    "event_invariants",
    "structure_coverage",
    "indicator_regression",
    "upstream_smoke",
    "event_reconciliation",
    "golden_set",
    "hash_idempotency",
)


def _calendar_sql(start: str, end: str) -> tuple[str, list[str]]:
    args: list[str] = []
    for day in trading_days_between(start, end):
        args.extend((day, prev_trading_day(day), next_trading_day(add_days(day, 1))))
    values = ",".join("(%s,%s,%s)" for _ in range(len(args) // 3))
    return f"calendar(trade_date,previous_date,next_date) AS (VALUES {values})", args


def _check_factor_baseline(conn, start: str, end: str) -> dict:
    calendar, calendar_args = _calendar_sql(start, end)
    with conn.cursor() as cur:
        cur.execute(
            """ WITH """ + calendar + """, events AS (
                SELECT stock_code, ex_date, factor_cum,
                       row_number() OVER (
                           PARTITION BY stock_code ORDER BY ex_date
                       ) AS event_number,
                       lag(factor_cum) OVER (
                           PARTITION BY stock_code ORDER BY ex_date
                       ) AS previous_b
                FROM stock_adjust_factor
                WHERE ex_date BETWEEN %s AND %s
            ), audited AS (
                SELECT e.stock_code, e.ex_date, e.event_number, e.factor_cum,
                       s.baseline_factor, r.close, r.change_percent, pr.close AS previous_close,
                       r.close / NULLIF(
                           pr.close * (1 + r.change_percent / 100), 0
                       ) AS price_factor,
                       coalesce(e.previous_b, s.baseline_factor) / e.factor_cum
                       AS authoritative_factor,
                       count(*) FILTER (WHERE e.event_number=1) OVER () AS first_events,
                       count(*) OVER () AS events_checked
                FROM events e
                JOIN calendar c ON c.trade_date=e.ex_date
                LEFT JOIN stock_factor_state s USING (stock_code)
                LEFT JOIN stock_daily_raw r
                  ON r.stock_code=e.stock_code AND r.trade_date=e.ex_date
                LEFT JOIN stock_daily_raw pr
                  ON pr.stock_code=e.stock_code AND pr.trade_date=c.previous_date
            ), failures AS (
                SELECT *, count(*) OVER () AS total_failures,
                    array_remove(ARRAY[
                        CASE WHEN close IS NULL OR close <= 0 OR previous_close IS NULL
                              OR previous_close <= 0 OR change_percent IS NULL
                             THEN 'pctChg/D-1' END,
                        CASE WHEN baseline_factor IS NULL OR baseline_factor <= 0
                              OR authoritative_factor IS NULL OR authoritative_factor <= 0
                             THEN 'B0' END,
                        CASE WHEN event_number=1 AND price_factor IS NOT NULL
                              AND baseline_factor IS NOT NULL
                              AND abs(baseline_factor - factor_cum * price_factor)
                                  > 1e-6 * greatest(abs(baseline_factor), 1)
                             THEN 'B0=B1*f1' END,
                        CASE WHEN price_factor IS NOT NULL
                              AND authoritative_factor IS NOT NULL
                              AND abs(price_factor - authoritative_factor)
                                  / authoritative_factor > 0.001
                             THEN 'relative_error>0.1%%' END
                    ], NULL) AS failed_fields
                FROM audited
            )
            SELECT stock_code, ex_date, failed_fields,
                   (SELECT count(*) FROM failures WHERE failed_fields <> '{}'::text[]),
                   max(first_events), max(events_checked)
            FROM failures
            WHERE failed_fields <> '{}'::text[]
            GROUP BY stock_code, ex_date, failed_fields
            ORDER BY stock_code, ex_date LIMIT 100
            """,
            (*calendar_args, start, end),
        )
        rows = cur.fetchall()
        if rows:
            total = rows[0][3]
            first_events = rows[0][4]
            events_checked = rows[0][5]
        else:
            cur.execute(
                """
                SELECT count(*), count(*) FILTER (WHERE event_number=1) FROM (
                    SELECT row_number() OVER (
                        PARTITION BY stock_code ORDER BY ex_date
                    ) AS event_number
                    FROM stock_adjust_factor WHERE ex_date BETWEEN %s AND %s
                ) events
                """,
                (start, end),
            )
            events_checked, first_events = cur.fetchone()
            total = 0
    return {
        "id": "factor_baseline",
        "status": "ok" if not total else "error",
        "events_checked": events_checked,
        "first_events_checked": first_events,
        "relative_tolerance": 0.001,
        "failure_count": total,
        "failures": [
            {
                "stock_code": row[0], "trade_date": row[1], "event_date": row[1],
                "fields": row[2],
            }
            for row in rows
        ],
        "failures_truncated": total > len(rows),
    }


def _check_event_invariants(conn, start: str, end: str) -> dict:
    calendar, calendar_args = _calendar_sql(start, end)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM stock_adjust_factor WHERE ex_date BETWEEN %s AND %s",
            (start, end),
        )
        events_checked = cur.fetchone()[0]
        cur.execute(
            """ WITH """ + calendar + """, events AS (
                SELECT stock_code, ex_date, factor_cum,
                       lag(factor_cum) OVER (
                           PARTITION BY stock_code ORDER BY ex_date
                       ) AS previous_b
                FROM stock_adjust_factor
                WHERE ex_date BETWEEN %s AND %s
            ), neighborhoods AS (
                SELECT e.stock_code, e.ex_date, e.factor_cum,
                       coalesce(e.previous_b, s.baseline_factor) AS previous_b,
                       s.factor_latest,
                       pr.trade_date AS previous_date,
                       coalesce(p.factor_cum, CASE
                           WHEN e.previous_b IS NULL AND pr.trade_date<%s THEN 1 END
                       ) AS previous_f,
                       coalesce(p.close, pr.close) AS previous_hfq,
                       pr.close AS previous_raw,
                       h.factor_cum AS current_f, h.close AS current_hfq,
                       r.close AS current_raw,
                       n.trade_date AS next_date, n.close AS next_hfq,
                       nr.close AS next_raw, n.factor_cum AS next_f
                FROM events e
                JOIN calendar c ON c.trade_date=e.ex_date
                JOIN stock_factor_state s USING (stock_code)
                LEFT JOIN stock_daily_raw pr
                  ON pr.stock_code=e.stock_code AND pr.trade_date=c.previous_date
                LEFT JOIN stock_daily_hfq p
                  ON p.stock_code=e.stock_code AND p.trade_date=c.previous_date
                LEFT JOIN stock_daily_hfq h
                  ON h.stock_code=e.stock_code AND h.trade_date=e.ex_date
                LEFT JOIN stock_daily_raw r
                  ON r.stock_code=e.stock_code AND r.trade_date=e.ex_date
                LEFT JOIN stock_daily_hfq n
                  ON n.stock_code=e.stock_code AND n.trade_date=c.next_date
                LEFT JOIN stock_daily_raw nr
                  ON nr.stock_code=e.stock_code AND nr.trade_date=c.next_date
            ), failures AS (
                SELECT *, count(*) OVER () AS total_failures,
                    array_remove(ARRAY[
                        CASE WHEN previous_b IS NULL OR previous_b <= 0
                              OR factor_latest IS NULL OR factor_latest <= 0
                             THEN 'state' END,
                        CASE WHEN previous_date IS NULL OR previous_f IS NULL
                              OR previous_raw IS NULL OR previous_raw <= 0
                             THEN 'D-1' END,
                        CASE WHEN current_f IS NULL OR current_raw IS NULL
                              OR current_raw <= 0 THEN 'D' END,
                        CASE WHEN next_date IS NULL OR next_f IS NULL
                              OR next_raw IS NULL OR next_raw <= 0 THEN 'D+1' END,
                        CASE WHEN previous_f IS NOT NULL AND current_f IS NOT NULL
                              AND previous_b IS NOT NULL
                              AND abs(current_f * (previous_b / factor_cum) - previous_f)
                                  > 1e-6 * greatest(abs(previous_f), 1)
                             THEN 'F(D)*f(D)=F(D-1)' END,
                        CASE WHEN previous_hfq IS NOT NULL AND previous_raw IS NOT NULL
                              AND abs(previous_hfq - previous_raw * previous_f)
                                  > 1e-6 * greatest(abs(previous_hfq), 1)
                             THEN 'D-1:hfq=raw*F' END,
                        CASE WHEN current_hfq IS NOT NULL AND current_raw IS NOT NULL
                              AND abs(current_hfq - current_raw * current_f)
                                  > 1e-6 * greatest(abs(current_hfq), 1)
                             THEN 'D:hfq=raw*F' END,
                        CASE WHEN next_hfq IS NOT NULL AND next_raw IS NOT NULL
                              AND abs(next_hfq - next_raw * next_f)
                                  > 1e-6 * greatest(abs(next_hfq), 1)
                             THEN 'D+1:hfq=raw*F' END,
                        CASE WHEN previous_hfq IS NOT NULL AND (
                              factor_latest IS NULL OR abs(
                                  previous_hfq / factor_latest * factor_latest - previous_hfq
                              ) > 1e-6 * greatest(abs(previous_hfq), 1))
                             THEN 'D-1:qfq*F_latest=hfq' END,
                        CASE WHEN current_hfq IS NOT NULL AND (
                              factor_latest IS NULL OR abs(
                                  current_hfq / factor_latest * factor_latest - current_hfq
                              ) > 1e-6 * greatest(abs(current_hfq), 1))
                             THEN 'D:qfq*F_latest=hfq' END,
                        CASE WHEN next_hfq IS NOT NULL AND (
                              factor_latest IS NULL OR abs(
                                  next_hfq / factor_latest * factor_latest - next_hfq
                              ) > 1e-6 * greatest(abs(next_hfq), 1))
                             THEN 'D+1:qfq*F_latest=hfq' END
                    ], NULL) AS failed_fields
                FROM neighborhoods
            )
            SELECT stock_code, ex_date, failed_fields,
                   (SELECT count(*) FROM failures WHERE failed_fields <> '{}'::text[])
            FROM failures
            WHERE failed_fields <> '{}'::text[]
            ORDER BY stock_code, ex_date LIMIT 100
            """,
            (*calendar_args, start, end, start),
        )
        rows = cur.fetchall()
    failures = [
        {
            "stock_code": row[0],
            "trade_date": row[1],
            "event_date": row[1],
            "fields": row[2],
        }
        for row in rows
    ]
    total = rows[0][3] if rows else 0
    reader_failures, reader_stocks = _check_actual_qfq(conn, start, end)
    all_failures = failures + reader_failures
    failure_count = total + len(reader_failures)
    return {
        "id": "event_invariants",
        "status": "ok" if not failure_count else "error",
        "events_checked": events_checked,
        "neighborhood_rows_checked": events_checked * 3,
        "reader_stocks_checked": reader_stocks,
        "failure_count": failure_count,
        "failures": all_failures[:100],
        "failures_truncated": failure_count > len(all_failures),
    }


def _check_actual_qfq(conn, start: str, end: str) -> tuple[list[dict], int]:
    from .price_reader import NewPriceReader, PriceQueryError

    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, min(ex_date) FROM stock_adjust_factor"
            " WHERE ex_date BETWEEN %s AND %s GROUP BY stock_code ORDER BY stock_code",
            (start, end),
        )
        stocks = cur.fetchall()
    failures: list[dict] = []
    reader = NewPriceReader()
    for code, first_event in stocks:
        try:
            result = reader.query_daily(conn, code, "qfq", start, end)
        except PriceQueryError as exc:
            failures.append({
                "stock_code": code, "trade_date": first_event, "event_date": first_event,
                "fields": ["qfq_reader"], "error": str(exc),
            })
            continue
        with conn.cursor() as cur:
            cur.execute(
                "SELECT factor_latest FROM stock_factor_state WHERE stock_code=%s",
                (code,),
            )
            state = cur.fetchone()
            cur.execute(
                "SELECT trade_date, close FROM stock_daily_hfq"
                " WHERE stock_code=%s AND trade_date BETWEEN %s AND %s",
                (code, start, end),
            )
            hfq = dict(cur.fetchall())
        if state is None:
            continue
        for row in result["rows"]:
            date = row["trade_date"]
            if date not in hfq or row["close"] is None:
                continue
            if not _near(float(row["close"]) * float(state[0]), hfq[date]):
                failures.append({
                    "stock_code": code, "trade_date": date, "event_date": first_event,
                    "field": "qfq_reader", "fields": ["qfq_reader"],
                    "expected": hfq[date], "actual": row["close"],
                })
                break
    return failures, len(stocks)


def _indicator_rows(conn, codes: list[str], start: str, end: str) -> list[dict]:
    if not codes:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH qfq AS (
                SELECT h.stock_code, h.trade_date, h.close / s.factor_latest AS qfq_close,
                       h.close AS hfq_close, h.factor_cum, h.amount AS hfq_amount,
                       r.close AS raw_close, r.amount AS raw_amount, s.factor_latest
                FROM stock_daily_hfq h
                JOIN stock_daily_raw r
                  ON r.stock_code=h.stock_code AND r.trade_date=h.trade_date
                JOIN stock_factor_state s ON s.stock_code=h.stock_code
                WHERE h.stock_code=ANY(%s) AND h.trade_date BETWEEN %s AND %s
            )
            SELECT stock_code, trade_date, qfq_close, hfq_close, factor_cum,
                   hfq_amount, raw_close, raw_amount, factor_latest,
                   max(qfq_close) OVER (
                       PARTITION BY stock_code ORDER BY trade_date
                       ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
                   ) AS high_60,
                   max(qfq_close) OVER (
                       PARTITION BY stock_code ORDER BY trade_date
                       ROWS BETWEEN 249 PRECEDING AND CURRENT ROW
                   ) AS high_52w,
                   min(qfq_close) OVER (
                       PARTITION BY stock_code ORDER BY trade_date
                       ROWS BETWEEN 249 PRECEDING AND CURRENT ROW
                   ) AS low_52w
            FROM qfq ORDER BY stock_code, trade_date
            """,
            (codes, start, end),
        )
        columns = (
            "stock_code", "trade_date", "qfq_close", "hfq_close", "factor_cum",
            "hfq_amount", "raw_close", "raw_amount", "factor_latest",
            "high_60", "high_52w", "low_52w",
        )
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _python_indicator_expectations(rows: list[dict]) -> dict[tuple[str, str], dict]:
    by_stock: dict[str, list[dict]] = {}
    for row in rows:
        if row["qfq_close"] is not None:
            by_stock.setdefault(row["stock_code"], []).append(row)
    expected: dict[tuple[str, str], dict] = {}
    for code, stock_rows in by_stock.items():
        closes = [float(row["qfq_close"]) for row in stock_rows]
        for index, row in enumerate(stock_rows):
            last_60 = closes[max(0, index - 59):index + 1]
            last_250 = closes[max(0, index - 249):index + 1]
            expected[(code, row["trade_date"])] = {
                "high_60": max(last_60),
                "high_52w": max(last_250),
                "low_52w": min(last_250),
                "is_60d_high": closes[index] >= max(last_60),
            }
    return expected


def _near(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= 1e-9 * max(abs(float(b)), 1.0)


def _compare_indicators(rows: list[dict], keys: set[tuple[str, str]]) -> tuple[list[dict], int]:
    expected = _python_indicator_expectations(rows)
    failures: list[dict] = []
    comparisons = 0
    by_key = {(row["stock_code"], row["trade_date"]): row for row in rows}
    for key in sorted(keys):
        row = by_key.get(key)
        if row is None:
            failures.append({
                "stock_code": key[0], "trade_date": key[1], "field": "qfq_row",
                "error": "missing",
            })
            continue
        wanted = expected.get(key)
        if wanted is None:
            failures.append({
                "stock_code": key[0], "trade_date": key[1], "field": "qfq_baseline",
                "error": "unavailable",
            })
            continue
        for field in ("high_60", "high_52w", "low_52w"):
            comparisons += 1
            if not _near(row[field], wanted[field]):
                failures.append({
                    "stock_code": key[0], "trade_date": key[1], "field": field,
                    "expected": wanted[field], "actual": float(row[field]),
                })
        comparisons += 1
        if bool(row["qfq_close"] >= row["high_60"]) != wanted["is_60d_high"]:
            failures.append({
                "stock_code": key[0], "trade_date": key[1], "field": "is_60d_high",
            })
        formulas = {
            "hfq=raw*F": _near(row["hfq_close"], float(row["raw_close"]) * float(row["factor_cum"])),
            "qfq*F_latest=hfq": _near(
                float(row["qfq_close"]) * float(row["factor_latest"]), row["hfq_close"]
            ),
            "amount_wan_unchanged": _near(row["hfq_amount"], row["raw_amount"]),
        }
        for field, passed in formulas.items():
            if not passed:
                failures.append({"stock_code": key[0], "trade_date": key[1], "field": field})
    return failures, comparisons


def _class_codes(conn, start: str, end: str, classification: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT r.stock_code FROM stock_daily_raw r
            WHERE r.trade_date BETWEEN %s AND %s
              AND r.close IS NOT NULL AND r.close>0
              AND (EXISTS (
                  SELECT 1 FROM stock_adjust_factor e
                  WHERE e.stock_code=r.stock_code AND e.ex_date BETWEEN %s AND %s
              )) = %s
            ORDER BY r.stock_code
            """,
            (start, end, start, end, classification == "event"),
        )
        return [row[0] for row in cur.fetchall()]


def _class_raw_keys(conn, codes: list[str], start: str, end: str) -> set[tuple[str, str]]:
    if not codes:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, trade_date FROM stock_daily_raw"
            " WHERE stock_code=ANY(%s) AND trade_date BETWEEN %s AND %s"
            " AND close IS NOT NULL AND close>0",
            (codes, start, end),
        )
        return {(row[0], row[1]) for row in cur.fetchall()}


def _legacy_diagnostics(conn, rows: list[dict], keys: set[tuple[str, str]],
                        start: str, end: str, migration_dir: str) -> tuple[list[dict], int]:
    codes = sorted({key[0] for key in keys})
    if not codes:
        return [], 0
    current = {(row["stock_code"], row["trade_date"]): row for row in rows}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, trade_date, close FROM stock_daily"
            " WHERE adj='qfq' AND stock_code=ANY(%s) AND trade_date BETWEEN %s AND %s"
            " AND close IS NOT NULL ORDER BY stock_code, trade_date",
            (codes, start, end),
        )
        legacy = [
            {"stock_code": row[0], "trade_date": row[1], "qfq_close": row[2]}
            for row in cur.fetchall()
        ]
        cur.execute(
            "SELECT stock_code, ex_date FROM stock_adjust_factor"
            " WHERE stock_code=ANY(%s) AND ex_date BETWEEN %s AND %s",
            (codes, start, end),
        )
        events = cur.fetchall()
    legacy_expected = _python_indicator_expectations(legacy)
    legacy_by_key = {(row["stock_code"], row["trade_date"]): row for row in legacy}
    try:
        with open(
            os.path.join(migration_dir, "legacy_diagnostic_review.csv"),
            encoding="utf-8", newline="",
        ) as handle:
            reviews = {
                (row["stock_code"], row["trade_date"]): row
                for row in csv.DictReader(handle) if row.get("reason")
            }
    except FileNotFoundError:
        reviews = {}
    events_by_stock: dict[str, list[str]] = {}
    for code, ex_date in events:
        events_by_stock.setdefault(code, []).append(ex_date)
    diagnostics = []
    for key in sorted(keys):
        legacy_row = legacy_by_key.get(key)
        row = current.get(key)
        if legacy_row is None or row is None or row["qfq_close"] is None:
            continue
        expected = legacy_expected[key]
        pairs = {
            "qfq": (legacy_row["qfq_close"], row["qfq_close"]),
            "high_52w": (expected["high_52w"], row["high_52w"]),
            "low_52w": (expected["low_52w"], row["low_52w"]),
        }
        differences = {
            field: abs(float(old) - float(new)) / max(abs(float(new)), 1.0)
            for field, (old, new) in pairs.items()
        }
        fields = [field for field, difference in differences.items() if difference > 0.01]
        if not fields:
            continue
        distances = [
            (abs((datetime.strptime(ex_date, "%Y%m%d") -
                 datetime.strptime(key[1], "%Y%m%d")).days), ex_date)
            for ex_date in events_by_stock.get(key[0], [])
        ]
        review = reviews.get(key)
        diagnostics.append({
            "stock_code": key[0], "trade_date": key[1],
            "field": "legacy_qfq/52w_diagnostic", "fields": fields,
            "legacy_close": float(legacy_row["qfq_close"]),
            "new_close": float(row["qfq_close"]),
            "legacy_high_52w": expected["high_52w"], "new_high_52w": row["high_52w"],
            "legacy_low_52w": expected["low_52w"], "new_low_52w": row["low_52w"],
            "relative_difference": max(differences.values()),
            "nearest_event": min(distances)[1] if distances else None,
            "explained_by_event": review is not None,
            "review_reason": review.get("reason") if review else None,
            "review_event": review.get("event_date") if review else None,
        })
    return diagnostics, sum(not row["explained_by_event"] for row in diagnostics)


def _sample_contract(conn, sample: list[dict], migration: dict,
                     start: str, end: str, golden_codes: set[str]) -> tuple[list[dict], dict]:
    failures: list[dict] = []
    if migration.get("ordinary_raw_sample", {}).get("seed") != "164":
        failures.append({"field": "sample_seed", "expected": "164"})
    recomputed = _raw_sample(
        conn, start, end, bool(migration.get("event_set_complete"))
    )
    actual_members = {
        (row["stock_code"], row["trade_date"], row["classification"]) for row in sample
    }
    expected_members = {
        (row["stock_code"], row["trade_date"], row["classification"]) for row in recomputed
    }
    if actual_members != expected_members:
        failures.append({
            "field": "sample_membership",
            "missing": len(expected_members - actual_members),
            "unexpected": len(actual_members - expected_members),
        })
    by_class: dict[str, set[str]] = {"event": set(), "no_event": set()}
    rows_per_stock: dict[str, int] = {}
    for row in sample:
        if row["classification"] in by_class:
            by_class[row["classification"]].add(row["stock_code"])
        rows_per_stock[row["stock_code"]] = rows_per_stock.get(row["stock_code"], 0) + 1
    bad_rows = {code: count for code, count in rows_per_stock.items() if count != 5}
    if bad_rows:
        failures.append({"field": "sample_rows_per_stock", "counts": bad_rows})
    metrics = {
        "event_stocks": len(by_class["event"]),
        "no_event_stocks": len(by_class["no_event"]),
        "rows_per_stock": 5,
        "seed": "164",
    }
    for classification in ("event", "no_event"):
        eligible = set(_class_codes(conn, start, end, classification)) - golden_codes
        expected = min(len(eligible), 100)
        if len(by_class[classification]) != expected:
            failures.append({
                "field": "sample_quota", "classification": classification,
                "expected": expected, "actual": len(by_class[classification]),
            })
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              array_agg(DISTINCT CASE WHEN left(stock_code,1)='6' THEN 'sh'
                   WHEN left(stock_code,1) IN ('4','8','9') THEN 'bse' ELSE 'sz' END),
              array_agg(DISTINCT left(trade_date,4)),
              array_agg(DISTINCT CASE WHEN close<10 THEN 'lt10'
                   WHEN close<100 THEN '10_100' ELSE 'gte100' END),
              array_agg(DISTINCT source)
            FROM stock_daily_raw
            WHERE trade_date BETWEEN %s AND %s AND close IS NOT NULL AND close>0
              AND NOT stock_code=ANY(%s)
            """,
            (start, end, sorted(golden_codes)),
        )
        expected_strata = cur.fetchone()
    actual_strata = [
        {row["exchange"] for row in sample},
        {row["price_year"] for row in sample},
        {row["price_bucket"] for row in sample},
        {row["source"] for row in sample},
    ]
    names = ("exchange", "year", "price_bucket", "source")
    missing = {
        name: sorted((set(expected or []) - actual))
        for name, expected, actual in zip(names, expected_strata, actual_strata)
        if set(expected or []) - actual
    }
    if missing:
        failures.append({"field": "sample_strata", "missing": missing})
    metrics["strata"] = names
    return failures, metrics


def _check_indicator_regression(conn, migration_dir: str, start: str, end: str,
                                migration: dict) -> dict:
    try:
        with open(os.path.join(migration_dir, "raw_sample.csv"), encoding="utf-8", newline="") as handle:
            sample = list(csv.DictReader(handle))
    except OSError as exc:
        return {
            "id": "indicator_regression", "status": "error",
            "failures": [{"field": "raw_sample.csv", "error": str(exc)}],
        }
    codes = sorted({row["stock_code"] for row in sample})
    rows = _indicator_rows(conn, codes, start, end)
    keys = {(row["stock_code"], row["trade_date"]) for row in sample}
    failures, comparisons = _compare_indicators(rows, keys)
    golden_path = resources.files("qstock_mcp").joinpath("data/adjustment_golden.json")
    golden_cases = json.loads(golden_path.read_text(encoding="utf-8"))["cases"]
    golden_codes = {case["stock_code"] for case in golden_cases}
    contract_failures, sample_metrics = _sample_contract(
        conn, sample, migration, start, end, golden_codes
    )
    failures.extend(contract_failures)
    diagnostics, unexplained = _legacy_diagnostics(
        conn, rows, keys, start, end, migration_dir
    )
    failures.extend(
        {
            "stock_code": row["stock_code"], "trade_date": row["trade_date"],
            "field": "unexplained_legacy_difference", "fields": row["fields"],
        }
        for row in diagnostics if not row["explained_by_event"]
    )
    expanded_to = []
    expanded_rows = 0
    if failures:
        for classification in sorted({
            row["classification"] for row in sample
            if (row["stock_code"], row["trade_date"]) in {
                (failure["stock_code"], failure["trade_date"]) for failure in failures
                if failure.get("trade_date")
            }
        }):
            all_codes = _class_codes(conn, start, end, classification)
            all_rows = _indicator_rows(conn, all_codes, start, end)
            all_keys = _class_raw_keys(conn, all_codes, start, end)
            expanded_failures, _ = _compare_indicators(all_rows, all_keys)
            _, expanded_legacy = _legacy_diagnostics(
                conn, all_rows, all_keys, start, end, migration_dir
            )
            expanded_to.append({
                "classification": classification, "stocks": len(all_codes),
                "rows": len(all_keys), "failure_count": len(expanded_failures),
                "legacy_unexplained": expanded_legacy,
            })
            expanded_rows += len(all_keys)
    golden_checked = 0
    golden_skipped = 0
    for case in golden_cases:
        if "indicators" not in case:
            continue
        wanted = case["indicators"]
        golden_rows = _indicator_rows(conn, [case["stock_code"]], start, wanted["date"])
        golden_row = next(
            (row for row in golden_rows if row["trade_date"] == wanted["date"]), None
        )
        if golden_row is None:
            golden_skipped += 1
            continue
        golden_checked += 1
        actual = {
            "qfq_close": golden_row["qfq_close"],
            "high_60": golden_row["high_60"], "high_52w": golden_row["high_52w"],
            "low_52w": golden_row["low_52w"],
            "is_60d_high": golden_row["qfq_close"] >= golden_row["high_60"],
        }
        _compare_golden_values(
            failures, case, actual,
            {key: value for key, value in wanted.items() if key != "date"},
        )
    golden_sample_rows = sum(row["stock_code"] in golden_codes for row in sample)
    if golden_sample_rows:
        failures.append({
            "field": "golden_sample_quota", "count": golden_sample_rows,
            "error": "Golden 不得占普通样本名额；请零上游重建抽样产物",
        })
    return {
        "id": "indicator_regression",
        "status": "ok" if not failures else "error",
        "sample_stocks": len(codes),
        "sample_rows": len(sample),
        "sample_contract": sample_metrics,
        "window_comparisons": comparisons,
        "golden_cases_checked": golden_checked,
        "golden_cases_skipped": golden_skipped,
        "golden_sample_rows": golden_sample_rows,
        "expanded_to": expanded_to,
        "expanded_rows_checked": expanded_rows,
        "legacy_parity_hard": False,
        "legacy_diagnostics": diagnostics[:100],
        "unexplained_significant_differences": unexplained,
        "failure_count": len(failures),
        "failures": failures[:100],
        "failures_truncated": len(failures) > 100,
    }


def _check_structure_coverage(conn, start: str, end: str, migration: dict,
                              migration_dir: str) -> dict:
    validation = _validation(conn, start, end, bool(migration.get("event_set_complete")))
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH duplicate_keys AS (
                SELECT stock_code, trade_date FROM stock_daily_raw
                WHERE trade_date BETWEEN %s AND %s
                GROUP BY stock_code, trade_date HAVING count(*)>1
                UNION ALL
                SELECT stock_code, ex_date FROM stock_adjust_factor
                WHERE ex_date BETWEEN %s AND %s
                GROUP BY stock_code, ex_date HAVING count(*)>1
                UNION ALL
                SELECT stock_code, trade_date FROM stock_daily_hfq
                WHERE trade_date BETWEEN %s AND %s
                GROUP BY stock_code, trade_date HAVING count(*)>1
            ), event_alignment AS (
                SELECT e.stock_code, e.ex_date
                FROM stock_adjust_factor e
                LEFT JOIN stock_daily_raw r
                  ON r.stock_code=e.stock_code AND r.trade_date=e.ex_date
                  AND r.close IS NOT NULL AND r.close>0
                LEFT JOIN stock_daily_hfq h
                  ON h.stock_code=e.stock_code AND h.trade_date=e.ex_date
                LEFT JOIN stock_factor_state s ON s.stock_code=e.stock_code
                WHERE e.ex_date BETWEEN %s AND %s
                  AND (r.stock_code IS NULL OR h.stock_code IS NULL OR s.stock_code IS NULL)
            )
            SELECT
                (SELECT count(*) FROM duplicate_keys),
                (SELECT count(*) FROM stock_adjust_factor
                 WHERE ex_date BETWEEN %s AND %s
                   AND (factor_cum<=0 OR factor_cum>='Infinity'::numeric)),
                (SELECT count(*) FROM stock_daily_hfq
                 WHERE trade_date BETWEEN %s AND %s
                   AND (factor_cum<=0 OR factor_cum>='Infinity'::numeric)),
                (SELECT count(*) FROM stock_factor_state
                 WHERE (baseline_factor IS NULL) <> (factor_latest IS NULL)
                    OR (NOT rebuild_pending AND baseline_factor IS NULL)
                    OR (baseline_factor IS NOT NULL AND
                        (baseline_factor<=0 OR baseline_factor>='Infinity'::numeric))
                    OR (factor_latest IS NOT NULL AND
                        (factor_latest<=0 OR factor_latest>='Infinity'::numeric))),
                (SELECT count(*) FROM event_alignment),
                (SELECT count(*) FROM stock_daily_raw r
                 WHERE r.trade_date BETWEEN %s AND %s AND r.close IS NOT NULL AND r.close>0
                   AND EXISTS (SELECT 1 FROM stock_factor_state s
                               WHERE s.stock_code=r.stock_code AND NOT s.rebuild_pending)
                   AND NOT EXISTS (SELECT 1 FROM stock_daily_hfq h
                                   WHERE h.stock_code=r.stock_code
                                     AND h.trade_date=r.trade_date)),
                (SELECT count(DISTINCT trade_date) FROM stock_daily_raw
                 WHERE trade_date BETWEEN %s AND %s),
                (SELECT count(*) FROM (
                    SELECT DISTINCT ON (trade_date) trade_date, status
                    FROM collection_log
                    WHERE section='stock_adjust_factor' AND trade_date BETWEEN %s AND %s
                      AND detail LIKE %s
                    ORDER BY trade_date, id DESC
                 ) latest_factor_days WHERE status IN ('ok','empty')),
                (SELECT count(*) FROM stock_factor_state s
                 WHERE s.baseline_factor IS NOT NULL AND s.factor_latest IS NOT NULL
                   AND abs(s.factor_latest - coalesce((
                       SELECT e.factor_cum FROM stock_adjust_factor e
                       WHERE e.stock_code=s.stock_code
                       ORDER BY e.ex_date DESC LIMIT 1
                   ), s.baseline_factor) / s.baseline_factor)
                       > 1e-6 * greatest(abs(s.factor_latest), 1))
            """,
            (
                start, end, start, end, start, end, start, end,
                start, end, start, end, start, end, start, end, start, end,
                f"adj-backfill event {start}~{end}%",
            ),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT trade_date, count(*) FROM stock_daily_raw"
            " WHERE trade_date BETWEEN %s AND %s GROUP BY trade_date",
            (start, end),
        )
        raw_day_counts = dict(cur.fetchall())
        cur.execute(
            "SELECT stock_code, latest_trade_date FROM stock_factor_state"
            " WHERE baseline_factor IS NULL AND factor_latest IS NULL AND rebuild_pending"
            " ORDER BY stock_code"
        )
        unknown_states = cur.fetchall()
    with open(os.path.join(migration_dir, "raw_days.csv"), encoding="utf-8", newline="") as handle:
        raw_days = list(csv.DictReader(handle))
    raw_day_failures = [
        {
            "trade_date": day["trade_date"], "field": "raw_day_rows",
            "expected": int(day["rows"]), "actual": raw_day_counts.get(day["trade_date"], 0),
        }
        for day in raw_days
        if start <= day["trade_date"] <= end and day["status"] in ("ok", "skipped")
        and int(day["rows"]) != raw_day_counts.get(day["trade_date"], 0)
    ]
    from .price_reader import NewPriceReader
    coverage_failures = []
    reader = NewPriceReader()
    for code, latest in unknown_states:
        result = reader.query_daily(conn, code, "none", latest, latest)
        if result["adjust"]["coverage"] != "partial":
            coverage_failures.append({
                "stock_code": code, "trade_date": latest, "field": "coverage",
                "expected": "partial", "actual": result["adjust"]["coverage"],
            })
    metrics = {
        "primary_key_duplicates": row[0],
        "invalid_event_factors": row[1],
        "invalid_hfq_factors": row[2],
        "state_shape_errors": row[3],
        "event_alignment_errors": row[4],
        "valid_raw_without_hfq_nonpending": row[5],
        "raw_days_covered": row[6],
        "factor_days_covered": row[7],
        "raw_day_row_mismatches": len(raw_day_failures),
        "state_factor_latest_mismatches": row[8],
    }
    blocking = {
        key: value for key, value in {**validation, **metrics}.items()
        if key in (
            "pending_states", "hfq_without_valid_raw", "state_without_raw",
            "classification_mismatches", "primary_key_duplicates",
            "invalid_event_factors", "invalid_hfq_factors", "state_shape_errors",
            "event_alignment_errors", "valid_raw_without_hfq_nonpending",
            "raw_day_row_mismatches", "state_factor_latest_mismatches",
        ) and value
    }
    if coverage_failures:
        blocking["coverage"] = len(coverage_failures)
    expected_days = migration.get("trading_days")
    if metrics["raw_days_covered"] != expected_days:
        blocking["raw_date_coverage"] = metrics["raw_days_covered"]
    if metrics["factor_days_covered"] != expected_days:
        blocking["factor_date_coverage"] = metrics["factor_days_covered"]
    return {
        "id": "structure_coverage",
        "status": "ok" if not blocking else "error",
        **validation,
        **metrics,
        "unknown_baseline_coverage": "partial",
        "unknown_baseline_cases_checked": len(unknown_states),
        "coverage_failures": coverage_failures,
        "raw_day_failures": raw_day_failures[:100],
        "blocking": blocking,
        "failures": [
            {"field": field, "count": count} for field, count in blocking.items()
        ] + raw_day_failures[:100] + coverage_failures[:100],
    }


def _event_values(rows: list[dict]) -> dict[tuple[str, str], tuple[str, str]]:
    return {
        (row["stock_code"], row["ex_date"]): (
            format(Decimal(str(row["factor_cum"])).normalize(), "f"), row["source"]
        )
        for row in rows
    }


def _event_digest(values: dict[tuple[str, str], tuple[str, str]]) -> str:
    text = "\n".join(
        f"{code}|{date}|{factor}|{source}"
        for (code, date), (factor, source) in sorted(values.items())
    )
    return _hash_text(text)


def _check_event_reconciliation(conn, migration_dir: str, start: str, end: str,
                                migration: dict) -> dict:
    try:
        with open(os.path.join(migration_dir, "events.csv"), encoding="utf-8", newline="") as handle:
            snapshot = _event_values(list(csv.DictReader(handle)))
        with open(os.path.join(migration_dir, "repairs.csv"), encoding="utf-8", newline="") as handle:
            repairs = list(csv.DictReader(handle))
    except OSError as exc:
        return {
            "id": "event_reconciliation", "status": "error",
            "failures": [{"field": "migration_artifact", "error": str(exc)}],
        }
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, ex_date, factor_cum, source FROM stock_adjust_factor"
            " WHERE ex_date BETWEEN %s AND %s",
            (start, end),
        )
        local = _event_values([
            {"stock_code": row[0], "ex_date": row[1], "factor_cum": row[2], "source": row[3]}
            for row in cur.fetchall()
        ])
    failures: list[dict] = []
    for key in sorted(set(snapshot) | set(local)):
        if snapshot.get(key) != local.get(key):
            failures.append({
                "stock_code": key[0], "event_date": key[1], "field": "factor_cum/source",
                "snapshot": snapshot.get(key), "local": local.get(key),
            })
    if not migration.get("event_set_complete"):
        failures.append({"field": "event_set_complete", "error": "事件日集合不完整"})
    reconciliation_errors = sum(
        row.get("reason") in ("factor_mismatch", "price_path_unverified")
        for row in repairs
    )
    if reconciliation_errors:
        failures.append({
            "field": "pctChg_reconciliation", "count": reconciliation_errors,
        })
    return {
        "id": "event_reconciliation",
        "status": "ok" if not failures else "error",
        "snapshot_rows": len(snapshot),
        "local_rows": len(local),
        "snapshot_stocks": len({key[0] for key in snapshot}),
        "local_stocks": len({key[0] for key in local}),
        "snapshot_hash": _event_digest(snapshot),
        "local_hash": _event_digest(local),
        "relative_tolerance": 0.001,
        "reconciliation_errors": reconciliation_errors,
        "failure_count": len(failures),
        "failures": failures[:100],
        "failures_truncated": len(failures) > 100,
    }


def _revision_scenarios(conn) -> tuple[dict, str | None]:
    from .adjustment import rebuild_stock

    scenarios = {
        "raw_revision": "not_run", "factor_revision": "not_run",
        "event_retraction": "not_run", "repeat_hash_stable": "not_run",
    }
    try:
        with conn.cursor() as cur:
            for table in (
                "stock_daily_raw", "stock_adjust_factor", "stock_daily_hfq",
                "stock_factor_state", "collection_log",
            ):
                cur.execute(
                    f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL)"
                )
            cur.execute("SET search_path TO pg_temp, public")
        code = "acceptance"
        raw = [
            {
                "trade_date": day, "open": close, "high": close, "low": close,
                "close": close, "preclose": close, "volume": 100,
                "amount": 10000.0, "change_percent": 0.0, "source": "fixture",
            }
            for day, close in (("20240102", 10.0), ("20240103", 11.0), ("20240104", 12.0))
        ]
        rebuild_stock(
            conn, code, raw, [{"ex_date": "20240103", "factor_cum": 2.0}],
            baseline_factor=1.0,
        )
        revised_raw = {**raw[2], "close": 13.0, "open": 13.0, "high": 13.0, "low": 13.0}
        raw_revision = rebuild_stock(conn, code, [revised_raw], [], baseline_factor=1.0)
        assert raw_revision["hfq_rows"][-1]["close"] == 26.0
        assert raw_revision["qfq_rows"][-1]["close"] == 13.0
        scenarios["raw_revision"] = "ok"
        factor_revision = rebuild_stock(
            conn, code, [], [{"ex_date": "20240103", "factor_cum": 4.0}],
            authoritative_event_dates=("20240103",), baseline_factor=1.0,
        )
        assert factor_revision["state"]["factor_latest"] == 4.0
        assert factor_revision["hfq_rows"][1]["close"] == 44.0
        scenarios["factor_revision"] = "ok"
        retracted = rebuild_stock(
            conn, code, [], [], authoritative_event_dates=("20240103",)
        )
        assert retracted["factor_events"] == []
        assert [row["close"] for row in retracted["qfq_rows"]] == [10.0, 11.0, 13.0]
        scenarios["event_retraction"] = "ok"
        repeated = rebuild_stock(
            conn, code, [], [], authoritative_event_dates=("20240103",)
        )
        keys = ("factor_events", "hfq_rows", "qfq_rows", "state")
        assert all(repeated[key] == retracted[key] for key in keys)
        scenarios["repeat_hash_stable"] = "ok"
        return scenarios, None
    except Exception as exc:  # noqa: BLE001 - 验收场景失败转为报告
        return scenarios, str(exc)
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                for table in (
                    "stock_daily_raw", "stock_adjust_factor", "stock_daily_hfq",
                    "stock_factor_state", "collection_log",
                ):
                    cur.execute(f"DROP TABLE IF EXISTS pg_temp.{table}")
            conn.commit()
        except Exception:  # noqa: BLE001 - 连接后续不再使用
            conn.rollback()


def _check_hash_idempotency(conn, migration_dir: str, start: str, end: str,
                            migration: dict) -> dict:
    began = monotonic()
    baseline_path = os.path.join(migration_dir, "stock_hashes.csv")
    try:
        with open(baseline_path, encoding="utf-8", newline="") as handle:
            baseline = {row["stock_code"]: row for row in csv.DictReader(handle)}
    except OSError as exc:
        return {
            "id": "hash_idempotency", "status": "error",
            "failures": [{"field": "stock_hashes.csv", "error": str(exc)}],
        }
    current = {
        row["stock_code"]: row for row in _stock_hashes(conn, start, end)
    }
    differences = []
    for code in sorted(set(baseline) | set(current)):
        before = baseline.get(code, {})
        after = current.get(code, {})
        for table in ("raw", "event", "hfq", "state"):
            for kind in ("rows", "hash"):
                field = f"{table}_{kind}"
                if str(before.get(field, "")) != str(after.get(field, "")):
                    differences.append({
                        "stock_code": code, "field": field,
                        "before": before.get(field, ""), "after": after.get(field, ""),
                    })
    scenarios, scenario_error = _revision_scenarios(conn)
    if scenario_error:
        differences.append({"field": "revision_scenarios", "error": scenario_error})
    duration = monotonic() - began
    resume_zero = migration.get("upstream_requests", {}).get("total") == 0
    if not resume_zero:
        differences.append({
            "field": "resume", "error": "迁移摘要不是零上游请求的断点续跑结果"
        })
    budget_s = 600
    if duration > budget_s:
        differences.append({
            "field": "performance", "duration_s": duration, "budget_s": budget_s,
        })
    return {
        "id": "hash_idempotency",
        "status": "ok" if not differences else "error",
        "stocks_compared": len(set(baseline) | set(current)),
        "tables": ["raw", "event", "hfq", "state"],
        "differences": differences[:100],
        "failure_count": len(differences),
        "differences_truncated": len(differences) > 100,
        "resume_zero_upstream": resume_zero,
        "revision_resume_aligned": not differences and migration.get("event_set_complete") is True,
        "revision_scenarios": scenarios,
        "duration_s": round(duration, 3),
        "performance_budget_s": budget_s,
        "failures": differences[:100],
    }


def _compare_golden_values(failures: list[dict], case: dict, actual: dict,
                           expected: dict) -> None:
    for field, wanted in expected.items():
        value = actual.get(field)
        passed = value == wanted
        if isinstance(wanted, (int, float)) and not isinstance(wanted, bool):
            passed = value is not None and _near(value, wanted)
        if not passed:
            failures.append({
                "stock_code": case["stock_code"], "trade_date": case.get("date"),
                "event_date": case.get("date"), "field": field,
                "expected": wanted, "actual": value,
            })


def _check_recorded_golden(case: dict, vector: dict) -> list[dict]:
    failures: list[dict] = []
    event_factor = float(vector["previous_b"]) / float(vector["current_b"])
    factor_f = float(vector.get(
        "normalized_b", float(vector["current_b"]) / float(vector["baseline_factor"])
    ))
    actual = {
        "event_factor": event_factor,
        "factor_f": factor_f,
        "previous_hfq": vector["previous_raw"],
        "hfq_close": float(vector["raw_close"]) * factor_f,
        "previous_qfq": float(vector["previous_raw"]) / factor_f,
        "qfq_close": float(vector["raw_close"]),
    }
    expected = {
        key: vector[key] for key in (
            "event_factor", "factor_f", "previous_hfq", "hfq_close",
            "previous_qfq", "qfq_close",
        ) if key in vector
    }
    _compare_golden_values(failures, case, actual, expected)
    return failures


def _golden_raw_neighbor(conn, case: dict, neighbor: dict | None,
                         failures: list[dict]) -> None:
    if neighbor is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM stock_daily_raw WHERE stock_code=%s AND trade_date<%s LIMIT 1",
                (case["stock_code"], case["date"]),
            )
            if cur.fetchone():
                failures.append({
                    "stock_code": case["stock_code"], "trade_date": case["date"],
                    "field": "D-1", "error": "expected no previous raw",
                })
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT close FROM stock_daily_raw WHERE stock_code=%s AND trade_date=%s",
            (case["stock_code"], neighbor["date"]),
        )
        row = cur.fetchone()
    _compare_golden_values(
        failures, {**case, "date": neighbor["date"]},
        {"raw_close": row[0] if row else None}, {"raw_close": neighbor["raw_close"]},
    )


def _check_database_golden(conn, case: dict) -> list[dict]:
    failures: list[dict] = []
    code, date = case["stock_code"], case["date"]
    with conn.cursor() as cur:
        if case["type"] == "excluded":
            counts = []
            for table in (
                "stock_daily_raw", "stock_adjust_factor", "stock_daily_hfq", "stock_factor_state"
            ):
                cur.execute(f"SELECT count(*) FROM {table} WHERE stock_code=%s", (code,))
                counts.append(cur.fetchone()[0])
            if any(counts):
                failures.append({"stock_code": code, "field": "bse_exclusion", "actual": counts})
            return failures
        if case["type"] == "database_event":
            cur.execute(
                """
                SELECT e.factor_cum, r.close, r.preclose, r.change_percent, r.tradestatus,
                       h.factor_cum, h.close, s.baseline_factor, s.factor_latest,
                       s.rebuild_pending
                FROM stock_adjust_factor e
                LEFT JOIN stock_daily_raw r
                  ON r.stock_code=e.stock_code AND r.trade_date=e.ex_date
                LEFT JOIN stock_daily_hfq h
                  ON h.stock_code=e.stock_code AND h.trade_date=e.ex_date
                LEFT JOIN stock_factor_state s ON s.stock_code=e.stock_code
                WHERE e.stock_code=%s AND e.ex_date=%s
                """,
                (code, date),
            )
        else:
            cur.execute(
                """
                SELECT NULL, r.close, r.preclose, r.change_percent, r.tradestatus,
                       h.factor_cum, h.close, s.baseline_factor, s.factor_latest,
                       s.rebuild_pending, s.latest_trade_date
                FROM stock_daily_raw r
                LEFT JOIN stock_daily_hfq h
                  ON h.stock_code=r.stock_code AND h.trade_date=r.trade_date
                LEFT JOIN stock_factor_state s ON s.stock_code=r.stock_code
                WHERE r.stock_code=%s AND r.trade_date=%s
                """,
                (code, date),
            )
        row = cur.fetchone()
    if row is None:
        return [{"stock_code": code, "trade_date": date, "field": "golden_row", "error": "missing"}]
    columns = (
        "factor_cum", "raw_close", "preclose", "change_percent", "tradestatus",
        "factor_f", "hfq_close", "baseline_factor", "factor_latest", "pending",
    )
    actual = dict(zip(columns, row))
    if case["type"] != "database_event":
        actual["latest_trade_date"] = row[10]
    _compare_golden_values(failures, case, actual, case["expected"])
    if "previous" in case:
        _golden_raw_neighbor(conn, case, case["previous"], failures)
    if "next" in case:
        _golden_raw_neighbor(conn, case, case["next"], failures)
    for special, field in (("equal_b_non_event", "equal_b"), ("invalid_close", "invalid_close")):
        if special not in case:
            continue
        wanted = case[special]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.close, r.tradestatus, h.factor_cum, h.close,
                       s.latest_trade_date,
                       EXISTS(SELECT 1 FROM stock_adjust_factor e
                              WHERE e.stock_code=r.stock_code AND e.ex_date=r.trade_date)
                FROM stock_daily_raw r
                LEFT JOIN stock_daily_hfq h
                  ON h.stock_code=r.stock_code AND h.trade_date=r.trade_date
                LEFT JOIN stock_factor_state s ON s.stock_code=r.stock_code
                WHERE r.stock_code=%s AND r.trade_date=%s
                """,
                (code, wanted["date"]),
            )
            special_row = cur.fetchone()
        if special_row is None:
            failures.append({"stock_code": code, "trade_date": wanted["date"], "field": field})
            continue
        special_actual = {
            "raw_close": special_row[0], "tradestatus": special_row[1],
            "factor_f": special_row[2], "hfq_close": special_row[3],
            "latest_trade_date": special_row[4],
            "event_absent": not special_row[5], "hfq_absent": special_row[3] is None,
        }
        _compare_golden_values(
            failures, {**case, "date": wanted["date"]}, special_actual,
            {key: value for key, value in wanted.items() if key != "date"},
        )
        if special == "equal_b_non_event" and not special_actual["event_absent"]:
            failures.append({"stock_code": code, "trade_date": wanted["date"], "field": "equal_b_event"})
    return failures


def _check_golden_set(conn) -> dict:
    path = resources.files("qstock_mcp").joinpath("data/adjustment_golden.json")
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    failures: list[dict] = []
    recorded_failures: list[dict] = []
    recorded_checked = 0
    for case in cases:
        if case["type"] == "recorded_event":
            recorded_checked += 1
            recorded_failures.extend(_check_recorded_golden(case, case["recorded"]))
        else:
            if "recorded_normalized" in case:
                recorded_checked += 1
                recorded_failures.extend(
                    _check_recorded_golden(case, case["recorded_normalized"])
                )
            failures.extend(_check_database_golden(conn, case))
    failures.extend(recorded_failures)
    return {
        "id": "golden_set",
        "status": "ok" if not failures else "error",
        "cases": len(cases),
        "required_coverage": [case["stock_code"] for case in cases],
        "boundaries": sorted({case["boundary"] for case in cases}),
        "recorded_cases_checked": recorded_checked,
        "recorded_failures": recorded_failures,
        "failure_count": len(failures),
        "failures": failures[:100],
        "failures_truncated": len(failures) > 100,
    }


def _nonnegative_finite(value) -> bool:
    return value is None or math.isfinite(float(value)) and float(value) >= 0


def _check_upstream_smoke(adapter, days: int, end: str | None,
                          throttle: float | None) -> dict:
    began = monotonic()
    start, last = trading_day_window(days, end=end)
    days_in_window = trading_days_between(start, last)
    pacer = _Pacer(ADJ_BACKFILL_THROTTLE_S if throttle is None else throttle)
    failures: list[dict] = []
    empty_factor_days = []
    empty_raw_days = []
    factor_requests = 0
    raw_requests = 0
    for trade_date in days_in_window:
        pacer.before_request()
        factor_requests += 1
        try:
            events = adapter.fetch_daily_adjust_factor(trade_date)
            if not events:
                empty_factor_days.append(trade_date)
            for event in events:
                if (
                    not event.get("stock_code")
                    or not math.isfinite(float(event["factor_cum"]))
                    or float(event["factor_cum"]) <= 0
                ):
                    failures.append({
                        "trade_date": trade_date, "field": "backAdjustFactor",
                        "stock_code": event.get("stock_code"),
                    })
        except BaostockError:
            raise
        except Exception as exc:  # noqa: BLE001 - smoke 收敛逐日上游失败
            failures.append({"trade_date": trade_date, "field": "factor", "error": str(exc)})
        pacer.before_request()
        raw_requests += 1
        try:
            raw = adapter.fetch_astock_daily(trade_date)
            if not raw:
                empty_raw_days.append(trade_date)
            codes = set()
            for row in raw:
                code = row.get("stock_code")
                if (
                    not code or code in codes or row.get("trade_date") != trade_date
                    or row.get("adj") not in (None, "none")
                    or not _nonnegative_finite(row.get("amount"))
                    or not _nonnegative_finite(row.get("volume"))
                ):
                    failures.append({
                        "trade_date": trade_date, "stock_code": code,
                        "field": "raw/date/adj/amount/volume",
                    })
                codes.add(code)
        except BaostockError:
            raise
        except Exception as exc:  # noqa: BLE001
            failures.append({"trade_date": trade_date, "field": "raw", "error": str(exc)})
    return {
        "id": "upstream_smoke",
        "status": "ok" if not failures else "error",
        "days": days_in_window,
        "empty_factor_days": empty_factor_days,
        "empty_raw_days": empty_raw_days,
        "unit_checks": ["amount_wan", "volume_shares"],
        "timeout_policy": "adapter_60s",
        "resume_evidence": "hash_idempotency",
        "upstream_requests": {
            "factor": factor_requests, "raw": raw_requests,
            "total": factor_requests + raw_requests,
        },
        "write_policy": "read_only",
        "duration_s": round(monotonic() - began, 3),
        "failure_count": len(failures),
        "failures": failures,
    }


def _write_acceptance_artifacts(report_dir: str, result: dict) -> dict:
    os.makedirs(report_dir, exist_ok=True)
    paths = {
        "summary": os.path.join(report_dir, "acceptance_summary.json"),
        "failures": os.path.join(report_dir, "acceptance_failures.csv"),
        "indicator_diagnostics": os.path.join(report_dir, "indicator_diagnostics.csv"),
    }
    result["artifacts"] = paths
    with open(paths["summary"], "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)
    failure_fields = [
        "check_id", "stock_code", "trade_date", "event_date", "field",
        "error", "expected", "actual", "detail",
    ]
    with open(paths["failures"], "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=failure_fields, extrasaction="ignore")
        writer.writeheader()
        for check in result["checks"]:
            for failure in check.get("failures", []):
                writer.writerow({
                    key: json.dumps(value, ensure_ascii=False, default=str)
                    if isinstance(value, (list, dict)) else value
                    for key, value in {"check_id": check["id"], **failure}.items()
                })
    indicator = next(
        check for check in result["checks"] if check["id"] == "indicator_regression"
    )
    diagnostics = indicator.get("legacy_diagnostics", [])
    diagnostic_fields = [
        "stock_code", "trade_date", "field", "fields", "legacy_close", "new_close",
        "legacy_high_52w", "new_high_52w", "legacy_low_52w", "new_low_52w",
        "relative_difference", "nearest_event", "explained_by_event",
        "review_reason", "review_event",
    ]
    with open(paths["indicator_diagnostics"], "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=diagnostic_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(diagnostics)
    return paths


def run_adjustment_acceptance(
    migration_dir: str,
    *,
    adapter=None,
    smoke_days: int | None = None,
    smoke_end: str | None = None,
    report_dir: str | None = None,
    throttle: float | None = None,
) -> dict:
    began = monotonic()
    tool = "adj-acceptance"
    params = {
        "migration_dir": migration_dir, "smoke_days": smoke_days,
        "smoke_end": smoke_end,
    }
    if smoke_days is not None and smoke_days not in (3, 4, 5):
        return _error(tool, params, "上游 smoke 必须限 3–5 个连续交易日")
    summary_path = os.path.join(migration_dir, "summary.json")
    try:
        with open(summary_path, encoding="utf-8") as handle:
            migration = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return _error(tool, params, f"无法读取迁移摘要: {exc}")

    window = migration.get("window") or {}
    if not window.get("start") or not window.get("end"):
        return _error(tool, params, "迁移摘要缺少 window.start/window.end")
    conn, err = connect()
    if err:
        return _error(tool, params, err)
    try:
        completed = {
            "factor_baseline": _check_factor_baseline(
                conn, window["start"], window["end"]
            ),
            "event_invariants": _check_event_invariants(
                conn, window["start"], window["end"]
            ),
            "structure_coverage": _check_structure_coverage(
                conn, window["start"], window["end"], migration, migration_dir
            ),
            "indicator_regression": _check_indicator_regression(
                conn, migration_dir, window["start"], window["end"], migration
            ),
            "event_reconciliation": _check_event_reconciliation(
                conn, migration_dir, window["start"], window["end"], migration
            ),
            "golden_set": _check_golden_set(conn),
            "hash_idempotency": _check_hash_idempotency(
                conn, migration_dir, window["start"], window["end"], migration
            ),
        }
    finally:
        conn.close()
    smoke: dict = {"id": "upstream_smoke", "status": "not_run"}
    if smoke_days is not None:
        if adapter is None:
            from .adapters.baostock_adapter import BaostockAdapter
            adapter = BaostockAdapter()
        try:
            smoke = _check_upstream_smoke(adapter, smoke_days, smoke_end, throttle)
        except BaostockError as exc:
            return baostock_error(tool, params, exc)
    completed["upstream_smoke"] = smoke
    checks = [
        completed.get(check_id, {"id": check_id, "status": "pending"})
        for check_id in CHECK_IDS
    ]
    passed = sum(check["status"] == "ok" for check in checks)
    failed = sum(check["status"] == "error" for check in checks)
    complete = passed == len(CHECK_IDS)
    upstream_total = smoke.get("upstream_requests", {}).get("total", 0)
    result = {
        "status": "ok" if complete else "partial_error",
        "stage": "verified" if complete else "rebuilt",
        "tool": tool,
        "params": {**params, "report_dir": report_dir},
        "window": migration.get("window"),
        "checks": checks,
        "summary": {
            "passed": passed,
            "failed": failed,
            "not_run": sum(check["status"] == "not_run" for check in checks),
            "upstream_requests": {"total": upstream_total},
            "duration_s": round(monotonic() - began, 3),
            "performance_budget_s": 600,
        },
        "messages": ["数据处理完成"] + (["数据可以使用"] if complete else []),
    }
    _write_acceptance_artifacts(
        report_dir or os.path.join(migration_dir, "acceptance"), result
    )
    return result
