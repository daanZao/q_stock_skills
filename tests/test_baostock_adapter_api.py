"""BaostockAdapter 公开数据方法委托 BaostockApi 的契约（issue #171）。"""

from types import SimpleNamespace

import pytest

import qstock_mcp.adapters.baostock_adapter as module
from qstock_mcp.baostock_api import QueryResult
from qstock_mcp.baostock_lease import BaostockLease


class FakeApi:
    def __init__(self):
        self.factor_dates = []

    def fetch_daily_adjust_factor(self, trade_date):
        self.factor_dates.append(trade_date)
        return QueryResult(
            fields=[
                "code",
                "dividOperateDate",
                "foreAdjustFactor",
                "backAdjustFactor",
                "adjustFacto",
            ],
            rows=[["sh.688300", "2026-10-16", "1.0", "3.855379", "3.855379"]],
            pages=1,
        )


def _install_api(monkeypatch, tmp_path, api):
    BaostockLease(tmp_path).acquire(owner="test")
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(
        module, "BaostockApi", SimpleNamespace(get=lambda: api), raising=False
    )
    monkeypatch.setattr(
        module,
        "_login",
        lambda: pytest.fail("旧 _login 路径不得执行"),
        raising=False,
    )


def test_fetch_daily_adjust_factor_delegates_to_api(monkeypatch, tmp_path):
    api = FakeApi()
    _install_api(monkeypatch, tmp_path, api)

    events = module.BaostockAdapter().fetch_daily_adjust_factor("20261016")

    assert api.factor_dates == ["20261016"]
    assert events == [
        {"stock_code": "688300", "ex_date": "20261016", "factor_cum": 3.855379}
    ]


def test_fetch_daily_delegates_to_api_and_preserves_mapping(monkeypatch, tmp_path):
    class DailyApi(FakeApi):
        def fetch_daily(self, code, start, end, *, fields, adjustflag):
            self.daily_call = (code, start, end, fields, adjustflag)
            return QueryResult(
                fields=["date", "close", "amount"],
                rows=[["2026-10-16", "102.70", "10270000"]],
                pages=1,
            )

    api = DailyApi()
    _install_api(monkeypatch, tmp_path, api)

    rows = module.BaostockAdapter().fetch_daily(
        "688300", "20261016", "20261016", adj="none"
    )

    assert api.daily_call == (
        "sh.688300",
        "20261016",
        "20261016",
        module._FIELDS,
        "3",
    )
    assert rows[0]["close"] == 102.70
    assert rows[0]["amount"] == 1027.0


def test_fetch_astock_daily_delegates_to_api(monkeypatch, tmp_path):
    class AStockApi(FakeApi):
        def fetch_astock_daily(self, trade_date):
            self.astock_date = trade_date
            return QueryResult(
                fields=["date", "code", "close", "adjustflag"],
                rows=[["2026-10-16", "sh.600519", "1700.0", "3"]],
                pages=1,
            )

    api = AStockApi()
    _install_api(monkeypatch, tmp_path, api)

    rows = module.BaostockAdapter().fetch_astock_daily("20261016")

    assert api.astock_date == "20261016"
    assert rows == [
        {
            "stock_code": "600519",
            "trade_date": "20261016",
            "open": None,
            "high": None,
            "low": None,
            "close": 1700.0,
            "preclose": None,
            "volume": None,
            "amount": None,
            "amplitude": None,
            "change_percent": None,
            "change_amount": None,
            "turnover_rate": None,
            "tradestatus": None,
            "adj": "none",
        }
    ]


def test_fetch_stock_list_delegates_to_api(monkeypatch, tmp_path):
    class StockListApi(FakeApi):
        def fetch_stock_list(self):
            self.stock_list_called = True
            return QueryResult(
                fields=["code", "code_name", "status"],
                rows=[
                    ["sh.600519", "贵州茅台", "1"],
                    ["sh.000001", "上证指数", "1"],
                ],
                pages=1,
            )

    api = StockListApi()
    _install_api(monkeypatch, tmp_path, api)

    rows = module.BaostockAdapter().fetch_stock_list()

    assert api.stock_list_called is True
    assert rows == [{"stock_code": "600519", "stock_name": "贵州茅台"}]


def test_fetch_index_daily_delegates_to_api(monkeypatch, tmp_path):
    class IndexApi(FakeApi):
        def fetch_index_daily(self, code, start, end, *, fields):
            self.index_call = (code, start, end, fields)
            return QueryResult(
                fields=["date", "close", "turn"],
                rows=[["2026-10-16", "102.70", "0.1"]],
                pages=1,
            )

    api = IndexApi()
    _install_api(monkeypatch, tmp_path, api)

    rows = module.BaostockAdapter().fetch_index_daily(
        "399001", "20261016", "20261016"
    )

    assert api.index_call == (
        "sz.399001",
        "20261016",
        "20261016",
        module._FIELDS,
    )
    assert rows[0]["close"] == 102.70
    assert rows[0]["turnover_rate"] == 0.1


def test_fetch_fundamentals_delegates_to_api(monkeypatch, tmp_path):
    class FundamentalsApi(FakeApi):
        def fetch_fundamentals(self, code):
            self.fundamentals_code = code
            return {
                "profit": [{"code": code, "roeAvg": "12.3"}],
                "errors": ["growth 2026Q4: 空返回"],
            }

    api = FundamentalsApi()
    _install_api(monkeypatch, tmp_path, api)

    payload = module.BaostockAdapter().fetch_fundamentals("600519")

    assert api.fundamentals_code == "sh.600519"
    assert payload == {
        "profit": [{"code": "sh.600519", "roeAvg": "12.3"}],
        "errors": ["growth 2026Q4: 空返回"],
    }


def test_api_timeout_is_connection_fetch_error(monkeypatch, tmp_path):
    from qstock_mcp.adapters.base import fetch_failure_kind
    from qstock_mcp.baostock_errors import BaostockTimeout

    class TimeoutApi(FakeApi):
        def fetch_stock_list(self):
            raise BaostockTimeout("socket 超时", timeout_s=60)

    api = TimeoutApi()
    _install_api(monkeypatch, tmp_path, api)

    with pytest.raises(module.FetchError) as exc:
        module.BaostockAdapter().fetch_stock_list()

    assert fetch_failure_kind(exc.value) == "connection"


def test_missing_lease_interrupts_fallback_without_retry(monkeypatch, tmp_path):
    from fakes import FakeAdapter
    from qstock_mcp.baostock_errors import BaostockLocked
    from qstock_mcp.fetch_chain import fetch_with_fallback

    api = FakeApi()
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(
        module, "BaostockApi", SimpleNamespace(get=lambda: api), raising=False
    )
    monkeypatch.setattr(
        module,
        "_login",
        lambda: pytest.fail("旧 _login 路径不得执行"),
        raising=False,
    )
    primary = FakeAdapter("primary", fail_times=3)

    with pytest.raises(BaostockLocked):
        fetch_with_fallback(
            [primary, module.BaostockAdapter()],
            "600519",
            "20261016",
            "20261016",
        )

    assert len(primary.calls) == 3


def test_proxy_failure_is_not_retried_again_by_fallback(monkeypatch, tmp_path):
    from qstock_mcp.baostock_errors import BaostockUpstreamError
    from qstock_mcp.fetch_chain import AllSourcesFailed, fetch_stock_list_with_fallback

    class TransientApi(FakeApi):
        def fetch_stock_list(self):
            self.calls = getattr(self, "calls", 0) + 1
            raise BaostockUpstreamError("瞬时错误", retryable=True)

    api = TransientApi()
    _install_api(monkeypatch, tmp_path, api)

    with pytest.raises(AllSourcesFailed) as exc:
        fetch_stock_list_with_fallback([module.BaostockAdapter()])

    assert api.calls == 1
    assert exc.value.attempted[0]["attempts"] == 1
    assert exc.value.attempted[0]["failure_kind"] == "connection"
