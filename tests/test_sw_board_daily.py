"""query_sw_board_daily 工具测试（issue #75，spec #70）。

小样本种子（两行业、跨一次分类变更、含停牌占位行、含 amount null 行、
含当日完全无日线行的成分、含字典未覆盖股票与兄弟码）打真实 PG 测试库
（pg_test fixture），验证 level 1/2 聚合数值、point-in-time 归属、
停牌剔除归一（家数与加权同一双非 null 口径）、stock_count 成分总数口径、
trade_date 缺省、error 契约与 meta 缺失名称容忍；另有合成批量数据的
20 日窗口连续查询性能实测。
"""

import time

import psycopg2
import pytest
from psycopg2.extras import execute_values

from qstock_mcp import tools_sw_board
from qstock_mcp.db import ensure_schema

# ------------------------------------------------- 小样本种子

META = [
    ("240000", "有色金属", 1),
    ("240100", "工业金属", 2),
    ("240101", "铜", 3),
    ("480000", "银行", 1),
    ("480100", "银行Ⅱ", 2),
    ("480101", "银行（2014 版）", 3),
    ("480300", "股份制银行Ⅱ", 2),
    ("480301", "股份制银行Ⅲ", 3),
    # 99xxxx 故意不入 meta：验证 industry_name 为 null 不报错
]

# (stock_code, board_code, effective_date, source_system)
CLASS = [
    ("000001", "240101", "2020-01-01", "sw"),
    ("000002", "240101", "2020-01-01", "sw"),
    ("000003", "480301", "2020-01-01", "sw"),
    ("000004", "480301", "2020-01-01", "sw"),
    # 000006 跨一次分类变更：2024-01-01 起从铜调入股份制银行
    ("000006", "240101", "2020-01-01", "sw"),
    ("000006", "480301", "2024-01-01", "sw"),
    ("000007", "990101", "2020-01-01", "sw"),  # meta 未覆盖的行业
    ("000008", "770101", "2020-01-01", "em"),  # 非 sw 体系，应被排除
    ("000009", "240101", "2020-01-01", "sw"),  # 当日完全无日线行：仍计入 stock_count
    ("000010", "240101", "2020-01-01", "sw"),  # pct 非 null 但 amount null
    ("000011", "480101", "2020-01-01", "sw"),  # 480301 的兄弟码（同 4801 前缀）
    # 000005 无字典行：应被排除而非报错
]

# (stock_code, trade_date, change_percent, amount)
DAILY = [
    # 20231229（分类变更前）
    ("000001", "20231229", 1.0, 100.0),
    ("000006", "20231229", 1.0, 50.0),
    # 20240102（分类变更后）
    ("000001", "20240102", 2.0, 100.0),
    ("000002", "20240102", -1.0, 300.0),
    ("000003", "20240102", 0.0, 200.0),
    ("000004", "20240102", None, None),  # 停牌占位行
    ("000005", "20240102", 5.0, 999.0),  # 字典未覆盖
    ("000006", "20240102", 4.0, 100.0),
    ("000007", "20240102", 1.0, 10.0),
    ("000008", "20240102", 3.0, 50.0),  # 仅 em 成分
    ("000010", "20240102", 5.0, None),  # pct 非 null 但 amount null：不计家数不加权
    ("000011", "20240102", 2.0, 100.0),
]


def _seed(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO sw_industry_meta (industry_code, name, level) VALUES %s",
            META,
        )
        execute_values(
            cur,
            "INSERT INTO sw_industry_class"
            " (stock_code, board_code, effective_date, source_system) VALUES %s",
            CLASS,
        )
        execute_values(
            cur,
            "INSERT INTO stock_daily"
            " (stock_code, trade_date, change_percent, amount) VALUES %s",
            DAILY,
        )
    conn.commit()
    conn.close()


def _seed_daily_rows(pg_test, rows):
    """追加显式 adj 的日线行：(stock_code, trade_date, adj, change_percent, amount)。"""
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO stock_daily"
            " (stock_code, trade_date, adj, change_percent, amount) VALUES %s",
            rows,
        )
    conn.commit()
    conn.close()


def _by_code(result):
    return {r["industry_code"]: r for r in result["rows"]}


# ------------------------------------------------- level 1/2 聚合数值


def test_aggregate_level2(pg_test):
    _seed(pg_test)
    result = tools_sw_board.query_sw_board_daily(trade_date="20240102", level=2)
    assert result["status"] == "ok"
    assert result["tool"] == "query_sw_board_daily"
    assert result["trade_date"] == "20240102"
    rows = _by_code(result)
    assert result["count"] == len(result["rows"]) == 4  # 240100/480100/480300/990100

    copper = rows["240100"]  # 成分 000001/000002/000009(无当日行)/000010(amount null)
    assert copper["industry_name"] == "工业金属"
    assert copper["change_pct"] == pytest.approx((2.0 * 100 - 1.0 * 300) / 400)
    # 000010 pct=5.0 但 amount null：不计入上涨家数（家数与加权同一双非 null 口径）
    assert (copper["up_count"], copper["down_count"], copper["flat_count"]) == (1, 1, 0)
    assert copper["amount"] == pytest.approx(400.0)
    # stock_count 含停牌占位与当日无日线行的成分；差即停牌/无行情数
    assert (copper["stock_count"], copper["counted_count"]) == (4, 2)
    assert copper["basis"] == "sw_amount_weighted"

    bank = rows["480300"]  # 000003(0,200) 000004(停牌) 000006(4.0,100)
    assert bank["industry_name"] == "股份制银行Ⅱ"
    # 停牌行从权重剔除并归一：(0*200 + 4*100) / (200+100)；change_pct 保留 4 位小数
    assert bank["change_pct"] == 1.3333
    assert (bank["up_count"], bank["down_count"], bank["flat_count"]) == (1, 0, 1)
    assert bank["amount"] == pytest.approx(300.0)
    assert bank["stock_count"] - bank["counted_count"] == 1
    assert (bank["stock_count"], bank["counted_count"]) == (3, 2)

    sibling = rows["480100"]  # 000011(2.0,100)，480301 的兄弟码组
    assert sibling["industry_name"] == "银行Ⅱ"
    assert sibling["change_pct"] == pytest.approx(2.0)
    assert (sibling["stock_count"], sibling["counted_count"]) == (1, 1)

    unnamed = rows["990100"]  # meta 未覆盖：名称为 null 不报错
    assert unnamed["industry_name"] is None
    assert unnamed["change_pct"] == pytest.approx(1.0)

    # 字典未覆盖的 000005 与仅 em 成分的 000008 被排除：全行业合计不含其成交额
    assert sum(r["amount"] for r in result["rows"]) == pytest.approx(810.0)


def test_aggregate_level1(pg_test):
    _seed(pg_test)
    result = tools_sw_board.query_sw_board_daily(trade_date="2024-01-02", level=1)
    assert result["status"] == "ok"
    rows = _by_code(result)
    assert set(rows) == {"240000", "480000", "990000"}
    # 一级码形态与 meta 一致（XX0000），可 join 名称
    assert rows["240000"]["industry_name"] == "有色金属"
    assert rows["480000"]["industry_name"] == "银行"
    assert rows["240000"]["change_pct"] == pytest.approx(-0.25)
    # 240000 成分 000001/000002/000009(无当日行)/000010(amount null)
    assert (rows["240000"]["stock_count"], rows["240000"]["counted_count"]) == (4, 2)
    # 480000 成分 000003/000004(停牌)/000006/000011：(0*200+4*100+2*100)/400
    assert rows["480000"]["change_pct"] == pytest.approx(1.5)
    assert (rows["480000"]["stock_count"], rows["480000"]["counted_count"]) == (4, 3)
    assert rows["990000"]["industry_name"] is None


# ------------------------------------------------- point-in-time 归属


def test_point_in_time_membership(pg_test):
    _seed(pg_test)
    before = tools_sw_board.query_sw_board_daily(trade_date="20231229", level=2)
    rows = _by_code(before)
    # 变更日前：000006 归 240100（铜）；480300/480100/990100 当日无成分日线行，
    # 整组不出现（空日期同理）
    assert set(rows) == {"240100"}
    # stock_count 为 PIT 归属成分总数：000001/000002/000006/000009/000010 共 5 只，
    # 当日有行情参与计算的仅 000001/000006
    assert (rows["240100"]["stock_count"], rows["240100"]["counted_count"]) == (5, 2)
    assert rows["240100"]["change_pct"] == pytest.approx(
        (1.0 * 100 + 1.0 * 50) / 150
    )

    after = tools_sw_board.query_sw_board_daily(trade_date="20240102", level=2)
    copper = _by_code(after)["240100"]
    # 变更日后：000006 归 480300，铜组 PIT 成分 4 只
    assert copper["stock_count"] == 4
    assert _by_code(after)["480300"]["stock_count"] == 3


# ------------------------------------------------- trade_date 缺省与空日期


def test_default_trade_date_is_latest(pg_test):
    _seed(pg_test)
    result = tools_sw_board.query_sw_board_daily()
    assert result["status"] == "ok"
    assert result["trade_date"] == "20240102"  # 库内最新交易日
    assert result["count"] == 3


def test_empty_date_count0(pg_test):
    _seed(pg_test)
    result = tools_sw_board.query_sw_board_daily(trade_date="20231201")
    assert result["status"] == "ok"
    assert result["count"] == 0
    assert result["rows"] == []


# ------------------------------------------------- adj 动态选择（issue #92）

# adj 选择测试的最小成分字典：三只股同属一个二级行业
_ADJ_CLASS = [
    ("000001", "240101", "2020-01-01", "sw"),
    ("000002", "240101", "2020-01-01", "sw"),
    ("000003", "240101", "2020-01-01", "sw"),
]


def _seed_class(pg_test, rows):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO sw_industry_class"
            " (stock_code, board_code, effective_date, source_system) VALUES %s",
            rows,
        )
    conn.commit()
    conn.close()


def test_adj_dominant_none(pg_test):
    """none 行数多于 qfq 的日子选 none（morning 批场景）：聚合只用 none 行。"""
    _seed_class(pg_test, _ADJ_CLASS)
    _seed_daily_rows(pg_test, [
        ("000001", "20240102", "qfq", 9.0, 999.0),
        ("000001", "20240102", "none", 1.0, 100.0),
        ("000002", "20240102", "none", 3.0, 200.0),
        ("000003", "20240102", "none", -1.0, 100.0),
    ])
    result = tools_sw_board.query_sw_board_daily(trade_date="20240102", level=2)
    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["adj"] == "none"  # 顶层透明标注本次口径
    row = result["rows"][0]
    assert row["adj"] == "none"
    # 只聚合 none 行：(1*100 + 3*200 - 1*100) / (100+200+100)
    assert row["change_pct"] == pytest.approx(1.5)
    assert row["amount"] == pytest.approx(400.0)
    assert (row["up_count"], row["down_count"], row["flat_count"]) == (2, 1, 0)


def test_adj_dominant_qfq(pg_test):
    """qfq 行数多于 none 的日子选 qfq：聚合只用 qfq 行。"""
    _seed_class(pg_test, _ADJ_CLASS)
    _seed_daily_rows(pg_test, [
        ("000001", "20240102", "qfq", 2.0, 100.0),
        ("000002", "20240102", "qfq", 4.0, 300.0),
        ("000003", "20240102", "qfq", 0.0, 100.0),
        ("000001", "20240102", "none", 9.0, 999.0),
    ])
    result = tools_sw_board.query_sw_board_daily(trade_date="20240102", level=2)
    assert result["status"] == "ok"
    row = result["rows"][0]
    assert row["adj"] == "qfq"
    # (2*100 + 4*300 + 0*100) / 500
    assert row["change_pct"] == pytest.approx(2.8)
    assert row["amount"] == pytest.approx(500.0)


def test_adj_tie_prefers_qfq(pg_test):
    """各 adj 行数持平（且 qfq 在列）时取 qfq。"""
    _seed_class(pg_test, _ADJ_CLASS)
    _seed_daily_rows(pg_test, [
        ("000001", "20240102", "qfq", 2.0, 100.0),
        ("000002", "20240102", "qfq", 4.0, 100.0),
        ("000001", "20240102", "none", 9.0, 100.0),
        ("000003", "20240102", "none", 9.0, 100.0),
    ])
    result = tools_sw_board.query_sw_board_daily(trade_date="20240102", level=2)
    assert result["status"] == "ok"
    row = result["rows"][0]
    assert row["adj"] == "qfq"
    assert row["change_pct"] == pytest.approx(3.0)


def test_dict_as_of(pg_test):
    """顶层 dict_as_of = sw 成分字典 max effective_date（yyyymmdd）。"""
    _seed(pg_test)
    result = tools_sw_board.query_sw_board_daily(trade_date="20240102")
    assert result["status"] == "ok"
    assert result["dict_as_of"] == "20240101"  # CLASS 最大 effective_date


def test_default_trade_date_none_only_latest(pg_test):
    """库内最新交易日只有 none 行时（东财故障、morning 批兜底），
    trade_date 缺省不被跳过且选中 none。"""
    _seed(pg_test)
    _seed_daily_rows(pg_test, [
        ("000001", "20240103", "none", 1.0, 100.0),
        ("000002", "20240103", "none", 2.0, 200.0),
    ])
    result = tools_sw_board.query_sw_board_daily()
    assert result["status"] == "ok"
    assert result["trade_date"] == "20240103"
    assert result["count"] == 1
    assert result["rows"][0]["adj"] == "none"
    assert result["rows"][0]["change_pct"] == 1.6667  # 保留 4 位小数


# ------------------------------------------------- industry_code 过滤


def test_industry_code_filter(pg_test):
    _seed(pg_test)
    # 2 位一级前缀
    r = tools_sw_board.query_sw_board_daily(
        trade_date="20240102", level=1, industry_code="24"
    )
    assert [row["industry_code"] for row in r["rows"]] == ["240000"]
    # 6 位完整三级码：精确匹配该行业，同属前 4 位前缀的兄弟码（480101）被排除
    r = tools_sw_board.query_sw_board_daily(
        trade_date="20240102", level=2, industry_code="480301"
    )
    assert [row["industry_code"] for row in r["rows"]] == ["480300"]
    assert r["rows"][0]["stock_count"] == 3  # 000003/000004/000006，不含 000011
    # 6 位一级码（按尾零对化简为前缀 '48'）：命中 4801xx 与 4803xx 两组
    r = tools_sw_board.query_sw_board_daily(
        trade_date="20240102", level=2, industry_code="480000"
    )
    assert [row["industry_code"] for row in r["rows"]] == ["480100", "480300"]


# ------------------------------------------------- error 契约


def test_invalid_level(pg_test):
    _seed(pg_test)
    result = tools_sw_board.query_sw_board_daily(trade_date="20240102", level=3)
    assert result["status"] == "error"
    assert result["tool"] == "query_sw_board_daily"
    assert result["params"]["level"] == 3


def test_invalid_industry_code(pg_test):
    _seed(pg_test)
    for bad in ("abc", "2", "24010", "24010X"):
        result = tools_sw_board.query_sw_board_daily(
            trade_date="20240102", industry_code=bad
        )
        assert result["status"] == "error"
        assert result["tool"] == "query_sw_board_daily"
        assert result["params"]["industry_code"] == bad


def test_invalid_trade_date(pg_test):
    _seed(pg_test)
    result = tools_sw_board.query_sw_board_daily(trade_date="2024-13-99")
    assert result["status"] == "error"
    assert result["tool"] == "query_sw_board_daily"


def test_missing_pg_dsn(monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    result = tools_sw_board.query_sw_board_daily()
    assert result["status"] == "error"
    assert result["tool"] == "query_sw_board_daily"
    assert "PG_DSN" in result["error"]


# ------------------------------------------------- 性能实测（合成批量数据）

_PERF_STOCKS = 3000
_PERF_DAYS = 25


def _perf_seed(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    boards = [f"{g}{s}01" for g in ("11", "22", "34", "48", "65", "72") for s in ("01", "02", "03", "04", "05")]
    meta = (
        [(f"{g}0000", f"一级{g}", 1) for g in ("11", "22", "34", "48", "65", "72")]
        + [(f"{g}{s}00", f"二级{g}{s}", 2) for g in ("11", "22", "34", "48", "65", "72") for s in ("01", "02", "03", "04", "05")]
        + [(b, f"三级{b}", 3) for b in boards]
    )
    dates = [f"202409{d:02d}" for d in range(1, _PERF_DAYS + 1)]
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO sw_industry_meta (industry_code, name, level) VALUES %s",
            meta,
        )
        execute_values(
            cur,
            "INSERT INTO sw_industry_class"
            " (stock_code, board_code, effective_date, source_system) VALUES %s",
            [
                (f"{i:06d}", boards[i % len(boards)], "2000-01-01", "sw")
                for i in range(_PERF_STOCKS)
            ],
        )
        for chunk_start in range(0, _PERF_STOCKS, 500):
            execute_values(
                cur,
                "INSERT INTO stock_daily"
                " (stock_code, trade_date, change_percent, amount) VALUES %s",
                [
                    (
                        f"{i:06d}",
                        d,
                        ((i * 7 + int(d[-2:])) % 21 - 10) / 10.0,
                        100.0 + (i % 50),
                    )
                    for i in range(chunk_start, chunk_start + 500)
                    for d in dates
                ],
                page_size=5000,
            )
    conn.commit()
    conn.close()
    return dates


def test_perf_20day_window(pg_test):
    """3000 股 × 25 日合成数据（75,000 行 stock_daily）：连续 20 日
    level 1/2 交替查询，校验行数正确并实测耗时（打印供报告引用）。"""
    dates = _perf_seed(pg_test)
    t0 = time.perf_counter()
    total_rows = 0
    for i, d in enumerate(dates[-20:]):
        result = tools_sw_board.query_sw_board_daily(
            trade_date=d, level=1 + i % 2
        )
        assert result["status"] == "ok"
        total_rows += result["count"]
    elapsed = time.perf_counter() - t0
    assert total_rows == 10 * 6 + 10 * 30  # level1 日 6 个一级，level2 日 30 个二级
    # 宽松上限：20 次查询合计 < 30s（实测远低于此，见打印）
    assert elapsed < 30.0
    print(
        f"\n[perf] {_PERF_STOCKS} 股 × {_PERF_DAYS} 日：20 次查询合计 "
        f"{elapsed:.3f}s，均值 {elapsed / 20 * 1000:.1f}ms/次"
    )
