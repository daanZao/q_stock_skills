"""qstock-scheduler 收盘后批测试（issue #82，spec #71/W5）。

CLI 直调 tools_* 核心层（不经 MCP 协议层）：一次执行完成收盘后批五 section
（boards/zt_pool/strong_stocks/lhb/indices），各 section 独立成败报告并落
采集台账；任一 section 失败 exit 1（失败明细在 stdout JSON），参数错误 exit 2。
"""

import json
from datetime import date

import psycopg2
import pytest

from qstock_mcp import scheduler
from qstock_mcp.db import ensure_schema
from fakes import FakeAdapter, FakeBoardAdapter

TODAY = date.today().strftime("%Y%m%d")


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


@pytest.fixture
def pg_ready(pg_test):
    _init(pg_test)
    return pg_test


def _run(capsys, argv, adapters=None, cons_adapters=()):
    # cons_adapters 缺省空链：概念成分积累（#84）直接失败、不触网
    rc = scheduler.main(argv, adapters=adapters, cons_adapters=cons_adapters)
    out = json.loads(capsys.readouterr().out)
    return rc, out


def _ledger_rows(dsn, section=None):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            sql = "SELECT trade_date, section, status, source, rows, error_kind FROM collection_log"
            args = ()
            if section:
                sql += " WHERE section = %s"
                args = (section,)
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


class TestCloseHappyPath:
    def test_all_sections_ok_exit_0(self, pg_ready, capsys):
        rc, out = _run(capsys, ["close"], adapters=[FakeBoardAdapter("fake")])
        assert rc == 0
        assert out["status"] == "ok"
        assert out["job"] == "close"
        assert out["trade_date"] == TODAY
        assert set(out["sections"]) == {
            "indices", "boards", "zt_pool", "strong_stocks", "lhb"}
        assert out["failed_sections"] == []
        assert all(s["status"] == "ok" for s in out["sections"].values())

    def test_ledger_records_all_sections(self, pg_ready, capsys):
        _run(capsys, ["close"], adapters=[FakeBoardAdapter("fake")])
        rows = [r for r in _ledger_rows(pg_ready)
                if r[1] in ("indices", "boards", "zt_pool", "strong_stocks", "lhb")]
        sections = {r[1] for r in rows}
        assert sections == {"indices", "boards", "zt_pool", "strong_stocks", "lhb"}
        assert all(r[0] == TODAY for r in rows)
        assert all(r[2] == "ok" for r in rows)
        assert all(r[3] == "fake" for r in rows)
        assert all(r[4] > 0 for r in rows)

    def test_rerun_idempotent(self, pg_ready, capsys):
        """重复执行幂等：业务键 upsert，台账 append-only 追加。"""
        _run(capsys, ["close"], adapters=[FakeBoardAdapter("fake")])
        _run(capsys, ["close"], adapters=[FakeBoardAdapter("fake")])
        rows = [r for r in _ledger_rows(pg_ready)
                if r[1] in ("indices", "boards", "zt_pool", "strong_stocks", "lhb")]
        assert len(rows) == 10  # 两轮各 5 行台账
        conn = psycopg2.connect(pg_ready)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM zt_pool")
                assert cur.fetchone()[0] == 1  # 业务表不重行
        finally:
            conn.close()


class TestClosePartialFailure:
    def test_one_section_fails_exit_1(self, pg_ready, capsys):
        adapters = [FakeBoardAdapter("fake", fail_sections=("lhb",))]
        rc, out = _run(capsys, ["close"], adapters=adapters)
        assert rc == 1
        assert out["status"] == "error"
        assert out["failed_sections"] == ["lhb"]
        assert out["sections"]["lhb"]["status"] == "error"
        assert out["sections"]["boards"]["status"] == "ok"

    def test_failure_recorded_in_ledger(self, pg_ready, capsys):
        adapters = [FakeBoardAdapter("fake", fail_sections=("lhb",))]
        _run(capsys, ["close"], adapters=adapters)
        rows = _ledger_rows(pg_ready, section="lhb")
        assert len(rows) == 1
        assert rows[0][2] == "error"
        assert rows[0][5] is not None  # error_kind

    def test_historical_date_realtime_sections_rejected(self, pg_ready, capsys):
        """历史日期：实时语义 section（indices/boards）被防线拒收，其余 section 正常。"""
        rc, out = _run(capsys, ["close", "--date", "20260105"],
                       adapters=[FakeBoardAdapter("fake")])
        assert rc == 1
        assert out["trade_date"] == "20260105"
        assert set(out["failed_sections"]) == {"indices", "boards"}
        assert out["sections"]["indices"]["error_kind"] == "unsupported"
        assert out["sections"]["zt_pool"]["status"] == "ok"
        # 拒收也落账（issue #77）：guard_reject 行留审计痕迹，业务零落库
        rows = _ledger_rows(pg_ready, section="indices")
        assert len(rows) == 1
        assert rows[0][2] == "error"
        assert rows[0][5] == "unsupported"
        assert len(_ledger_rows(pg_ready, section="zt_pool")) == 1


class TestCloseContract:
    def test_invalid_date_exit_1(self, pg_ready, capsys):
        rc, out = _run(capsys, ["close", "--date", "not-a-date"],
                       adapters=[FakeBoardAdapter("fake")])
        assert rc == 1
        assert out["status"] == "error"
        assert out["job"] == "close"

    def test_unexpected_exception_exit_1(self, capsys, monkeypatch):
        def _boom(**kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(scheduler, "fetch_board_snapshot", _boom)
        rc, out = _run(capsys, ["close"], adapters=[FakeBoardAdapter("fake")])
        assert rc == 1
        assert out["status"] == "error"
        assert "disk on fire" in out["error"]

    def test_missing_subcommand_exit_2(self, capsys):
        with pytest.raises(SystemExit) as e:
            scheduler.main([])
        assert e.value.code == 2


# ---------------------------------------------------------------- 个股日线固定入口（issue #112）

BAR = {
    "trade_date": "20261020",
    "open": 1321.01,
    "high": 1335.00,
    "low": 1316.14,
    "close": 1331.88,
    "volume": 3_413_600,
    "amount": 452539.90,
    "amplitude": 1.4234,
    "change_percent": 0.5215,
    "change_amount": 6.91,
    "turnover_rate": 0.27,
}


def _daily_run(capsys, argv, adapters):
    rc = scheduler.main(argv, daily_adapters=adapters)
    return rc, json.loads(capsys.readouterr().out)


def test_daily_check_ok(capsys):
    bar = dict(BAR)
    rc, out = _daily_run(
        capsys,
        ["daily", "--codes", "600519", "--date", "20261020", "--check"],
        [FakeAdapter("tencent", rows=[bar]),
         FakeAdapter("sina", rows=[bar]),
         FakeAdapter("baostock", rows=[bar])],
    )
    assert rc == 0
    assert out["job"] == "daily" and out["mode"] == "check"
    assert out["status"] == "ok"
    assert out["results"][0]["stock_code"] == "600519"


def test_daily_check_mismatch_exit_1(capsys):
    rc, out = _daily_run(
        capsys,
        ["daily", "--codes", "600519", "--date", "20261020", "--check"],
        [FakeAdapter("tencent", rows=[dict(BAR)]),
         FakeAdapter("sina", rows=[dict(BAR, close=1340.0)])],
    )
    assert rc == 1
    assert out["status"] == "mismatch"
    assert out["mismatch_count"] == 1


def test_daily_fetch_writes_rows_and_reports_source(pg_ready, capsys):
    rc, out = _daily_run(
        capsys,
        ["daily", "--codes", "600519", "--date", "20261020"],
        [FakeAdapter("tencent")],
    )
    assert rc == 0
    assert out["mode"] == "fetch"
    assert out["results"][0]["source"] == "tencent"
    assert out["results"][0]["rows_upserted"] == 1

    conn = psycopg2.connect(pg_ready)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, trade_date, source FROM stock_daily"
            " WHERE stock_code = '600519' AND trade_date = '20261020'"
        )
        assert cur.fetchone() == ("600519", "20261020", "tencent")
    conn.close()


def test_daily_input_conflicts_exit_1(capsys):
    rc, out = _daily_run(capsys, ["daily", "--codes", "600519", "--all"], [])
    assert rc == 1 and "互斥" in out["error"]
    rc, out = _daily_run(
        capsys,
        ["daily", "--codes", "600519", "--date", "20261020", "--start", "20261001"],
        [],
    )
    assert rc == 1 and "互斥" in out["error"]
    rc, out = _daily_run(capsys, ["daily"], [])
    assert rc == 1 and "--codes" in out["error"]


def test_daily_fetch_rerun_reports_zero_gap(pg_ready, capsys):
    """已补过的区间重跑零抓取：source 为 null 并带 zero-gap note（幂等）。"""
    argv = ["daily", "--codes", "600519", "--date", "20261020"]
    _daily_run(capsys, argv, [FakeAdapter("tencent")])
    rc, out = _daily_run(capsys, argv, [FakeAdapter("tencent")])
    assert rc == 0
    assert out["results"][0]["source"] is None
    assert out["results"][0]["rows_upserted"] == 0
    assert "零抓取" in out["results"][0]["note"]


# ---------------------------------------------------------------- 基本面透传固定入口（issue #115）

FUNDAMENTALS_PAYLOAD = {
    "financial_abstract": [{"选项": "常用指标", "指标": "营业总收入", "20251231": 1.7e11}],
    "valuation": {"总市值": [{"date": "2026-10-20", "value": 1.64e12}]},
}


def _fund_run(capsys, argv, adapters):
    rc = scheduler.main(argv, fundamentals_adapters=adapters)
    return rc, json.loads(capsys.readouterr().out)


def test_fundamentals_ok_reports_sections_and_planned_absent(capsys):
    from fakes import FakeFundamentalsAdapter

    rc, out = _fund_run(
        capsys,
        ["fundamentals", "--codes", "600519,000001"],
        [FakeFundamentalsAdapter("akshare")],
    )
    assert rc == 0
    assert out["job"] == "fundamentals"
    assert out["status"] == "ok"
    assert out["total"] == 2 and out["failed"] == 0
    assert [r["stock_code"] for r in out["results"]] == ["600519", "000001"]
    assert all(r["source"] == "akshare" for r in out["results"])
    assert all(
        r["sections"] == ["financial_abstract", "valuation"] for r in out["results"]
    )
    assert all("base_info" in r["planned_absent"] for r in out["results"])


def test_fundamentals_section_errors_reported_not_fatal(capsys):
    from fakes import FakeFundamentalsAdapter

    payload = dict(FUNDAMENTALS_PAYLOAD, errors=["valuation: 市净率: boom"])
    rc, out = _fund_run(
        capsys,
        ["fundamentals", "--codes", "600519"],
        [FakeFundamentalsAdapter("akshare", payload=payload)],
    )
    assert rc == 0
    r = out["results"][0]
    assert r["sections"] == ["financial_abstract", "valuation"]
    assert r["section_errors"] == ["valuation: 市净率: boom"]


def test_fundamentals_all_fail_exit_1_still_declares_planned_absent(capsys):
    from fakes import FakeFundamentalsAdapter

    rc, out = _fund_run(
        capsys,
        ["fundamentals", "--codes", "600519"],
        [FakeFundamentalsAdapter("akshare", fail_times=99),
         FakeFundamentalsAdapter("baostock", fail_times=99)],
    )
    assert rc == 1
    assert out["status"] == "error"
    r = out["results"][0]
    assert r["status"] == "error"
    assert "base_info" in r["planned_absent"]
    assert [a["source"] for a in r["attempted_sources"]] == ["akshare", "baostock"]


def test_fundamentals_requires_codes(capsys):
    rc, out = _fund_run(capsys, ["fundamentals"], [])
    assert rc == 1
    assert "--codes" in out["error"]
