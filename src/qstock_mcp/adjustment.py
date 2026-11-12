"""单票复权竖切：raw 与累计 B 事实 upsert，重建 hfq/state 并派生 qfq。"""

from decimal import Decimal

from psycopg2.extras import execute_values  # type: ignore[import-untyped]

from .adjustment_lock import lock_adjustment_facts
from .dates import prev_trading_day

_RAW_FIELDS = (
    "trade_date", "open", "high", "low", "close", "preclose", "volume",
    "amount", "amplitude", "change_percent", "change_amount", "turnover_rate",
    "tradestatus", "source",
)
_HFQ_FIELDS = (
    "trade_date", "open", "high", "low", "close", "volume", "amount",
    "turnover_rate", "tradestatus", "factor_cum",
)


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def event_factor_from_pctchg(previous_close, close, pct) -> Decimal | None:
    """权威 pctChg 价格路径单次因子；不猜、不夹逼。"""
    if pct is None or close is None or previous_close is None:
        return None
    if close <= 0 or previous_close <= 0:
        return None
    realclose = _decimal(previous_close) * (1 + _decimal(pct) / 100)
    if realclose <= 0:
        return None
    factor = _decimal(close) / realclose
    return factor if factor > 0 else None


def factor_relative_error(price_factor: Decimal, authoritative: Decimal) -> Decimal:
    """双路径相对误差（0.1% 容差由调用方判定）。"""
    return abs(price_factor - authoritative) / authoritative


def _event_price_factor(raw_by_date: dict[str, dict], ex_date: str) -> Decimal | None:
    """用权威 pctChg 计算事件日价格路径单次因子（D-1 必须是确切前一交易日）。"""
    row = raw_by_date.get(ex_date)
    previous = raw_by_date.get(prev_trading_day(ex_date))
    if row is None or previous is None:
        return None
    return event_factor_from_pctchg(
        previous.get("close"), row.get("close"), row.get("change_percent")
    )


def _audit_event_factors(
    events: list[dict], raw_by_date: dict[str, dict], baseline: Decimal | None,
) -> tuple[list[dict], bool]:
    """逐事件做 B 路径与权威价格路径对账；返回 (audit, any_not_ok)。"""
    audit: list[dict] = []
    previous_b: Decimal | None = baseline
    for event in events:
        factor_cum = _decimal(event["factor_cum"])
        price_factor = _event_price_factor(raw_by_date, event["ex_date"])
        authoritative: Decimal | None
        relative_error: Decimal | None
        if previous_b is None:
            authoritative = price_factor
            status = "ok" if price_factor is not None else "unverified"
            relative_error = Decimal(0) if price_factor is not None else None
        else:
            authoritative = previous_b / factor_cum
            if price_factor is None:
                status = "unverified"
                relative_error = None
            else:
                relative_error = factor_relative_error(price_factor, authoritative)
                status = "ok" if relative_error <= Decimal("0.001") else "mismatch"
        audit.append({
            "ex_date": event["ex_date"],
            "event_factor": float(authoritative) if authoritative is not None else None,
            "price_factor": float(price_factor) if price_factor is not None else None,
            "relative_error": float(relative_error) if relative_error is not None else None,
            "status": status,
        })
        previous_b = factor_cum
    return audit, any(item["status"] != "ok" for item in audit)


def _upsert_factor_events(cur, values: list[list]) -> None:
    if not values:
        return
    execute_values(
        cur,
        """
        INSERT INTO stock_adjust_factor (stock_code, ex_date, factor_cum,
            kind, detail, reg_date, source)
        VALUES %s
        ON CONFLICT (stock_code, ex_date) DO UPDATE SET
            factor_cum=EXCLUDED.factor_cum, kind=EXCLUDED.kind,
            detail=EXCLUDED.detail, reg_date=EXCLUDED.reg_date,
            source=EXCLUDED.source, updated_at=CURRENT_TIMESTAMP
        """,
        values,
    )


def _record_quarantine(conn, trade_date: str, quarantined: list[dict]) -> None:
    if not quarantined:
        return
    from .ledger import record_collection

    record_collection(
        conn,
        trade_date=trade_date,
        section="stock_daily_raw",
        status="error",
        source="baostock",
        rows=len(quarantined),
        error_kind="invalid_close",
        detail="; ".join(
            f"{row['stock_code']}:{row['trade_date']}" for row in quarantined
        ),
    )


def rebuild_stock(
    conn,
    stock_code: str,
    raw_rows: list[dict],
    factor_events: list[dict],
    *,
    authoritative_event_dates=(),
    baseline_factor=None,
    rebuild_from: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict:
    """写入单票事实并原子重建派生；authoritative_event_dates 整日替换事件。

    window_start/window_end 是回填窗口（#163）：事件、派生与状态只按窗口内
    事实计算，窗口外 raw 仅作首个事件 D-1 计算输入，不写入 hfq。窗口模式下
    逐事件做 B 路径 vs 权威 pctChg 价格路径对账，未通过即 pending 但保留已
    建立的基线（基线本身不可验证时仍 NULL/NULL）。
    """
    def _in_window(trade_date: str) -> bool:
        return (
            (window_start is None or trade_date >= window_start)
            and (window_end is None or trade_date <= window_end)
        )

    try:
        lock_adjustment_facts(conn)
        if window_end is not None:
            # 只拒绝“库内已有”的窗口外事实；调用方显式传入的窗口外输入由下方
            # 窗口过滤处理。回填入口另有全局守卫，避免历史窗口回退生产派生。
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT max(trade_date) FROM stock_daily_raw WHERE stock_code=%s",
                    (stock_code,),
                )
                latest_raw = cur.fetchone()[0]
                cur.execute(
                    "SELECT max(ex_date) FROM stock_adjust_factor WHERE stock_code=%s",
                    (stock_code,),
                )
                latest_event = cur.fetchone()[0]
            if (latest_raw and latest_raw > window_end) or (
                latest_event and latest_event > window_end
            ):
                raise ValueError(
                    "window_end 早于库内已有复权事实，拒绝重建以免回退窗口外派生"
                )
        with conn.cursor() as cur:
            raw_values = [
                [stock_code] + [row.get(field) for field in _RAW_FIELDS[:-1]]
                + [row.get("source", "baostock")]
                for row in raw_rows
            ]
            if raw_values:
                execute_values(
                    cur,
                    """
                    INSERT INTO stock_daily_raw (stock_code, trade_date, open, high, low,
                        close, preclose, volume, amount, amplitude, change_percent,
                        change_amount, turnover_rate, tradestatus, source)
                    VALUES %s
                    ON CONFLICT (stock_code, trade_date) DO UPDATE SET
                        open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                        close=EXCLUDED.close, preclose=EXCLUDED.preclose,
                        volume=EXCLUDED.volume, amount=EXCLUDED.amount,
                        amplitude=EXCLUDED.amplitude,
                        change_percent=EXCLUDED.change_percent,
                        change_amount=EXCLUDED.change_amount,
                        turnover_rate=EXCLUDED.turnover_rate,
                        tradestatus=EXCLUDED.tradestatus, source=EXCLUDED.source,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    raw_values,
                )
            cur.execute(
                "SELECT ex_date, factor_cum FROM stock_adjust_factor"
                " WHERE stock_code=%s ORDER BY ex_date",
                (stock_code,),
            )
            known_events = [
                {"ex_date": date, "factor_cum": value} for date, value in cur.fetchall()
            ]
            filtered_events = []
            for event in sorted(factor_events, key=lambda item: item["ex_date"]):
                same_date = next(
                    (old for old in known_events if old["ex_date"] == event["ex_date"]),
                    None,
                )
                previous = next(
                    (old for old in reversed(known_events) if old["ex_date"] < event["ex_date"]),
                    None,
                )
                if same_date is None and previous is not None and _decimal(
                    previous["factor_cum"]
                ) == _decimal(event["factor_cum"]):
                    continue
                filtered_events.append(event)
                known_events = [
                    old for old in known_events if old["ex_date"] != event["ex_date"]
                ] + [event]
                known_events.sort(key=lambda item: item["ex_date"])
            event_values = [
                [
                    stock_code, event["ex_date"], event["factor_cum"],
                    event.get("kind"), event.get("detail"), event.get("reg_date"),
                    event.get("source", "baostock-adjust-factor"),
                ]
                for event in filtered_events
            ]
            _upsert_factor_events(cur, event_values)
            for ex_date in authoritative_event_dates:
                cur.execute(
                    "DELETE FROM stock_adjust_factor WHERE stock_code=%s AND ex_date=%s",
                    (stock_code, ex_date),
                )
                _upsert_factor_events(
                    cur, [value for value in event_values if value[1] == ex_date]
                )

            cur.execute(
                "SELECT trade_date, open, high, low, close, preclose, volume, amount,"
                " amplitude, change_percent, change_amount, turnover_rate, tradestatus, source"
                " FROM stock_daily_raw WHERE stock_code=%s ORDER BY trade_date",
                (stock_code,),
            )
            raw_all = [dict(zip(_RAW_FIELDS, row)) for row in cur.fetchall()]
            raw_window = [row for row in raw_all if _in_window(row["trade_date"])]
            raw_by_date = {row["trade_date"]: row for row in raw_all}
            quarantined = [
                {"stock_code": stock_code, "trade_date": row["trade_date"], "reason": "invalid_close"}
                for row in raw_window if row["close"] is None or row["close"] <= 0
            ]
            raw = [row for row in raw_window if row["close"] is not None and row["close"] > 0]
            cur.execute(
                "SELECT ex_date, factor_cum, kind, detail, reg_date, source"
                " FROM stock_adjust_factor WHERE stock_code=%s ORDER BY ex_date",
                (stock_code,),
            )
            events_all = [
                dict(zip(("ex_date", "factor_cum", "kind", "detail", "reg_date", "source"), row))
                for row in cur.fetchall()
            ]
            events = [event for event in events_all if _in_window(event["ex_date"])]

            if not raw:
                cur.execute("DELETE FROM stock_daily_hfq WHERE stock_code=%s", (stock_code,))
                cur.execute("DELETE FROM stock_factor_state WHERE stock_code=%s", (stock_code,))
                conn.commit()
                if raw_window or raw_all:
                    _record_quarantine(
                        conn, (raw_window or raw_all)[-1]["trade_date"], quarantined
                    )
                return {
                    "stock_code": stock_code,
                    "coverage": "partial",
                    "note": "无有效 raw 行",
                    "raw_rows": raw_all,
                    "factor_events": [
                        {**event, "factor_cum": float(event["factor_cum"]), "event_factor": None}
                        for event in events
                    ],
                    "factor_audit": [],
                    "hfq_rows": [],
                    "qfq_rows": [],
                    "state": None,
                    "quarantined": quarantined,
                }

            pending = False
            if baseline_factor is None:
                if events:
                    first_factor = _event_price_factor(raw_by_date, events[0]["ex_date"])
                    if first_factor is None:
                        pending = True
                        baseline = None
                    else:
                        baseline = _decimal(events[0]["factor_cum"]) * first_factor
                else:
                    baseline = Decimal(1)
            else:
                baseline = _decimal(baseline_factor)

            factor_audit, audit_not_ok = _audit_event_factors(
                events, raw_by_date, baseline
            )
            if window_start is not None and audit_not_ok:
                pending = True
            audit_by_date = {item["ex_date"]: item for item in factor_audit}
            for event in events:
                event["event_factor"] = audit_by_date[event["ex_date"]]["event_factor"]

            if baseline is None:
                latest_trade_date = raw[-1]["trade_date"]
                cur.execute("DELETE FROM stock_daily_hfq WHERE stock_code=%s", (stock_code,))
                cur.execute(
                    """
                    INSERT INTO stock_factor_state (stock_code, baseline_factor, factor_latest,
                        latest_trade_date, rebuild_pending, last_rebuilt_at)
                    VALUES (%s, NULL, NULL, %s, true, NULL)
                    ON CONFLICT (stock_code) DO UPDATE SET
                        baseline_factor=NULL, factor_latest=NULL,
                        latest_trade_date=EXCLUDED.latest_trade_date,
                        rebuild_pending=true, last_rebuilt_at=NULL
                    """,
                    (stock_code, latest_trade_date),
                )
                conn.commit()
                _record_quarantine(conn, latest_trade_date, quarantined)
                return {
                    "stock_code": stock_code,
                    "coverage": "partial",
                    "note": "首个事件缺少可信窗口基线",
                    "raw_rows": raw_all,
                    "factor_events": [
                        {**event, "factor_cum": float(event["factor_cum"])}
                        for event in events
                    ],
                    "factor_audit": factor_audit,
                    "hfq_rows": [],
                    "qfq_rows": [],
                    "state": {
                        "stock_code": stock_code,
                        "baseline_factor": None,
                        "factor_latest": None,
                        "latest_trade_date": latest_trade_date,
                        "rebuild_pending": True,
                    },
                    "quarantined": quarantined,
                }
            assert baseline is not None
            current_b = baseline
            event_by_date = {event["ex_date"]: event for event in events}

            hfq = []
            for row in raw:
                event_for_row = event_by_date.get(row["trade_date"])
                if event_for_row is not None:
                    current_b = _decimal(event_for_row["factor_cum"])
                factor = current_b / baseline
                hfq.append({
                    **{key: row.get(key) for key in _HFQ_FIELDS[:-1]},
                    "factor_cum": factor,
                    "open": float(_decimal(row["open"]) * factor) if row.get("open") is not None else None,
                    "high": float(_decimal(row["high"]) * factor) if row.get("high") is not None else None,
                    "low": float(_decimal(row["low"]) * factor) if row.get("low") is not None else None,
                    "close": float(_decimal(row["close"]) * factor),
                })

            derive_start = rebuild_from if rebuild_from is not None else window_start
            if derive_start is None and window_end is None:
                cur.execute("DELETE FROM stock_daily_hfq WHERE stock_code=%s", (stock_code,))
            elif window_end is None:
                cur.execute(
                    "DELETE FROM stock_daily_hfq WHERE stock_code=%s AND trade_date>=%s",
                    (stock_code, derive_start),
                )
            else:
                cur.execute(
                    "DELETE FROM stock_daily_hfq WHERE stock_code=%s"
                    " AND trade_date BETWEEN %s AND %s",
                    (stock_code, derive_start or "00000000", window_end),
                )
            rebuilt_hfq = hfq
            if derive_start is not None:
                rebuilt_hfq = [
                    row for row in hfq if str(row["trade_date"]) >= derive_start
                ]
            if rebuilt_hfq:
                execute_values(
                    cur,
                    """
                    INSERT INTO stock_daily_hfq (stock_code, trade_date, open, high, low,
                        close, volume, amount, turnover_rate, tradestatus, factor_cum)
                    VALUES %s
                    """,
                    [
                        [stock_code] + [row.get(field) for field in _HFQ_FIELDS[:-1]]
                        + [row["factor_cum"]]
                        for row in rebuilt_hfq
                    ],
                )
            latest_b = _decimal(events[-1]["factor_cum"]) if events else baseline
            factor_latest = latest_b / baseline
            latest_trade_date = raw[-1]["trade_date"]
            cur.execute(
                """
                INSERT INTO stock_factor_state (stock_code, baseline_factor, factor_latest,
                    latest_trade_date, rebuild_pending, last_rebuilt_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (stock_code) DO UPDATE SET
                    baseline_factor=EXCLUDED.baseline_factor,
                    factor_latest=EXCLUDED.factor_latest,
                    latest_trade_date=EXCLUDED.latest_trade_date,
                    rebuild_pending=EXCLUDED.rebuild_pending,
                    last_rebuilt_at=CURRENT_TIMESTAMP
                """,
                (stock_code, baseline, factor_latest, latest_trade_date, pending),
            )

            select_sql = (
                "SELECT trade_date, open, high, low, close, volume, amount,"
                " turnover_rate, tradestatus, factor_cum FROM stock_daily_hfq"
                " WHERE stock_code=%s"
            )
            select_args: list[str] = [stock_code]
            if window_end is not None:
                select_sql += " AND trade_date BETWEEN %s AND %s"
                select_args.extend([derive_start or "00000000", window_end])
            elif derive_start is not None:
                select_sql += " AND trade_date>=%s"
                select_args.append(derive_start)
            cur.execute(select_sql + " ORDER BY trade_date", select_args)
            stored_hfq = [dict(zip(_HFQ_FIELDS, row)) for row in cur.fetchall()]
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _record_quarantine(conn, latest_trade_date, quarantined)

    qfq = []
    for row in stored_hfq:
        adjusted = dict(row)
        for field in ("open", "high", "low", "close"):
            if row[field] is not None:
                adjusted[field] = float(_decimal(row[field]) / factor_latest)
        qfq.append(adjusted)
    return {
        "stock_code": stock_code,
        "coverage": "partial" if (quarantined or pending) else "unknown",
        "raw_rows": raw_all,
        "factor_events": [
            {**event, "factor_cum": float(event["factor_cum"])} for event in events
        ],
        "factor_audit": factor_audit,
        "hfq_rows": [
            {**row, "factor_cum": float(row["factor_cum"])} for row in stored_hfq
        ],
        "qfq_rows": qfq,
        "state": {
            "stock_code": stock_code,
            "baseline_factor": float(baseline),
            "factor_latest": float(factor_latest),
            "latest_trade_date": latest_trade_date,
            "rebuild_pending": pending,
        },
        "quarantined": quarantined,
    }
