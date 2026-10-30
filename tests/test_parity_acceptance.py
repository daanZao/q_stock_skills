"""迁移总验收对拍编排（issue #122，spec #111）：历史连续性 + 五面验收编排。

覆盖：连续性逐面归因（有行覆盖 / 事件驱动面台账 empty=上游确认无数据 /
断档区分未采集与采集失败 / 每日节律面无行即断档）、indices 面
verify_index_coverage 复用（逐指数日历缺口 = 回填窗口定位）、
run_parity_acceptance 注入 fake 的端到端结构（zt_pool infeasible 为已记录
决策非失败）。DB 用例触真实 PG 测试库（≤100 行，pg_test 每用例清库）；
对拍用例不触真实网络（fake 适配器）。
"""

from datetime import datetime, timezone

import psycopg2
import pytest

from qstock_mcp.db import ensure_schema
from qstock_mcp.parity_acceptance import (
    check_history_continuity,
    run_parity_acceptance,
)

TRADING_DAYS = [
    "20261013", "20261014", "20261015", "20261016",
    "20261019", "20261020", "20261021", "20261022", "20261023",
]
END, WINDOW = "20261023", 10


@pytest.fixture
def pg_ready(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()
    return pg_test


def _seed_continuity(dsn):
    """窗口 20261013~20261023 的分面覆盖/空确认/断档场景（共 27 行）。"""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            # snapshot（每日节律）：覆盖 2 天，其余无行无台账 → 断档
            for d in ("20261013", "20261014"):
                cur.execute(
                    "INSERT INTO market_snapshot (trade_date, stock_code,"
                    " stock_name, latest_price) VALUES (%s, '600519',"
                    " '贵州茅台', 1700.0)", (d,))
            # boards（每日节律）：覆盖 1 天
            cur.execute(
                "INSERT INTO market_boards (trade_date, board_type,"
                " board_name, latest_price) VALUES ('20261013',"
                " 'industry_l1', '食品饮料', 6500.0)")
            # zt_pool（事件驱动）：覆盖 1 天；1014 台账 empty = 上游确认无数据
            cur.execute(
                "INSERT INTO zt_pool (trade_date, pool_type, stock_code)"
                " VALUES ('20261013', 'zt', '600519')")
            # lhb（事件驱动）：覆盖 1 天；1014 台账 ok(0 行) = 无榜单；
            # 1015 台账 error = 采集失败；1016 无台账 = 未采集
            cur.execute(
                "INSERT INTO lhb_basic (trade_date, stock_code, lhb_reason)"
                " VALUES ('20261013', '600519', '日涨幅偏离值达7%')")
            # index_daily：000001 全覆盖；000688 缺 1014/1015（回填窗口场景）
            for d in TRADING_DAYS:
                cur.execute(
                    "INSERT INTO index_daily (index_code, trade_date, close)"
                    " VALUES ('000001', %s, 3000.0)", (d,))
            for d in (d for d in TRADING_DAYS if d not in ("20261014", "20261015")):
                cur.execute(
                    "INSERT INTO index_daily (index_code, trade_date, close)"
                    " VALUES ('000688', %s, 900.0)", (d,))
            # market_indices：覆盖 1 天
            cur.execute(
                "INSERT INTO market_indices (trade_date, index_code,"
                " index_name, latest_price) VALUES ('20261013', '000001',"
                " '上证指数', 3000.0)")
            # 台账：zt_pool 1014 empty；lhb 1014 ok(0 行) / 1015 error
            ledger = [
                ("20261014", "zt_pool", "empty", None, 0),
                ("20261014", "lhb", "ok", "exchange-lhb", 0),
                ("20261015", "lhb", "error", None, None),
            ]
            for trade_date, section, status, source, rows in ledger:
                cur.execute(
                    "INSERT INTO collection_log (trade_date, section, status,"
                    " source, rows, error_kind, started_at, finished_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (trade_date, section, status, source, rows,
                     "no_data_for_date" if status == "empty" else
                     ("upstream_unreachable" if status == "error" else None),
                     datetime(2026, 10, 23, tzinfo=timezone.utc),
                     datetime(2026, 10, 23, tzinfo=timezone.utc)))
        conn.commit()
    finally:
        conn.close()


class TestHistoryContinuity:
    def test_daily_cadence_faces_flag_all_missing_days(self, pg_ready):
        _seed_continuity(pg_ready)
        out = check_history_continuity(end=END, window_days=WINDOW)
        assert out["status"] == "gap"
        snap = out["faces"]["snapshot"]
        assert snap["cadence"] == "daily"
        assert snap["covered_days"] == 2
        assert snap["empty_confirmed_days"] == []
        assert len(snap["gap_days"]) == 7  # 9 交易日 - 覆盖 2
        assert all(g["ledger"] is None for g in snap["gap_days"])  # 未采集归因
        boards = out["faces"]["boards"]
        assert boards["covered_days"] == 1
        assert len(boards["gap_days"]) == 8

    def test_event_cadence_empty_confirmed_is_not_gap(self, pg_ready):
        _seed_continuity(pg_ready)
        out = check_history_continuity(end=END, window_days=WINDOW)
        zt = out["faces"]["zt_pool"]
        assert zt["empty_confirmed_days"] == ["20261014"]  # 台账 empty 覆盖
        assert [g["trade_date"] for g in zt["gap_days"]] == [
            "20261015", "20261016", "20261019", "20261020",
            "20261021", "20261022", "20261023",
        ]
        lhb = out["faces"]["lhb"]
        assert lhb["empty_confirmed_days"] == ["20261014"]  # ok(0 行) = 无榜单
        by_date = {g["trade_date"]: g["ledger"] for g in lhb["gap_days"]}
        assert by_date["20261015"] == "error"  # 采集失败归因
        assert by_date["20261016"] is None  # 未采集归因

    def test_indices_face_reuses_verify_index_coverage(self, pg_ready):
        _seed_continuity(pg_ready)
        out = check_history_continuity(end=END, window_days=WINDOW)
        idx = out["faces"]["indices"]
        coverage = {i["index_code"]: i for i in idx["index_daily"]["indices"]}
        assert coverage["000001"]["status"] == "ok"
        assert coverage["000001"]["missing_days"] == []
        assert coverage["000688"]["status"] == "gap"
        # 缺口即回填命令的 --start 定位（回填窗口显式呈现）
        assert coverage["000688"]["missing_days"] == ["20261014", "20261015"]
        assert idx["market_indices"]["covered_days"] == 1
        assert idx["market_indices"]["status"] == "gap"
        assert out["summary"]["indices"] == "gap"

    def test_empty_window_is_all_gap_but_structured(self, pg_ready):
        out = check_history_continuity(end=END, window_days=WINDOW)
        assert out["status"] == "gap"
        snap = out["faces"]["snapshot"]
        assert snap["covered_days"] == 0
        assert len(snap["gap_days"]) == 9
        assert snap["table_rows"] == 0 and snap["table_last_date"] is None

    def test_param_guards(self, pg_ready):
        assert check_history_continuity(faces="nope")["status"] == "error"
        assert check_history_continuity(window_days=0)["status"] == "error"
        assert check_history_continuity(end="2026-13-99")["status"] == "error"


# ---------------------------------------------------------------- 验收编排（fake 注入）

class _SpotFake:
    def __init__(self, name, rows):
        self.name = name
        self._rows = {r["index_code"]: [r] for r in rows}

    def fetch_index_spot(self, index_code):
        return self._rows.get(index_code, [])


class _LhbExchangeFake:
    name = "exchange-lhb"

    def fetch_lhb(self, trade_date):
        return {"lhb_basic": [{
            "stock_code": "600519", "lhb_reason": "日涨幅偏离值达7%",
            "day_amount": 42.0, "net_buy_amount": 1.5,
        }], "errors": []}


class _LhbSinaFake:
    name = "sina"

    def daily_detail(self, trade_date):
        return [{
            "stock_code": "600519", "amount_wan": 42.0, "seat_type": "4",
        }]

    def seat_detail(self, code, trade_date, seat_type):
        return [{"net": 1.5}]


def _run_acceptance(pg_ready, **kw):
    from fakes import FakeSnapshotAdapter, FakeSwBoardsAdapter

    spot_rows = [{
        "index_code": "000001", "index_name": "上证指数",
        "latest_price": 3000.0, "change_percent": 0.5,
        "change_amount": 15.0, "volume": 4.0e8, "amount": 4.0e7,
        "amplitude": 1.0, "high": 3020.0, "low": 2980.0, "open": 2990.0,
        "pre_close": 2985.0, "volume_ratio": 1.1, "trade_date": "20261023",
    }]
    defaults = dict(
        trade_date="20261023",
        probe=lambda: {"reachable": True, "banned": False, "detail": "fake"},
        sw_adapters=[FakeSwBoardsAdapter()],
        snapshot_adapters=[
            FakeSnapshotAdapter("tencent", trade_date="20261023"),
            FakeSnapshotAdapter("tencent_rank", trade_date=None),
            FakeSnapshotAdapter("sina", trade_date=None),
        ],
        index_adapters=[_SpotFake("tencent", spot_rows), _SpotFake("sina", spot_rows)],
        lhb_exchange=_LhbExchangeFake(),
        lhb_sina=_LhbSinaFake(),
    )
    defaults.update(kw)
    return run_parity_acceptance(**defaults)


class TestRunParityAcceptance:
    def test_all_faces_structure_and_rollups(self, pg_ready):
        _seed_continuity(pg_ready)
        out = _run_acceptance(pg_ready)
        assert out["status"] == "ok"  # 对拍源齐 = ok；连续性 gap 不进整体失败
        assert set(out["parity"]) == set(out["continuity"]["faces"]) == {
            "boards", "snapshot", "indices", "lhb", "zt_pool"}
        assert out["parity_summary"]["boards"] == "ok"
        assert out["parity_summary"]["indices"] == "ok"
        assert out["parity_summary"]["zt_pool"] == "infeasible"  # ADR 0012 记录决策
        zt = out["parity"]["zt_pool"]
        assert zt["probe"] == {"reachable": True, "banned": False, "detail": "fake"}
        assert "ADR 0012" in zt["note"]
        # 种子场景 1015 起无行无台账 → 连续性如实报断档（验收报告证据）
        assert out["continuity_summary"]["zt_pool"] == "gap"

    def test_probe_failure_is_evidence_not_error(self, pg_ready):
        _seed_continuity(pg_ready)

        def _boom_probe():
            raise RuntimeError("boom")

        out = _run_acceptance(pg_ready, probe=_boom_probe)
        assert out["status"] == "ok"
        assert out["parity"]["zt_pool"]["probe"]["reachable"] is False

    def test_probe_skipped_when_not_provided(self, pg_ready):
        out = _run_acceptance(pg_ready, probe=None)
        assert out["parity"]["zt_pool"]["probe"]["skipped"] is True

    def test_hard_error_when_parity_source_fails(self, pg_ready):
        _seed_continuity(pg_ready)
        out = _run_acceptance(pg_ready, index_adapters=[
            _SpotFake("tencent", []), _SpotFake("sina", []),
        ])
        # spot 全空 → error 进入整体失败
        assert out["parity"]["indices"]["spot"]["status"] == "error"
        assert out["status"] == "error"
        assert "indices" in out["error"]

    def test_faces_subset(self, pg_ready):
        _seed_continuity(pg_ready)
        out = _run_acceptance(pg_ready, faces="snapshot,zt_pool")
        assert set(out["parity"]) == {"snapshot", "zt_pool"}
        assert set(out["continuity"]["faces"]) == {"snapshot", "zt_pool"}

    def test_bad_params_are_errors(self, pg_ready):
        assert run_parity_acceptance(faces="nope")["status"] == "error"
        assert run_parity_acceptance(limit=0)["status"] == "error"
        assert run_parity_acceptance(trade_date="bad")["status"] == "error"
