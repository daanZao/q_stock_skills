"""qstock-scheduler parity / stock-list 子命令测试（issue #122，spec #111）。

parity：总验收对拍编排的 CLI 薄包装——fake 适配器注入跑通五面结构、
--faces 子集、--no-probe、源失败 exit 1；stock-list：清单抓取落库 +
台账（section=stock_list）与 --check 互证（不落库不写台账）。不触真实网络。
"""

import json

import psycopg2
import pytest

from qstock_mcp import scheduler
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.db import ensure_schema

from fakes import FakeSnapshotAdapter, FakeSwBoardsAdapter


@pytest.fixture
def pg_ready(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()
    return pg_test


def _run(capsys, argv, **main_kw):
    rc = scheduler.main(argv, **main_kw)
    out = json.loads(capsys.readouterr().out)
    return rc, out


_SPOT_ROWS = [{
    "index_code": "000001", "index_name": "上证指数",
    "latest_price": 3000.0, "change_percent": 0.5,
    "change_amount": 15.0, "volume": 4.0e8, "amount": 4.0e7,
    "amplitude": 1.0, "high": 3020.0, "low": 2980.0, "open": 2990.0,
    "pre_close": 2985.0, "volume_ratio": 1.1, "trade_date": "20261023",
}]


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
        return [{"stock_code": "600519", "amount_wan": 42.0, "seat_type": "4"}]

    def seat_detail(self, code, trade_date, seat_type):
        return [{"net": 1.5}]


def _main_kwargs():
    """注入全 fake 链的 main() 参数（parity 子命令经由 main 的适配器缝）。"""
    return dict(
        adapters=[FakeSwBoardsAdapter()],
        snapshot_adapters=[
            FakeSnapshotAdapter("tencent", trade_date="20261023"),
            FakeSnapshotAdapter("tencent_rank", trade_date=None),
            FakeSnapshotAdapter("sina", trade_date=None),
        ],
        index_adapters=[_SpotFake("tencent", _SPOT_ROWS), _SpotFake("sina", _SPOT_ROWS)],
    )


class TestParityCommand:
    def test_parity_all_faces_exit_0(self, pg_ready, capsys, monkeypatch):
        monkeypatch.setattr(
            "qstock_mcp.adapters.ths_adapter.probe",
            lambda: {"reachable": True, "banned": False, "detail": "fake"},
        )
        # lhb 面在 CLI 层无适配器注入缝（compare_lhb_sources 客户端直连），
        # 测试 monkeypatch 替换；其本身行为由 test_parity_acceptance 核心层覆盖
        monkeypatch.setattr(
            "qstock_mcp.scheduler.compare_lhb_sources",
            lambda trade_date=None, exchange=None, sina=None: {
                "status": "ok", "tool": "compare_lhb_sources",
                "params": {"trade_date": trade_date}, "results": [],
                "mismatch_count": 0,
            },
        )
        rc, out = _run(capsys, ["parity", "--date", "20261023"], **_main_kwargs())
        assert rc == 0
        assert out["job"] == "parity" and out["status"] == "ok"
        assert set(out["parity"]) == {"boards", "snapshot", "indices", "lhb", "zt_pool"}
        assert out["parity"]["zt_pool"]["status"] == "infeasible"
        assert out["parity"]["zt_pool"]["probe"]["banned"] is False
        assert out["continuity"]["status"] == "gap"  # 空库如实报断档

    def test_parity_faces_subset(self, pg_ready, capsys):
        rc, out = _run(
            capsys, ["parity", "--date", "20261023", "--faces", "snapshot",
                     "--no-probe"], **_main_kwargs())
        assert rc == 0
        assert set(out["parity"]) == {"snapshot"}
        assert out["parity_summary"] == {"snapshot": "ok"}

    def test_parity_source_failure_exit_1(self, pg_ready, capsys):
        kw = _main_kwargs()
        kw["index_adapters"] = [_SpotFake("tencent", []), _SpotFake("sina", [])]
        rc, out = _run(
            capsys, ["parity", "--date", "20261023", "--faces", "indices",
                     "--no-probe"], **kw)
        assert rc == 1
        assert out["status"] == "error"
        assert "indices" in out["error"]

    def test_parity_bad_faces_exit_1(self, pg_ready, capsys):
        rc, out = _run(capsys, ["parity", "--faces", "nope"], **_main_kwargs())
        assert rc == 1
        assert out["status"] == "error" and "face" in out["error"]


class _ListFake:
    """清单链 fake：仅 fetch_stock_list（fallback 内核只调此能力）。"""

    def __init__(self, name, rows=None, exc=None):
        self.name = name
        self._rows = rows
        self._exc = exc

    def fetch_stock_list(self):
        if self._exc is not None:
            raise self._exc
        return self._rows


def _ledger_rows(dsn):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, section, status, source, rows"
                " FROM collection_log WHERE section = 'stock_list'")
            return cur.fetchall()
    finally:
        conn.close()


class TestStockListCommand:
    def test_fetch_upserts_and_ledger(self, pg_ready, capsys):
        rows = [{"stock_code": "600519", "stock_name": "贵州茅台"},
                {"stock_code": "920001", "stock_name": "北交样本"}]
        rc, out = _run(capsys, ["stock-list"],
                       list_adapters=[_ListFake("baostock", rows)])
        assert rc == 0
        assert out["status"] == "ok" and out["mode"] == "fetch"
        assert out["source"] == "baostock" and out["rows_upserted"] == 2
        assert out["bse_rows"] == 1
        ledger = _ledger_rows(pg_ready)
        assert len(ledger) == 1
        assert ledger[0][1] == "stock_list" and ledger[0][2] == "ok"
        assert ledger[0][3] == "baostock" and ledger[0][4] == 2

    def test_fetch_chain_failure_exit_1(self, pg_ready, capsys):
        conn_exc = FetchError("efinance 连接重置")
        conn_exc.__cause__ = ConnectionResetError("reset")
        rc, out = _run(capsys, ["stock-list"],
                       list_adapters=[_ListFake("efinance", exc=conn_exc)])
        assert rc == 1
        assert out["status"] == "error"
        assert out["error_kind"] == "upstream_unreachable"
        ledger = _ledger_rows(pg_ready)
        assert len(ledger) == 1 and ledger[0][2] == "error"

    def test_check_mode_no_ledger(self, pg_ready, capsys):
        rows = [{"stock_code": "600519", "stock_name": "贵州茅台"}]
        rc, out = _run(capsys, ["stock-list", "--check"],
                       list_adapters=[_ListFake("baostock", rows)])
        assert rc == 0
        assert out["job"] == "stock-list" and out["mode"] == "check"
        assert out["status"] == "ok" and out["reference_source"] == "baostock"
        assert _ledger_rows(pg_ready) == []  # 对拍模式不写台账

    def test_fetch_defaults_to_chain_when_no_injection(self, pg_ready, capsys,
                                                       monkeypatch):
        """未注入适配器时经 default_adapters 生产链（None 不得 TypeError）。"""
        seen = {}

        def _fake_default():
            seen["called"] = True
            return [_ListFake("baostock", [{"stock_code": "600519", "stock_name": "茅台"}])]

        monkeypatch.setattr("qstock_mcp.adapters.default_adapters", _fake_default)
        rc, out = _run(capsys, ["stock-list"])
        assert rc == 0
        assert seen.get("called") is True
        assert out["source"] == "baostock" and out["rows_upserted"] == 1
