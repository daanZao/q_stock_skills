"""import_sw_industry_meta 工具测试（issue #74，spec #70）。

种子文件完整性 + PG 测试库幂等导入 + 成分字典覆盖率断言：
字典（含全历史）出现过的全部行业代码及其一/二级父代码必须在 meta 中。
"""

import functools

import psycopg2

from qstock_mcp import tools_init
from qstock_mcp._sw_classify import (
    load_sw_industry_meta_seed,
    parse_sw_classification,
)

from test_import_board_classification import SAMPLE


@functools.lru_cache(maxsize=1)
def _dict_rows():
    """真实 xls（12,929 行）解析一次，模块内各用例共享。"""
    return parse_sw_classification(str(SAMPLE))


def _dict_codes():
    return {r["board_code"] for r in _dict_rows()}


def _current_codes():
    latest = {}
    for r in _dict_rows():
        k = r["stock_code"]
        if k not in latest or r["effective_date"] > latest[k]["effective_date"]:
            latest[k] = r
    return {r["board_code"] for r in latest.values()}


# ------------------------------------------------- 种子文件（纯函数，不连库）


def test_seed_file_integrity():
    seed = load_sw_industry_meta_seed()
    codes = [r["industry_code"] for r in seed]
    assert len(codes) == len(set(codes))
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert all(r["level"] in (1, 2, 3) for r in seed)
    assert all(r["name"] for r in seed)
    # 级别与代码形态一致：一级 XX0000 / 二级 XXXX00 / 三级其余
    for r in seed:
        c = r["industry_code"]
        expected = 1 if c.endswith("0000") else 2 if c.endswith("00") else 3
        assert r["level"] == expected


def test_seed_covers_classification_dict():
    meta_codes = {r["industry_code"] for r in load_sw_industry_meta_seed()}
    all_codes = _dict_codes()
    # 全历史三级代码全覆盖
    assert all_codes <= meta_codes
    # 现行分类的一/二级父代码全覆盖
    current = _current_codes()
    parents = {c[:2] + "0000" for c in current} | {c[:4] + "00" for c in current}
    assert parents <= meta_codes


# ------------------------------------------------- 核心层（真实 PG 测试库）


def test_import_meta(pg_test):
    result = tools_init.import_sw_industry_meta()
    assert result["status"] == "ok"
    assert result["tool"] == "import_sw_industry_meta"
    seed = load_sw_industry_meta_seed()
    assert result["rows"] == len(seed)


    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sw_industry_meta")
        assert cur.fetchone()[0] == len(seed)
        cur.execute(
            "SELECT name, level FROM sw_industry_meta WHERE industry_code = '440101'"
        )
        assert cur.fetchone() == ("银行", 3)
        cur.execute(
            "SELECT name, level FROM sw_industry_meta WHERE industry_code = '480000'"
        )
        assert cur.fetchone() == ("银行", 1)
    conn.close()


def test_import_meta_is_idempotent(pg_test):
    first = tools_init.import_sw_industry_meta()
    second = tools_init.import_sw_industry_meta()
    assert first["status"] == second["status"] == "ok"
    assert second["rows"] == first["rows"]


    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sw_industry_meta")
        assert cur.fetchone()[0] == first["rows"]
    conn.close()


def test_import_meta_missing_pg_dsn(monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    result = tools_init.import_sw_industry_meta()
    assert result["status"] == "error"
    assert result["tool"] == "import_sw_industry_meta"
    assert "PG_DSN" in result["error"]
