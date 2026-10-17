"""import_board_classification 工具测试（issue #73，spec #70）。

解析层打真实文件 tests/fixtures/StockClassifyUse_stock.xls（#67 的 12,929 行基准）；
核心层打真实 PG 测试库（pg_test fixture），验证以文件为准清空重载语义
（重复执行幂等、文件中删除的行不残留、只清本 source_system）与错误契约。
"""

from datetime import date, datetime
from pathlib import Path

import psycopg2
import pytest

from qstock_mcp import tools_init
from qstock_mcp._sw_classify import ClassificationFileError, parse_sw_classification

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "StockClassifyUse_stock.xls"


def _sw_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM sw_industry_class WHERE source_system = 'sw'"
        )
        return cur.fetchone()[0]


def _db_keys(conn) -> set:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, board_code, effective_date FROM sw_industry_class"
            " WHERE source_system = 'sw'"
        )
        return set(cur.fetchall())


# ------------------------------------------------- 解析层（纯函数，不连库）


def test_parse_real_file():
    rows = parse_sw_classification(str(SAMPLE))
    assert len(rows) == 12929
    assert all(len(r["stock_code"]) == 6 for r in rows)
    assert all(len(r["board_code"]) == 6 for r in rows)
    assert all(isinstance(r["effective_date"], date) for r in rows)
    assert all(isinstance(r["updated_at"], datetime) for r in rows)
    # 全历史变更：000001 三条不同生效日期
    changes = [r for r in rows if r["stock_code"] == "000001"]
    assert len(changes) == 3
    assert changes[0]["effective_date"] == date(1991, 4, 3)
    assert changes[0]["board_code"] == "440101"


def test_parse_missing_file():
    with pytest.raises(ClassificationFileError, match="不存在"):
        parse_sw_classification("/nonexistent/StockClassifyUse_stock.xls")


def test_parse_bad_format(tmp_path):
    bad = tmp_path / "bad.xls"
    bad.write_bytes(b"this is not an xls file")
    with pytest.raises(ClassificationFileError):
        parse_sw_classification(str(bad))


# ------------------------------------------------- 核心层（真实 PG 测试库）


def test_import_real_file(pg_test):
    result = tools_init.import_board_classification("sw", str(SAMPLE))
    assert result["status"] == "ok"
    assert result["tool"] == "import_board_classification"
    assert result["inserted"] == 12929

    file_keys = {
        (r["stock_code"], r["board_code"], r["effective_date"])
        for r in parse_sw_classification(str(SAMPLE))
    }
    conn = psycopg2.connect(pg_test)
    assert _sw_count(conn) == 12929
    assert _db_keys(conn) == file_keys
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT source_system FROM sw_industry_class")
        assert [row[0] for row in cur.fetchall()] == ["sw"]
    conn.close()


def test_import_twice_is_idempotent(pg_test):
    first = tools_init.import_board_classification("sw", str(SAMPLE))
    second = tools_init.import_board_classification("sw", str(SAMPLE))
    assert first["status"] == second["status"] == "ok"
    assert second["deleted"] == 12929
    assert second["inserted"] == 12929
    conn = psycopg2.connect(pg_test)
    assert _sw_count(conn) == 12929
    conn.close()


def test_deleted_row_not_resurrected(pg_test, monkeypatch):
    """清空重载语义：文件中删掉的行重导后不存在。"""
    tools_init.import_board_classification("sw", str(SAMPLE))
    rows = parse_sw_classification(str(SAMPLE))
    removed = rows[0]
    monkeypatch.setattr(
        tools_init, "parse_sw_classification", lambda file_path: rows[1:]
    )
    result = tools_init.import_board_classification("sw", str(SAMPLE))
    assert result["status"] == "ok"
    assert result["inserted"] == 12928

    conn = psycopg2.connect(pg_test)
    assert _sw_count(conn) == 12928
    assert (
        removed["stock_code"],
        removed["board_code"],
        removed["effective_date"],
    ) not in _db_keys(conn)
    conn.close()


def test_import_keeps_other_source_system(pg_test):
    """以文件为准只清本 source_system，其他体系（东财概念预留）不受影响。"""
    tools_init.import_board_classification("sw", str(SAMPLE))
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sw_industry_class"
            " (source_system, stock_code, board_code, effective_date)"
            " VALUES ('em', '600519', 'BK0477', '2026-01-01')"
        )
    conn.commit()
    conn.close()

    tools_init.import_board_classification("sw", str(SAMPLE))

    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM sw_industry_class WHERE source_system = 'em'"
        )
        assert cur.fetchone()[0] == 1
    assert _sw_count(conn) == 12929
    conn.close()


def test_import_missing_file_error_contract():
    result = tools_init.import_board_classification("sw", "/nonexistent/x.xls")
    assert result["status"] == "error"
    assert result["tool"] == "import_board_classification"
    assert result["params"] == {"source_system": "sw", "file_path": "/nonexistent/x.xls"}
    assert "不存在" in result["error"]


def test_import_bad_format_error_contract(tmp_path):
    bad = tmp_path / "bad.xls"
    bad.write_bytes(b"not an xls")
    result = tools_init.import_board_classification("sw", str(bad))
    assert result["status"] == "error"
    assert result["tool"] == "import_board_classification"
    assert result["params"]["file_path"] == str(bad)


def test_import_unsupported_source_system():
    result = tools_init.import_board_classification("em", str(SAMPLE))
    assert result["status"] == "error"
    assert result["tool"] == "import_board_classification"
    assert result["params"] == {"source_system": "em", "file_path": str(SAMPLE)}
    assert "em" in result["error"]
