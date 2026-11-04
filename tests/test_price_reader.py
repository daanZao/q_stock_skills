"""个股价格读取接缝：默认 legacy，可替换实现且不静默回落（issue #159）。"""

import psycopg2
import pytest

from qstock_mcp import price_reader
from qstock_mcp.db import ensure_schema
from qstock_mcp.repository import upsert_daily


def test_price_reader_can_switch_to_registered_implementation(monkeypatch):
    class FakeReader:
        def select_daily(self, conn, stock_code, adj, start, end):
            return [{"stock_code": stock_code, "adj": adj, "start": start, "end": end}]

    price_reader.register_price_reader("fake", FakeReader())
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "fake")
    assert price_reader.select_daily(None, "600519", "qfq", "20240101", "20240131") == [
        {"stock_code": "600519", "adj": "qfq", "start": "20240101", "end": "20240131"}
    ]


@pytest.mark.parametrize("name", ["new", "misspelled"])
def test_price_reader_rejects_unavailable_implementation(monkeypatch, name):
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", name)
    with pytest.raises(RuntimeError, match="QSTOCK_PRICE_SOURCE"):
        price_reader.get_price_reader()


def test_default_reader_returns_legacy_values(pg_test, monkeypatch):
    monkeypatch.delenv("QSTOCK_PRICE_SOURCE", raising=False)
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    upsert_daily(
        conn, "600519", "qfq",
        [{"trade_date": "20240102", "close": 10.2, "volume": 1000}],
        "tencent",
    )
    rows = price_reader.select_daily(conn, "600519", "qfq", "20240101", "20240131")
    assert rows[0]["close"] == 10.2
    assert rows[0]["source"] == "tencent"
    conn.close()


def test_coverage_uses_selected_price_reader(pg_test, monkeypatch):
    from qstock_mcp.repository import upsert_stock_list
    from qstock_mcp.tools_coverage import verify_history_coverage

    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    upsert_stock_list(conn, [{"stock_code": "600519", "stock_name": "贵州茅台"}], "fake")
    conn.close()

    class FakeReader:
        def select_dates(self, conn, stock_code, adj, start, end):
            assert (stock_code, adj, start, end) == ("600519", "qfq", "20240102", "20240102")
            return {"20240102"}

    price_reader.register_price_reader("coverage-fake", FakeReader())
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "coverage-fake")
    result = verify_history_coverage("20240102", "20240102")
    assert result["sections"]["stock_daily"] == [{
        "code": "600519", "name": None, "rows": 1,
        "first_date": "20240102", "last_date": "20240102",
        "missing_days": [], "status": "ok",
    }]
