"""申万官网薄封装（issue #118，spec #111）：录制响应解析 + 量纲换算 + 窗口过滤。

fixture 为 2026-10-23/24 实抓响应裁剪（tests/fixtures/sw/，README 见
docs/research/2026-10-20-sw-boards-mapping.md 与量纲对拍记录）。测试不触网：
解析为纯函数直喂 fixture；fetch_sw_board_history 的窗口过滤经 monkeypatch
替换 _get_json 驱动。
"""

import json
from pathlib import Path

import pytest

from qstock_mcp.adapters._sw_board import (
    check_sw_envelope,
    map_sw_analysis_rows,
    map_sw_board_list,
    map_sw_current_rows,
    map_sw_trend_rows,
)
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.adapters.sw_adapter import SwSResearchAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "sw"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- current（实时快照）

def test_current_l1_rows_units_and_derived():
    rows = map_sw_current_rows(_load("current_l1.json")["data"]["results"], 1)
    assert len(rows) == 3
    agri = next(r for r in rows if r["board_code"] == "801010")
    assert agri["board_type"] == "industry_l1"
    assert agri["board_name"] == "农林牧渔"
    # 量纲：l5 百万元→万元 ×1e2；l11 万手→股 ×1e6（2026-10-24 收盘对拍核实）
    assert agri["amount"] == pytest.approx(22352.71 * 1e2)
    assert agri["volume"] == int(2963.00 * 1e6)
    # 价格直取 + 派生（最新/昨收派生涨跌幅、高低/昨收派生振幅）
    assert agri["latest_price"] == 2491.17
    assert agri["pre_close"] == 2491.92
    assert agri["open"] == 2475.69
    assert agri["change_amount"] == pytest.approx(2491.17 - 2491.92)
    assert agri["change_percent"] == pytest.approx(
        (2491.17 - 2491.92) / 2491.92 * 100
    )
    assert agri["amplitude"] == pytest.approx(
        (2494.40 - 2440.51) / 2491.92 * 100
    )
    # 申万不发布列恒 None（三字段第四态废弃口径）
    assert agri["volume_ratio"] is None
    assert agri["leading_stock"] is None
    assert agri["leading_change"] is None
    assert agri["stock_count"] is None


def test_current_l2_rows_board_type():
    rows = map_sw_current_rows(_load("current_l2.json")["data"]["results"], 2)
    assert all(r["board_type"] == "industry_l2" for r in rows)
    assert all(r["board_code"].startswith("801") for r in rows)


def test_current_missing_values_not_fabricated():
    records = _load("current_l1.json")["data"]["results"]
    broken = dict(records[0], l3="-", l5="", l11=None)
    row = map_sw_current_rows([broken], 1)[0]
    assert row["pre_close"] is None
    assert row["amount"] is None
    assert row["volume"] is None
    # 昨收缺失：派生涨跌/振幅不可算，置 None 不伪造
    assert row["change_percent"] is None
    assert row["change_amount"] is None
    assert row["amplitude"] is None


# ---------------------------------------------------------------- trend（历史 K 线）

def test_trend_rows_units_markup_and_amplitude():
    data = _load("trend_801010.json")["data"]
    rows = map_sw_trend_rows(data, 1, "801010", "农林牧渔")
    assert len(rows) == 4
    last = rows[-1]
    assert last["trade_date"] == "20261023"
    # markup/hike 接口直给（akshare 封装会丢弃，自写适配取回）
    assert last["change_percent"] == -0.03
    assert last["change_amount"] == -0.75
    # 量纲：bargainsum 亿元→万元 ×1e4；bargainamount 亿股→股 ×1e8
    assert last["amount"] == pytest.approx(223.52707228 * 1e4)
    assert last["volume"] == int(29.62997614 * 1e8)
    # 振幅 = (最高−最低)/昨收（昨收=上一行收盘）；首行无昨收为 None
    prev_close = rows[-2]["latest_price"]
    assert last["amplitude"] == pytest.approx((2493.67 - 2442.52) / prev_close * 100)
    assert rows[0]["trade_date"] == "19991230"  # 历史深度至 1999
    assert rows[0]["amplitude"] is None
    assert all(r["board_type"] == "industry_l1" for r in rows)
    assert all(r["board_code"] == "801010" for r in rows)


# ---------------------------------------------------------------- analysis（每日分析报表）

def test_analysis_rows_valuation_dims():
    records = _load("analysis_l1.json")["data"]["results"]
    rows = map_sw_analysis_rows(records, 1)
    agri = next(r for r in rows if r["board_code"] == "801010")
    # bargaindate ISO 带时区 → yyyymmdd
    assert agri["trade_date"] == "20261023"
    assert agri["level"] == 1
    assert agri["close_index"] == 2491.17
    assert agri["turnover_rate"] == 4.65
    assert agri["pe_ratio"] == 78.88
    assert agri["pb_ratio"] == 2.37
    assert agri["dividend_yield"] == 1.43
    # 流通市值 亿元→万元 ×1e4（ADR 0006）
    assert agri["float_cap"] == pytest.approx(5664.54 * 1e4)


# ---------------------------------------------------------------- 板块清单

def test_board_list_from_current_raw():
    raw = {
        1: _load("current_l1.json")["data"]["results"],
        2: _load("current_l2.json")["data"]["results"],
    }
    boards = map_sw_board_list(raw)
    assert {b["level"] for b in boards} == {1, 2}
    assert all({"board_code", "board_name", "level"} <= set(b) for b in boards)


# ---------------------------------------------------------------- 信封校验

def test_envelope_rejects_bad_code_and_shape():
    with pytest.raises(ValueError):
        check_sw_envelope({"code": "500", "message": "boom", "data": {}}, "t")
    with pytest.raises(ValueError):
        check_sw_envelope({"code": "200", "data": None}, "t")
    with pytest.raises(ValueError):
        check_sw_envelope([1, 2], "t")
    check_sw_envelope({"code": "200", "message": "ok", "data": {"count": 0}}, "t")


# ---------------------------------------------------------------- 窗口过滤（HTTP 层 monkeypatch）

def test_history_window_filter(monkeypatch):
    """fetch_sw_board_history 客户端过滤 [start, end]：trend 一次调用即全历史。"""
    adapter = SwSResearchAdapter(request_interval_s=0)
    payload = _load("trend_801010.json")
    monkeypatch.setattr(adapter, "_get_json", lambda *a, **k: payload)
    board = {"board_code": "801010", "board_name": "农林牧渔", "level": 1}
    rows = adapter.fetch_sw_board_history(board, "20261020", "20261023")
    assert [r["trade_date"] for r in rows] == ["20261022", "20261023"]
    assert adapter.fetch_sw_board_history(board, "19990101", "19990131") == []
