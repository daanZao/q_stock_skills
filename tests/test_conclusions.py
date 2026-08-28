"""save_conclusion / query_conclusions 接缝测试：真实 PG 测试库（issue #7）。

结论表契约（docs/adr/0003）：(subject_type, subject_code, trade_date,
conclusion_type) 为业务唯一键，重复写入为 upsert 语义；payload 为任意 JSON，
结论类型由写入方 skill 自报，server 不校验。
"""

import psycopg2

from qstock_mcp.db import ensure_schema
from qstock_mcp.tools_conclusions import query_conclusions, save_conclusion

MARKET_REVIEW = {
    "subject_type": "market",
    "subject_code": "_market",
    "trade_date": "20240105",
    "conclusion_type": "daily_review.close",
    "payload": {"summary": "缩量反弹", "breadth": {"up": 3200, "down": 1800}},
}


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def test_save_inserts_then_query_roundtrip(pg_test):
    _init(pg_test)
    result = save_conclusion(**MARKET_REVIEW)
    assert result["status"] == "ok"
    assert result["tool"] == "save_conclusion"
    assert result["outcome"] == "inserted"

    got = query_conclusions(subject_type="market")
    assert got["status"] == "ok"
    assert got["count"] == 1
    row = got["rows"][0]
    assert row["subject_code"] == "_market"
    assert row["trade_date"] == "20240105"
    assert row["conclusion_type"] == "daily_review.close"
    assert row["payload"] == MARKET_REVIEW["payload"]


def test_save_same_business_key_is_upsert_not_duplicate(pg_test):
    _init(pg_test)
    assert save_conclusion(**MARKET_REVIEW)["outcome"] == "inserted"
    revised = dict(MARKET_REVIEW, payload={"summary": "改为放量上攻"})
    assert save_conclusion(**revised)["outcome"] == "updated"

    got = query_conclusions(subject_type="market")
    assert got["count"] == 1
    assert got["rows"][0]["payload"] == {"summary": "改为放量上攻"}


def _seed(pg_test):
    _init(pg_test)
    save_conclusion(**MARKET_REVIEW)
    save_conclusion(
        subject_type="stock",
        subject_code="600519",
        trade_date="20240105",
        conclusion_type="sepa.stage",
        payload={"stage": 2, "notes": ["突破", "放量"]},
    )
    save_conclusion(
        subject_type="stock",
        subject_code="600519",
        trade_date="20240104",
        conclusion_type="sepa.stage",
        payload={"stage": 1},
    )


def test_query_filters_by_trade_date(pg_test):
    _seed(pg_test)
    got = query_conclusions(trade_date="2024-01-05")
    assert got["status"] == "ok"
    assert got["count"] == 2
    assert {r["subject_type"] for r in got["rows"]} == {"market", "stock"}


def test_query_filters_by_subject_and_type(pg_test):
    _seed(pg_test)
    got = query_conclusions(subject_type="stock", subject_code="600519")
    assert got["count"] == 2
    got = query_conclusions(conclusion_type="sepa.stage", trade_date="20240104")
    assert got["count"] == 1
    assert got["rows"][0]["payload"] == {"stage": 1}


def test_query_empty_result_is_zero_count_not_error(pg_test):
    _seed(pg_test)
    got = query_conclusions(subject_code="000001")
    assert got["status"] == "ok"
    assert got["count"] == 0
    assert got["rows"] == []


def test_save_accepts_arbitrary_json_payload(pg_test):
    _init(pg_test)
    result = save_conclusion(
        subject_type="stock",
        subject_code="600519",
        trade_date="20240105",
        conclusion_type="custom.type",
        payload={"scalar": 1, "list": [1, "a", None], "nested": {"x": {"y": True}}},
    )
    assert result["status"] == "ok"
    got = query_conclusions(conclusion_type="custom.type")
    assert got["rows"][0]["payload"]["nested"] == {"x": {"y": True}}


def test_save_rejects_bad_date(pg_test):
    result = save_conclusion(**dict(MARKET_REVIEW, trade_date="not-a-date"))
    assert result["status"] == "error"
    assert result["tool"] == "save_conclusion"


def test_save_rejects_non_json_payload(pg_test):
    result = save_conclusion(**dict(MARKET_REVIEW, payload={"bad": object()}))
    assert result["status"] == "error"


def test_query_rejects_bad_date(pg_test):
    assert query_conclusions(trade_date="2024-13-99")["status"] == "error"


def test_save_accepts_non_object_json_payload(pg_test):
    _init(pg_test)
    result = save_conclusion(
        subject_type="stock",
        subject_code="600519",
        trade_date="20240105",
        conclusion_type="custom.list",
        payload=[1, "two", {"three": 3}],
    )
    assert result["status"] == "ok"
    got = query_conclusions(conclusion_type="custom.list")
    assert got["rows"][0]["payload"] == [1, "two", {"three": 3}]
