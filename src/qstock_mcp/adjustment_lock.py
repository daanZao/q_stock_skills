"""复权事实写入与切换共用的事务咨询锁。"""

ADJUSTMENT_FACT_LOCK_KEY = 0x7161646A


def lock_adjustment_facts(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADJUSTMENT_FACT_LOCK_KEY,))
