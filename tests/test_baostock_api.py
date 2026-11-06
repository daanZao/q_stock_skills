"""BaostockApi 单例/会话/超时/重登/预算（issue #170，ADR 0019）。

主接缝：`BaostockApi.query` 与按数据能力命名的 fetch_* 方法；baostock
客户端是唯一被替换的系统边界（fake client），预算使用临时目录真实文件。
"""

import logging
from datetime import date
from types import SimpleNamespace

import pytest

from qstock_mcp.baostock_api import BaostockApi, QueryResult
from qstock_mcp.baostock_errors import (
    BaostockBudgetExhausted,
    BaostockTimeout,
    BaostockUpstreamError,
)
from qstock_mcp.baostock_lease import BaostockBudget


class FakeResult:
    def __init__(
        self,
        rows=None,
        *,
        error_code="0",
        error_msg="",
        fields=("code", "date"),
        cur_page_num="1",
    ):
        self.fields = list(fields)
        self.error_code = error_code
        self.error_msg = error_msg
        self.cur_page_num = cur_page_num
        self._rows = list(rows or [])
        self._index = 0

    def next(self):
        if self._index >= len(self._rows):
            return False
        self._index += 1
        return True

    def get_row_data(self):
        return self._rows[self._index - 1]


class FakeClient:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.login_calls = 0
        self.logout_calls = 0
        self.query_calls = []

    def login(self):
        self.login_calls += 1
        return SimpleNamespace(error_code="0", error_msg="")

    def logout(self):
        self.logout_calls += 1
        return SimpleNamespace(error_code="0", error_msg="")

    def _result(self):
        return self.results.pop(0) if self.results else FakeResult()

    def query_history_k_data_plus(self, code, fields, **kwargs):
        self.query_calls.append(("daily", code, fields, kwargs))
        return self._result()

    def query_daily_history_k_AStock(self, **kwargs):
        self.query_calls.append(("astock", kwargs))
        return self._result()

    def query_daily_adjust_factor(self, **kwargs):
        self.query_calls.append(("factor", kwargs))
        return self._result()

    def query_stock_basic(self, **kwargs):
        self.query_calls.append(("stock_list", kwargs))
        return self._result()

    def query_profit_data(self, **kwargs):
        self.query_calls.append(("profit", kwargs))
        return self._result()

    def query_growth_data(self, **kwargs):
        self.query_calls.append(("growth", kwargs))
        return self._result()


@pytest.fixture(autouse=True)
def _reset_singleton():
    BaostockApi.reset_instance()
    yield
    BaostockApi.reset_instance()


def _api(tmp_path, client, **kwargs):
    return BaostockApi.get(
        client_factory=lambda: client,
        budget=BaostockBudget(tmp_path / "baostock-budget.json"),
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_get_is_process_singleton(tmp_path):
    client = FakeClient()
    first = _api(tmp_path, client)
    second = BaostockApi.get()
    assert first is second


def test_session_logged_in_once_for_multiple_requests(tmp_path):
    client = FakeClient(
        [FakeResult([["sh.600519", "2026-10-16"]]), FakeResult([["sh.600519", "2026-10-17"]])]
    )
    api = _api(tmp_path, client)

    api.fetch_daily_adjust_factor("20261016")
    api.fetch_daily_adjust_factor("20261017")

    assert client.login_calls == 1
    assert client.logout_calls == 0
    assert client.query_calls == [
        ("factor", {"date": "2026-10-16"}),
        ("factor", {"date": "2026-10-17"}),
    ]


def test_close_logs_out_once_and_is_idempotent(tmp_path):
    client = FakeClient([FakeResult()])
    api = _api(tmp_path, client)
    api.fetch_stock_list()

    api.close()
    api.close()

    assert client.logout_calls == 1
    # 关闭后再次请求会重新登录
    client.results.append(FakeResult())
    api.fetch_stock_list()
    assert client.login_calls == 2


def test_query_counts_login_query_pages_and_logout(tmp_path):
    client = FakeClient([FakeResult([["a"], ["b"], ["c"]], cur_page_num="3")])
    api = _api(tmp_path, client)

    result = api.fetch_stock_list()
    assert result == QueryResult(
        fields=["code", "date"], rows=[["a"], ["b"], ["c"]], pages=3
    )
    budget = api.budget.snapshot()
    assert budget["login"] == 1
    assert budget["query"] == 1
    assert budget["page"] == 2
    assert budget["logout"] == 0
    assert budget["total"] == 4

    api.close()
    assert api.budget.snapshot()["logout"] == 1
    assert api.budget.snapshot()["total"] == 5


def test_session_kick_relogs_in_once_and_retries_same_request(tmp_path):
    client = FakeClient(
        [
            FakeResult(error_code="10001001", error_msg="用户未登录"),
            FakeResult([["sh.688300", "2026-10-16", "3.855379"]]),
        ]
    )
    api = _api(tmp_path, client)

    result = api.fetch_daily_adjust_factor("20261016")

    assert result.rows == [["sh.688300", "2026-10-16", "3.855379"]]
    assert client.login_calls == 2
    assert client.logout_calls == 1  # 被踢后重置旧会话
    assert [call for call in client.query_calls if call[0] == "factor"] == [
        ("factor", {"date": "2026-10-16"}),
        ("factor", {"date": "2026-10-16"}),
    ]


def test_session_kick_after_pagination_retries_same_request(tmp_path):
    client = FakeClient(
        [
            FakeResult([], error_code="10001001", error_msg="用户未登录"),
            FakeResult([["sh.600519", "2026-10-16"]]),
        ]
    )
    api = _api(tmp_path, client)

    result = api.fetch_stock_list()

    assert result.rows == [["sh.600519", "2026-10-16"]]
    assert client.login_calls == 2
    assert len(client.query_calls) == 2


def test_timeout_resets_session_and_retries_once(tmp_path):
    import time

    class SlowClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.slow_remaining = 2

        def query_stock_basic(self, **kwargs):
            self.query_calls.append(("stock_list", kwargs))
            if self.slow_remaining:
                self.slow_remaining -= 1
                time.sleep(0.05)
            return FakeResult()

    client = SlowClient()
    api = _api(tmp_path, client, timeout_s=0.01)

    with pytest.raises(BaostockTimeout) as exc:
        api.fetch_stock_list()

    assert "超时" in str(exc.value)
    assert client.login_calls == 2
    assert client.logout_calls == 2
    assert len(client.query_calls) == 2
    # 超时后会话已重置，下个请求会重登
    client.query_calls.clear()
    api.fetch_stock_list()
    assert client.login_calls == 3


def test_timeout_applies_in_worker_thread(tmp_path):
    import socket
    import threading

    class SlowClient(FakeClient):
        def login(self):
            self._reader, self._writer = socket.socketpair()
            return super().login()

        def logout(self):
            self._reader.close()
            self._writer.close()
            return super().logout()

        def query_stock_basic(self, **kwargs):
            self.query_calls.append(("stock_list", kwargs))
            self._reader.recv(1)
            return FakeResult()

    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(0.2)
    try:
        client = SlowClient()
        api = _api(tmp_path, client, timeout_s=0.01)
        errors = []

        def request():
            try:
                api.fetch_stock_list()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=request)
        thread.start()
        thread.join(1)

        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], BaostockTimeout)
        assert client.login_calls == 2
        assert len(client.query_calls) == 2
    finally:
        socket.setdefaulttimeout(previous_timeout)


def test_login_installs_socket_default_timeout_when_unset(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getdefaulttimeout", lambda: None)
    calls = []
    monkeypatch.setattr(
        socket, "setdefaulttimeout", lambda value: calls.append(value)
    )
    client = FakeClient([FakeResult()])
    api = _api(tmp_path, client)

    api.ensure_session()

    assert calls == [60.0]
    assert client.login_calls == 1


def test_transient_error_retries_once(tmp_path):
    client = FakeClient(
        [
            FakeResult(error_code="10002007", error_msg="网络接收错误"),
            FakeResult([["sh.600519", "2026-10-16"]]),
        ]
    )
    api = _api(tmp_path, client)

    result = api.fetch_stock_list()

    assert result.rows == [["sh.600519", "2026-10-16"]]
    assert client.login_calls == 2
    assert client.logout_calls == 1
    assert len(client.query_calls) == 2


def test_transient_error_fails_after_one_retry(tmp_path):
    client = FakeClient(
        [
            FakeResult(error_code="10002007", error_msg="网络接收错误"),
            FakeResult(error_code="10002007", error_msg="网络接收错误"),
        ]
    )
    api = _api(tmp_path, client)

    with pytest.raises(BaostockUpstreamError) as exc:
        api.fetch_stock_list()

    assert exc.value.details["error_code"] == "10002007"
    assert exc.value.details["retryable"] is True
    assert len(client.query_calls) == 2


def test_budget_stop_rejects_before_touching_client(tmp_path):
    budget = BaostockBudget(tmp_path / "baostock-budget.json")
    budget.record("query", 50_000)
    client = FakeClient()
    api = BaostockApi.get(
        client_factory=lambda: client,
        budget=budget,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(BaostockBudgetExhausted) as exc:
        api.fetch_stock_list()

    assert exc.value.details["budget"]["total"] == 50_000
    assert client.login_calls == 0
    assert client.query_calls == []


def test_budget_warning_logged_at_45k(tmp_path, caplog):
    budget = BaostockBudget(tmp_path / "baostock-budget.json")
    budget.record("query", 45_000)
    client = FakeClient([FakeResult()])
    api = BaostockApi.get(
        client_factory=lambda: client,
        budget=budget,
        sleep=lambda _seconds: None,
    )

    with caplog.at_level(logging.WARNING):
        api.fetch_stock_list()

    assert any("45,000" in record.message for record in caplog.records)


def test_fetch_daily_passes_client_arguments(tmp_path):
    client = FakeClient([FakeResult()])
    api = _api(tmp_path, client)

    api.fetch_daily(
        "sh.600519",
        "20260101",
        "20260131",
        fields="date,close",
        adjustflag="2",
    )

    assert client.query_calls == [
        (
            "daily",
            "sh.600519",
            "date,close",
            {
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
                "frequency": "d",
                "adjustflag": "2",
            },
        )
    ]


def test_fetch_astock_daily_and_index_daily_pass_expected_arguments(tmp_path):
    client = FakeClient([FakeResult(), FakeResult()])
    api = _api(tmp_path, client)

    api.fetch_astock_daily("20261016")
    api.fetch_index_daily(
        "sh.000300", "20260101", "20260131", fields="date,close"
    )

    assert client.query_calls[0] == ("astock", {"date": "2026-10-16"})
    assert client.query_calls[1] == (
        "daily",
        "sh.000300",
        "date,close",
        {
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "frequency": "d",
            "adjustflag": "3",
        },
    )


def test_unknown_action_in_budget_is_rejected(tmp_path):
    budget = BaostockBudget(tmp_path / "baostock-budget.json")
    with pytest.raises(ValueError):
        budget.record("other", 1)


def test_fetch_fundamentals_walks_quarters_and_collects_section_errors(tmp_path):
    fields = ["code", "year", "quarter", "value"]

    class FundamentalsClient(FakeClient):
        def query_profit_data(self, **kwargs):
            self.query_calls.append(("profit", kwargs))
            return FakeResult(
                [["sh.600519", "2026", "4", "1.23"]], fields=fields
            )

        def query_growth_data(self, **kwargs):
            self.query_calls.append(("growth", kwargs))
            if kwargs["quarter"] == 4:
                return FakeResult(error_code="10004001", error_msg="参数错误")
            return FakeResult(
                [["sh.600519", "2026", "3", "0.10"]], fields=fields
            )

    client = FundamentalsClient()
    api = _api(tmp_path, client)

    payload = api.fetch_fundamentals("sh.600519", today=date(2026, 11, 5))

    assert payload["profit"] == [
        {"code": "sh.600519", "year": "2026", "quarter": "4", "value": "1.23"}
    ]
    assert payload["growth"] == [
        {"code": "sh.600519", "year": "2026", "quarter": "3", "value": "0.10"}
    ]
    assert len(payload["errors"]) == 1
    assert "growth 2026Q4" in payload["errors"][0]
    assert "参数错误" in payload["errors"][0]
    assert [call[0] for call in client.query_calls] == [
        "profit",
        "growth",
        "growth",
    ]
    assert client.login_calls == 1


def test_fetch_fundamentals_raises_when_all_sections_empty(tmp_path):
    class EmptyClient(FakeClient):
        def query_profit_data(self, **kwargs):
            self.query_calls.append(("profit", kwargs))
            return FakeResult(error_code="10004001", error_msg="参数错误")

        def query_growth_data(self, **kwargs):
            self.query_calls.append(("growth", kwargs))
            return FakeResult(error_code="10004001", error_msg="参数错误")

    api = _api(tmp_path, EmptyClient())

    with pytest.raises(BaostockUpstreamError, match="基本面无数据"):
        api.fetch_fundamentals("sh.600519", today=date(2026, 11, 5))


class PagedResult(FakeResult):
    """按每页 N 行推进 cur_page_num 的 fake（模拟客户端翻页）。"""

    def __init__(self, rows, *, rows_per_page=2):
        super().__init__(rows)
        self._rows_per_page = rows_per_page
        self._served = 0

    def next(self):
        if self._served >= len(self._rows):
            return False
        self._served += 1
        self._index = self._served
        if self._served % self._rows_per_page == 0:
            self.cur_page_num = str(int(self.cur_page_num) + 1)
        return True


def test_pages_are_counted_incrementally(tmp_path):
    client = FakeClient(
        [PagedResult([["a"], ["b"], ["c"], ["d"]])]
    )
    api = _api(tmp_path, client)

    result = api.fetch_stock_list()

    assert result.rows == [["a"], ["b"], ["c"], ["d"]]
    assert result.pages == 3
    assert api.budget.snapshot()["page"] == 2


def test_page_that_reaches_50k_stops_before_next_page(tmp_path):
    budget = BaostockBudget(tmp_path / "baostock-budget.json")
    budget.record("query", 49_997)
    client = FakeClient([PagedResult([["a"], ["b"], ["c"], ["d"]])])
    api = BaostockApi.get(
        client_factory=lambda: client,
        budget=budget,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(BaostockBudgetExhausted):
        api.fetch_stock_list()

    snapshot = api.budget.snapshot()
    assert snapshot["total"] == 50_000
    assert snapshot["page"] == 1
    assert len(client.query_calls) == 1


def test_failed_page_request_is_counted_before_retry(tmp_path):
    class FlakyPagedResult(FakeResult):
        def __init__(self):
            super().__init__([["a"], ["b"]])
            self._served = 0

        def next(self):
            if self._served < 2:
                self._served += 1
                self._index = self._served
                return True
            self.error_code = "10002007"
            self.error_msg = "网络接收错误"
            return False

    client = FakeClient([FlakyPagedResult(), FakeResult([["ok"]])])
    api = _api(tmp_path, client)

    result = api.fetch_stock_list()

    assert result.rows == [["ok"]]
    assert len(client.query_calls) == 2
    assert api.budget.snapshot()["page"] == 1


def test_explicit_close_propagates_logout_failure(tmp_path):
    class LogoutFailureClient(FakeClient):
        def logout(self):
            self.logout_calls += 1
            raise RuntimeError("logout failed")

    client = LogoutFailureClient([FakeResult()])
    api = _api(tmp_path, client)
    api.fetch_stock_list()

    with pytest.raises(RuntimeError, match="logout failed"):
        api.close()

    assert client.logout_calls == 1
    api.close()
    assert client.logout_calls == 1
