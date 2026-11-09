import hashlib


def _validation(conn, start: str, end: str, event_set_complete: bool) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH raw_window AS (
                SELECT * FROM stock_daily_raw WHERE trade_date BETWEEN %s AND %s
            ), valid_raw AS (
                SELECT * FROM raw_window WHERE close IS NOT NULL AND close > 0
            ), raw_codes AS (
                SELECT DISTINCT stock_code FROM raw_window
            ), valid_codes AS (
                SELECT DISTINCT stock_code FROM valid_raw
            ), event_window AS (
                SELECT * FROM stock_adjust_factor WHERE ex_date BETWEEN %s AND %s
            ), event_codes AS (
                SELECT DISTINCT stock_code FROM event_window
            ), state_codes AS (
                SELECT stock_code FROM stock_factor_state
                WHERE latest_trade_date BETWEEN %s AND %s
            ), universe AS (
                SELECT stock_code FROM raw_codes
                UNION
                SELECT stock_code FROM event_codes
                UNION
                SELECT stock_code FROM state_codes
            ), no_event_bad AS (
                SELECT v.stock_code
                FROM valid_codes v
                LEFT JOIN event_codes e USING (stock_code)
                LEFT JOIN stock_factor_state s USING (stock_code)
                WHERE %s AND e.stock_code IS NULL
                  AND (
                      s.stock_code IS NULL OR s.baseline_factor <> 1
                      OR s.factor_latest <> 1
                      OR EXISTS (
                          SELECT 1 FROM stock_daily_hfq h
                          WHERE h.stock_code=v.stock_code
                            AND h.trade_date BETWEEN %s AND %s
                            AND h.factor_cum <> 1
                      )
                  )
            )
            SELECT
                (SELECT count(*) FROM event_window),
                (SELECT count(*) FROM event_codes),
                (SELECT count(*) FROM raw_codes r
                 WHERE NOT EXISTS (SELECT 1 FROM event_codes e WHERE e.stock_code=r.stock_code)),
                (SELECT count(*) FROM raw_window),
                (SELECT count(*) FROM raw_codes),
                (SELECT count(*) FROM raw_window WHERE close IS NULL OR close <= 0),
                (SELECT count(*) FROM stock_daily_hfq WHERE trade_date BETWEEN %s AND %s),
                (SELECT count(*) FROM stock_factor_state s
                 WHERE EXISTS (SELECT 1 FROM universe u WHERE u.stock_code=s.stock_code)),
                (SELECT count(*) FROM stock_factor_state s
                 WHERE s.rebuild_pending
                   AND EXISTS (SELECT 1 FROM universe u WHERE u.stock_code=s.stock_code)),
                (SELECT count(*) FROM stock_daily_hfq h
                 WHERE h.trade_date BETWEEN %s AND %s
                   AND NOT EXISTS (
                       SELECT 1 FROM valid_raw r
                       WHERE r.stock_code=h.stock_code AND r.trade_date=h.trade_date
                   )),
                (SELECT count(*) FROM valid_raw r
                 WHERE NOT EXISTS (
                     SELECT 1 FROM stock_daily_hfq h
                     WHERE h.stock_code=r.stock_code AND h.trade_date=r.trade_date
                 )),
                (SELECT count(*) FROM stock_factor_state s
                 WHERE EXISTS (SELECT 1 FROM universe u WHERE u.stock_code=s.stock_code)
                   AND NOT EXISTS (
                       SELECT 1 FROM raw_codes r WHERE r.stock_code=s.stock_code
                   )),
                (SELECT count(*) FROM no_event_bad)
            """,
            (
                start, end, start, end, start, end, event_set_complete, start, end,
                start, end, start, end,
            ),
        )
        row = cur.fetchone()
    return {
        "event_set_complete": event_set_complete,
        "event_rows": row[0],
        "event_stocks": row[1],
        "no_event_stocks": row[2] if event_set_complete else 0,
        "raw_rows": row[3],
        "raw_stocks": row[4],
        "quarantined_rows": row[5],
        "hfq_rows": row[6],
        "state_rows": row[7],
        "pending_states": row[8],
        "hfq_without_valid_raw": row[9],
        "valid_raw_without_hfq": row[10],
        "state_without_raw": row[11],
        "classification_mismatches": row[12],
    }


def _hash_text(values) -> str:
    return hashlib.md5(values.encode("utf-8")).hexdigest()


def _table_hashes(conn, table: str, start: str, end: str) -> dict[str, dict]:
    queries = {
        "raw": (
            "stock_daily_raw", "trade_date",
            ["trade_date", "open", "high", "low", "close", "preclose", "volume",
             "amount", "amplitude", "change_percent", "change_amount",
             "turnover_rate", "tradestatus", "source"],
        ),
        "event": (
            "stock_adjust_factor", "ex_date",
            ["ex_date", "factor_cum", "kind", "detail", "reg_date", "source"],
        ),
        "hfq": (
            "stock_daily_hfq", "trade_date",
            ["trade_date", "open", "high", "low", "close", "volume", "amount",
             "turnover_rate", "tradestatus", "factor_cum"],
        ),
    }
    table_name, date_column, fields = queries[table]
    rendered = ", ".join(f"coalesce({field}::text, '<null>')" for field in fields)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT stock_code, count(*),
                   coalesce(string_agg(concat_ws('|', {rendered}), '|'
                            ORDER BY {date_column}), '')
            FROM {table_name}
            WHERE {date_column} BETWEEN %s AND %s
            GROUP BY stock_code
            """,
            (start, end),
        )
        return {
            row[0]: {"rows": row[1], "hash": _hash_text(row[2])}
            for row in cur.fetchall()
        }


def _state_hashes(conn, codes: set[str], start: str, end: str) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT stock_code, baseline_factor, factor_latest, latest_trade_date,
                   rebuild_pending
            FROM stock_factor_state
            WHERE stock_code=ANY(%s) OR latest_trade_date BETWEEN %s AND %s
            ORDER BY stock_code
            """,
            (sorted(codes), start, end),
        )
        return {
            row[0]: {
                "rows": 1,
                "hash": _hash_text("|".join(
                    "<null>" if value is None else str(value) for value in row
                )),
            }
            for row in cur.fetchall()
        }


def _stock_hashes(conn, start: str, end: str) -> list[dict]:
    hashes = {
        table: _table_hashes(conn, table, start, end)
        for table in ("raw", "event", "hfq")
    }
    codes = set().union(*(set(rows) for rows in hashes.values()))
    hashes["state"] = _state_hashes(conn, codes, start, end)
    codes.update(hashes["state"])
    result = []
    for code in sorted(codes):
        row = {"stock_code": code}
        for table in ("raw", "event", "hfq", "state"):
            value = hashes[table].get(code, {"rows": 0, "hash": ""})
            row[f"{table}_rows"] = value["rows"]
            row[f"{table}_hash"] = value["hash"]
        result.append(row)
    return result


def _raw_sample(conn, start: str, end: str, event_set_complete: bool) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH event_codes AS (
                SELECT DISTINCT stock_code FROM stock_adjust_factor
                WHERE ex_date BETWEEN %s AND %s
            ), classified AS (
                SELECT r.stock_code, r.trade_date, r.close, r.amount, r.source,
                       CASE WHEN e.stock_code IS NOT NULL THEN 'event'
                            WHEN %s THEN 'no_event' ELSE 'unclassified' END AS classification,
                       CASE WHEN left(r.stock_code, 1)='6' THEN 'sh'
                            WHEN left(r.stock_code, 1) IN ('4', '8', '9') THEN 'bse'
                            ELSE 'sz' END AS exchange,
                       left(r.trade_date, 4) AS price_year,
                       CASE WHEN r.close < 10 THEN 'lt10'
                            WHEN r.close < 100 THEN '10_100' ELSE 'gte100' END AS price_bucket
                FROM stock_daily_raw r
                LEFT JOIN event_codes e USING (stock_code)
                WHERE r.trade_date BETWEEN %s AND %s
                  AND r.close IS NOT NULL AND r.close > 0
            ), stock_codes AS (
                SELECT DISTINCT stock_code, classification, exchange FROM classified
            ), exchange_ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY classification, exchange
                    ORDER BY md5('164:stock:' || stock_code), stock_code
                ) AS exchange_position
                FROM stock_codes
            ), stock_ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY classification
                    ORDER BY exchange_position,
                             md5('164:stock:' || stock_code), stock_code
                ) AS stock_position
                FROM exchange_ranked
            ), selected_stocks AS (
                SELECT stock_code FROM stock_ranked WHERE stock_position <= 100
            ), candidates AS (
                SELECT c.* FROM classified c
                JOIN selected_stocks s USING (stock_code)
            ), stratum_ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY stock_code, exchange, price_year, price_bucket, source
                    ORDER BY md5('164:row:' || stock_code || ':' || trade_date), trade_date
                ) AS stratum_position
                FROM candidates
            ), sample_ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY stock_code
                    ORDER BY stratum_position,
                             md5('164:row:' || stock_code || ':' || trade_date), trade_date
                ) AS sample_position
                FROM stratum_ranked
            )
            SELECT stock_code, trade_date, classification, close, amount, source,
                   exchange, price_year, price_bucket
            FROM sample_ranked WHERE sample_position <= 5
            ORDER BY stock_code, trade_date
            """,
            (start, end, event_set_complete, start, end),
        )
        return [
            {
                "stock_code": row[0], "trade_date": row[1], "classification": row[2],
                "close": row[3], "amount": row[4], "source": row[5],
                "exchange": row[6], "price_year": row[7], "price_bucket": row[8],
            }
            for row in cur.fetchall()
        ]


def audit_adjustment_migration(conn, start: str, end: str,
                               event_set_complete: bool) -> dict:
    sample = _raw_sample(conn, start, end, event_set_complete)
    return {
        "validation": _validation(conn, start, end, event_set_complete),
        "stock_hashes": _stock_hashes(conn, start, end),
        "raw_sample": sample,
        "sample_summary": {
            "seed": "164",
            "strategy": "100_per_class_5_per_stock_stratified",
            "rows": len(sample),
            "stocks": len({row["stock_code"] for row in sample}),
            "event_stocks": len({
                row["stock_code"] for row in sample if row["classification"] == "event"
            }),
            "no_event_stocks": len({
                row["stock_code"] for row in sample if row["classification"] == "no_event"
            }),
        },
    }
