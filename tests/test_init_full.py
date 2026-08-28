"""init_database 轻量初始化（issue #8）接缝测试：fake 适配器 + 真实 PG 测试库。

覆盖：三部分（股票清单/全市场快照/指数日线）独立成败报告、部分失败 →
partial_error、全市场历史回溯（显式参数、单股失败不中断）、重复调用幂等。
fake 返回小区间固定行，缩短全历史回溯的测试耗时。
"""

import psycopg2

from qstock_mcp.tools_init import MAJOR_INDICES, init_database

from fakes import LIST_ROWS, SNAPSHOT_ROWS, FakeInitAdapter, weekday_rows

INDEX_ROWS = weekday_rows("20240101", "20240131")
DAILY_ROWS = weekday_rows("20240101", "20240131")


def _fake(name="fake", **kwargs):
    return FakeInitAdapter(name, index_rows=INDEX_ROWS, **kwargs)


def _counts(pg_test) -> dict:
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        out = {}
        for table in ("stock_list", "market_snapshot", "index_daily", "stock_daily"):
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = cur.fetchone()[0]
    conn.close()
    return out


def _index_counts(pg_test) -> dict:
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT index_code, count(*) FROM index_daily GROUP BY index_code")
        out = dict(cur.fetchall())
    conn.close()
    return out


def test_default_init_reports_three_parts(pg_test):
    result = init_database(adapters=[_fake()])
    assert result["status"] == "ok"
    assert result["tool"] == "init_database"
    assert result["params"] == {"backfill_history": False}

    parts = result["parts"]
    assert set(parts) == {"stock_list", "market_snapshot", "index_daily"}

    assert parts["stock_list"]["status"] == "ok"
    assert parts["stock_list"]["rows"] == len(LIST_ROWS)
    assert parts["stock_list"]["source"] == "fake"

    assert parts["market_snapshot"]["status"] == "ok"
    assert parts["market_snapshot"]["rows"] == len(SNAPSHOT_ROWS)
    assert parts["market_snapshot"]["source"] == "fake"
    assert len(parts["market_snapshot"]["trade_date"]) == 8  # API 未报告时回退当天

    assert parts["index_daily"]["status"] == "ok"
    assert parts["index_daily"]["rows"] > 0
    assert {i["index_code"] for i in parts["index_daily"]["indices"]} == {
        code for code, _ in MAJOR_INDICES
    }
    for i in parts["index_daily"]["indices"]:
        assert i["status"] == "ok"
        assert i["rows"] > 0
        assert i["source"] == "fake"

    counts = _counts(pg_test)
    assert counts["stock_list"] == len(LIST_ROWS)
    assert counts["market_snapshot"] == len(SNAPSHOT_ROWS)
    index_counts = _index_counts(pg_test)
    assert set(index_counts) == {code for code, _ in MAJOR_INDICES}
    assert all(n > 0 for n in index_counts.values())


def test_snapshot_failure_isolated(pg_test):
    adapters = [
        _fake("a", fail={"snapshot"}),
        _fake("b", fail={"snapshot"}),
    ]
    result = init_database(adapters=adapters)
    assert result["status"] == "partial_error"
    assert result["failed_parts"] == ["market_snapshot"]

    part = result["parts"]["market_snapshot"]
    assert part["status"] == "error"
    assert part["error"]
    assert [a["source"] for a in part["attempted_sources"]] == ["a", "b"]

    assert result["parts"]["stock_list"]["status"] == "ok"
    assert result["parts"]["index_daily"]["status"] == "ok"


def test_index_daily_failure_isolated(pg_test):
    result = init_database(adapters=[FakeInitAdapter("a", fail={"index"})])
    assert result["status"] == "partial_error"
    assert result["failed_parts"] == ["index_daily"]

    part = result["parts"]["index_daily"]
    assert part["status"] == "error"
    assert part["rows"] == 0
    assert all(i["status"] == "error" and i["error"] for i in part["indices"])

    assert result["parts"]["stock_list"]["status"] == "ok"
    assert result["parts"]["market_snapshot"]["status"] == "ok"


def test_backfill_writes_stock_daily(pg_test):
    result = init_database(
        backfill_history=True, adapters=[_fake(daily_rows=DAILY_ROWS)]
    )
    assert result["status"] == "ok"

    part = result["parts"]["backfill"]
    assert part["status"] == "ok"
    assert part["total"] == len(LIST_ROWS)
    assert part["succeeded"] == len(LIST_ROWS)
    assert part["failed"] == []
    assert part["rows_upserted"] == len(DAILY_ROWS) * len(LIST_ROWS)
    assert _counts(pg_test)["stock_daily"] == len(DAILY_ROWS) * len(LIST_ROWS)


def test_backfill_single_stock_failure_does_not_abort(pg_test):
    result = init_database(
        backfill_history=True,
        adapters=[_fake(daily_rows=DAILY_ROWS, fail_stocks={"000001"})],
    )
    assert result["status"] == "partial_error"

    part = result["parts"]["backfill"]
    assert part["status"] == "partial_error"
    assert part["total"] == len(LIST_ROWS)
    assert part["succeeded"] == len(LIST_ROWS) - 1
    assert [f["stock_code"] for f in part["failed"]] == ["000001"]
    assert part["failed"][0]["error"]


def test_backfill_not_run_by_default(pg_test):
    result = init_database(adapters=[_fake()])
    assert "backfill" not in result["parts"]
    assert _counts(pg_test)["stock_daily"] == 0


def test_stock_list_skips_rows_without_code_or_name(pg_test):
    # 脏行（缺代码/缺名称）跳过：stock_name 为 NOT NULL 列，一行脏数据不拖垮整批
    rows = LIST_ROWS + [
        {"stock_code": "600000", "stock_name": None},
        {"stock_code": "", "stock_name": "无名氏"},
    ]
    result = init_database(adapters=[_fake(list_rows=rows)])
    assert result["status"] == "ok"
    assert result["parts"]["stock_list"]["rows"] == len(LIST_ROWS)
    assert _counts(pg_test)["stock_list"] == len(LIST_ROWS)


def test_backfill_empty_stock_list_is_error(pg_test):
    # 清单抓取全失败 → 无可回溯标的，回溯部分必须报 error 而非假 ok
    adapters = [_fake("a", fail={"list"}), _fake("b", fail={"list"})]
    result = init_database(backfill_history=True, adapters=adapters)
    part = result["parts"]["backfill"]
    assert part["status"] == "error"
    assert "股票清单为空" in part["error"]
    assert part["total"] == 0
    assert result["status"] == "partial_error"  # 快照/指数部分正常
    assert set(result["failed_parts"]) == {"stock_list", "backfill"}


def test_repeated_init_is_idempotent(pg_test):
    adapters = [_fake()]
    first = init_database(adapters=adapters)
    assert first["status"] == "ok"
    before = _counts(pg_test)

    second = init_database(adapters=adapters)
    assert second["status"] == "ok"
    assert _counts(pg_test) == before
    assert before["stock_list"] > 0
    assert before["market_snapshot"] > 0
    assert before["index_daily"] > 0


def test_repeated_backfill_is_idempotent(pg_test):
    adapters = [_fake(daily_rows=DAILY_ROWS)]
    first = init_database(backfill_history=True, adapters=adapters)
    assert first["status"] == "ok"
    before = _counts(pg_test)

    second = init_database(backfill_history=True, adapters=adapters)
    assert second["status"] == "ok"
    assert _counts(pg_test) == before
    assert before["stock_daily"] > 0
