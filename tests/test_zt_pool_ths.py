"""zt_pool THS 低频纪律 + strong_stocks 库内自算的工具层测试（issue #120）。

生产路径（adapters=None）走 tools_board 的新承载：zt_pool → ThsAdapter
（每日一次台账守卫、max_retries=0、封禁→upstream_unreachable、单池失败
partial_error）；strong_stocks → tools_strong 库内自算。真实 ThsAdapter 以
monkeypatch 换成脚本 fake，不触网；落库断言打真实 PG 测试库。
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
import pytest

from qstock_mcp import scheduler
from qstock_mcp.adapters import FetchError
from qstock_mcp.adapters import ths_adapter
from qstock_mcp.adapters.base import ZT_POOL_FIELDS
from qstock_mcp.db import ensure_schema
from qstock_mcp.ledger import select_collection_log
from qstock_mcp.repository import upsert_board_rows, upsert_daily
from qstock_mcp.tools_board import fetch_board_snapshot
from qstock_mcp.tools_strong import (
    HIGH_WINDOW_DAYS,
    MULTI_ZT_WINDOW_DAYS,
    compute_strong_rows,
)

TODAY = date.today().strftime("%Y%m%d")
FIXTURES = Path(__file__).parent / "fixtures" / "ths"


def _init(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def _table_rows(pg_test, table, trade_date):
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {table} WHERE trade_date = %s ORDER BY stock_code",
            (trade_date,),
        )
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def _ledger(pg_test, section):
    conn = psycopg2.connect(pg_test)
    rows = select_collection_log(conn, trade_date=TODAY, section=section,
                                 limit=20)
    conn.close()
    return rows


class FakeThsAdapter:
    """协议级 fake：fetch_zt_pool 返回已映射行；异常脚本模拟封禁/失败。"""

    name = "ths"

    def __init__(self, rows=None, exc=None):
        self._rows = rows
        self._exc = exc
        self.pool_errors: dict[str, str] = {}
        self.calls = 0

    def fetch_zt_pool(self, trade_date):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._rows if self._rows is not None else _fixture_rows()


def _fixture_rows() -> list[dict]:
    rows = []
    for pool_type, fn in [("zt", "limit_up_pool.json"),
                          ("dt", "down_limit_pool.json"),
                          ("zb", "open_limit_pool.json")]:
        payload = json.loads((FIXTURES / fn).read_text())
        from qstock_mcp.adapters._board_ths import map_ths_pool_rows
        from qstock_mcp.adapters.ths_adapter import parse_pool_payload

        items, _ = parse_pool_payload(payload, pool_type)
        rows += map_ths_pool_rows(items, pool_type)
    return rows


@pytest.fixture
def ths_factory(monkeypatch):
    """替换 tools_board 内 `from .adapters.ths_adapter import ThsAdapter` 的目标。"""

    def _install(adapter: FakeThsAdapter):
        monkeypatch.setattr(ths_adapter, "ThsAdapter",
                            lambda: adapter)
        return adapter

    return _install


class TestZtPoolThsPath:
    def test_three_pools_land_with_ths_fields(self, pg_test, ths_factory):
        _init(pg_test)
        fake = ths_factory(FakeThsAdapter())
        result = fetch_board_snapshot(trade_date=TODAY, sections=["zt_pool"])
        assert result["status"] == "ok"
        section = result["sections"]["zt_pool"]
        assert section["status"] == "ok"
        assert section["source"] == "ths"
        assert section["rows"] == 4  # 2 zt + 1 dt + 1 zb
        assert fake.calls == 1
        rows = _table_rows(pg_test, "zt_pool", TODAY)
        assert {r["pool_type"] for r in rows} == {"zt", "dt", "zb"}
        zt = [r for r in rows if r["pool_type"] == "zt" and
              r["stock_code"] == "603123"][0]
        assert zt["consecutive_boards"] == 2
        assert zt["sealed_amount"] == pytest.approx(12500.0)
        assert zt["reason_type"] == "医药+原料药"
        assert zt["limit_up_type"] == "换手板"
        assert zt["limit_up_time"] == "093000"
        zb = [r for r in rows if r["pool_type"] == "zb"][0]
        assert zb["zb_info"] == "3"
        # 台账：单次 ok 行，source=ths
        (log,) = _ledger(pg_test, "zt_pool")
        assert log["status"] == "ok"
        assert log["source"] == "ths"

    def test_daily_once_guard_skips_second_run(self, pg_test, ths_factory):
        _init(pg_test)
        fake = ths_factory(FakeThsAdapter())
        first = fetch_board_snapshot(trade_date=TODAY, sections=["zt_pool"])
        assert first["sections"]["zt_pool"]["rows"] == 4
        second = fetch_board_snapshot(trade_date=TODAY, sections=["zt_pool"])
        section = second["sections"]["zt_pool"]
        assert section["status"] == "ok"
        assert section["rows"] == 4
        assert "每日一次" in section["note"]
        assert fake.calls == 1  # 第二次未打上游
        logs = _ledger(pg_test, "zt_pool")
        assert len(logs) == 2  # 采集 + skip 各一行（append-only）
        assert "skip" in (logs[0]["detail"] or "")

    def test_ban_is_upstream_unreachable_and_aborts(self, pg_test, ths_factory):
        _init(pg_test)
        fake = ths_factory(FakeThsAdapter(
            exc=FetchError("同花顺 IP 级封禁（Nginx forbidden）",
                           kind="connection")))
        result = fetch_board_snapshot(trade_date=TODAY, sections=["zt_pool"])
        assert result["status"] == "error"  # 核心 section 中止线
        assert result["error_kind"] == "upstream_unreachable"
        section = result["sections"]["zt_pool"]
        assert section["error_kind"] == "upstream_unreachable"
        assert "次日" in section["retry_advice"]
        (log,) = _ledger(pg_test, "zt_pool")
        assert log["status"] == "error"
        assert log["error_kind"] == "upstream_unreachable"
        assert fake.calls == 1  # 单次尝试，无同源重试

    def test_partial_pool_failure_lands_and_reports(self, pg_test, ths_factory):
        _init(pg_test)
        fake = FakeThsAdapter(rows=_fixture_rows())
        fake.pool_errors = {"dt": "THS down_limit_pool HTTP 错误：status=404"}
        ths_factory(fake)
        result = fetch_board_snapshot(trade_date=TODAY, sections=["zt_pool"])
        section = result["sections"]["zt_pool"]
        assert section["status"] == "ok"
        assert "dt" in section["partial_error"]
        assert section["rows"] == 4
        (log,) = _ledger(pg_test, "zt_pool")
        assert log["status"] == "ok"
        assert "dt" in (log["detail"] or "")

    def test_injected_adapters_keep_old_fetch_path(self, pg_test):
        """注入适配器（测试缝）走原 fetch 路径：fake 无 Ths 纪律语义。"""
        _init(pg_test)
        from fakes import FakeBoardAdapter, ZT_POOL_ROWS

        result = fetch_board_snapshot(
            trade_date=TODAY, sections=["zt_pool"],
            adapters=[FakeBoardAdapter("fake")])
        assert result["sections"]["zt_pool"]["source"] == "fake"
        assert _table_rows(pg_test, "zt_pool", TODAY)[0]["stock_code"] == \
            ZT_POOL_ROWS[0]["stock_code"]


def _recent_days(n: int) -> list[str]:
    """最近 n 个交易日口径的自然日序列（含今日，倒序）。

    TODAY 恒在列（测试不因周末漏今日）；自然日向前取足 n 个（周末跳过）。
    """
    base = datetime.strptime(TODAY, "%Y%m%d").date()
    days: list[str] = []
    i = 0
    while len(days) < n:
        d = (base - timedelta(days=i)).strftime("%Y%m%d")
        if d == TODAY or datetime.strptime(d, "%Y%m%d").date().weekday() < 5:
            days.append(d)
        i += 1
    return days


def _seed_daily(pg_test, code: str, closes: dict[str, float]):
    """closes: {trade_date: close}，其余字段确定性填充。"""
    rows = []
    for d, close in sorted(closes.items()):
        row = {"trade_date": d, "close": float(close)}
        rows.append(row)
    conn = psycopg2.connect(pg_test)
    upsert_daily(conn, code, "qfq",
                 [{**r, "open": r["close"], "high": r["close"],
                   "low": r["close"], "volume": 1000, "amount": 5000.0,
                   "amplitude": 1.0, "change_percent": 1.0,
                   "change_amount": 0.1, "turnover_rate": 2.0} for r in rows],
                 "fake")
    conn.close()


def _seed_zt(pg_test, entries: list[tuple[str, str, int | None]]):
    """(stock_code, trade_date, consecutive_boards) 直接落 zt_pool。"""
    conn = psycopg2.connect(pg_test)
    rows = [
        {"pool_type": "zt", "stock_code": code, "stock_name": f"股{code}",
         "consecutive_boards": boards, "trade_date": d}
        for code, d, boards in entries
    ]
    upsert_board_rows(conn, "zt_pool", "trade_date",
                      ("pool_type", "stock_code"), ZT_POOL_FIELDS,
                      TODAY, rows)
    conn.close()


def _seed_stock_list(pg_test, codes: list[str]):
    conn = psycopg2.connect(pg_test)
    with conn.cursor() as cur:
        for code in codes:
            cur.execute(
                "INSERT INTO stock_list (stock_code, stock_name, source)"
                " VALUES (%s, %s, 'fake') ON CONFLICT (stock_code)"
                " DO UPDATE SET stock_name = EXCLUDED.stock_name",
                (code, f"名称{code}"))
    conn.commit()
    conn.close()


class TestStrongSelfCalc:
    def _seed_inputs(self, pg_test):
        """A=60日新高；B=近10日多次涨停（含今日）；C=无；D=次新股新高。

        days[0]=今日、days[-1]=最早（倒序）；60 日窗口 = days[:60]。
        """
        days = _recent_days(HIGH_WINDOW_DAYS)
        window = days
        recent10 = days[:MULTI_ZT_WINDOW_DAYS]
        ascending = {d: 10.0 + i * 0.1 for i, d in enumerate(reversed(window))}
        descending = {d: 10.0 - i * 0.01 for i, d in enumerate(reversed(window))}
        _seed_stock_list(pg_test, ["600001", "600002", "600003", "600004"])
        _seed_daily(pg_test, "600001", ascending)   # 今日 = 窗口最高收盘
        _seed_daily(pg_test, "600002", descending)  # 今日低于窗口最高
        _seed_daily(pg_test, "600003", descending)
        _seed_daily(pg_test, "600004",
                    {d: 5.0 + i * 0.1 for i, d in enumerate(reversed(days[:3]))})
        _seed_zt(pg_test, [
            ("600002", TODAY, 2),
            ("600002", recent10[3], 1),
            ("600002", recent10[6], 1),
        ])

    def test_selfcalc_selects_and_reasons(self, pg_test):
        _init(pg_test)
        self._seed_inputs(pg_test)
        result = fetch_board_snapshot(trade_date=TODAY,
                                      sections=["strong_stocks"])
        assert result["status"] == "ok"
        section = result["sections"]["strong_stocks"]
        assert section["source"] == "selfcalc"
        assert section["status"] == "ok"
        rows = _table_rows(pg_test, "strong_stocks", TODAY)
        by_code = {r["stock_code"]: r for r in rows}
        assert set(by_code) == {"600001", "600002", "600004"}
        assert by_code["600001"]["reason"] == "60日新高"
        # ascending 序列今日 = 窗口最高收盘（值由 _seed_inputs 的窗口长度决定）
        assert by_code["600001"]["latest_price"] == pytest.approx(
            10.0 + (HIGH_WINDOW_DAYS - 1) * 0.1)
        assert "多次涨停" in by_code["600002"]["reason"]
        assert "3 次" in by_code["600002"]["reason"]
        assert by_code["600002"]["consecutive_boards"] == 2  # 取自今日 zt 行
        assert by_code["600002"]["stock_name"] == "名称600002"
        assert by_code["600004"]["reason"] == "60日新高"  # 次新股短窗口同样成立
        (log,) = _ledger(pg_test, "strong_stocks")
        assert log["status"] == "ok"
        assert log["source"] == "selfcalc"

    def test_missing_daily_is_no_data_not_failure(self, pg_test):
        _init(pg_test)
        result = fetch_board_snapshot(trade_date=TODAY,
                                      sections=["strong_stocks"])
        assert result["status"] == "ok"  # 非故障，不中止
        section = result["sections"]["strong_stocks"]
        assert section["status"] == "ok"
        assert section["empty_kind"] == "no_data_for_date"
        assert "跳过自算" in section["note"]
        assert _table_rows(pg_test, "strong_stocks", TODAY) == []
        (log,) = _ledger(pg_test, "strong_stocks")
        assert log["status"] == "empty"
        assert log["error_kind"] == "no_data_for_date"
        assert "selfcalc_skip" in (log["detail"] or "")

    def test_min_zt_count_threshold(self, pg_test):
        """涨停次数 < 阈值不入选（单独口径验证，不依赖 seed 组合）。"""
        _init(pg_test)
        days = _recent_days(HIGH_WINDOW_DAYS)
        window = days
        _seed_daily(pg_test, "600009",
                    {d: 10.0 - i * 0.01 for i, d in enumerate(reversed(window))})
        _seed_zt(pg_test, [("600009", TODAY, 1)])  # 仅 1 次涨停
        conn = psycopg2.connect(pg_test)
        rows = compute_strong_rows(conn, TODAY)
        conn.close()
        assert rows == []  # 非新高 + 涨停次数不足

    def test_schema_migration_idempotent(self, pg_test):
        """017 迁移幂等：ensure_schema 重复执行列不重复、列存在。"""
        _init(pg_test)
        _init(pg_test)  # 第二次执行全部 sql/*.sql（含 017 ALTER）
        conn = psycopg2.connect(pg_test)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sealed_amount, reason_type FROM zt_pool LIMIT 0")
        conn.close()


class TestLimitpoolCli:
    def test_limitpool_happy_path(self, pg_test, ths_factory, capsys):
        _init(pg_test)
        fake = ths_factory(FakeThsAdapter())
        TestStrongSelfCalc()._seed_inputs(pg_test)
        rc = scheduler.main(["limitpool", "--date", TODAY])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["job"] == "limitpool"
        assert out["trade_date"] == TODAY
        assert out["failed_sections"] == []
        assert out["sections"]["zt_pool"]["source"] == "ths"
        assert out["sections"]["strong_stocks"]["source"] == "selfcalc"
        assert fake.calls == 1

    def test_limitpool_ban_exit_1(self, pg_test, ths_factory, capsys):
        _init(pg_test)
        ths_factory(FakeThsAdapter(
            exc=FetchError("同花顺 IP 级封禁", kind="connection")))
        rc = scheduler.main(["limitpool", "--date", TODAY])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["status"] == "error"
        assert out["error_kind"] == "upstream_unreachable"

    def test_limitpool_probe(self, pg_test, monkeypatch, capsys):
        _init(pg_test)
        monkeypatch.setattr(ths_adapter, "probe",
                            lambda: {"reachable": True, "banned": False,
                                     "detail": "HTTP 200，页面正常"})
        rc = scheduler.main(["limitpool", "--probe"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["mode"] == "probe"
        assert out["banned"] is False

    def test_limitpool_probe_banned_exit_1(self, pg_test, monkeypatch, capsys):
        _init(pg_test)
        monkeypatch.setattr(ths_adapter, "probe",
                            lambda: {"reachable": True, "banned": True,
                                     "detail": "HTTP 403，命中封禁页"})
        rc = scheduler.main(["limitpool", "--probe"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["banned"] is True
