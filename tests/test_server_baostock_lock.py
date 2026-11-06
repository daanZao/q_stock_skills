"""MCP 实例显式租约与数据工具入口门禁（#173，ADR 0019）。"""

import asyncio
import json
from types import SimpleNamespace

from qstock_mcp import scheduler, server
from qstock_mcp.baostock_api import BaostockApi
from qstock_mcp.baostock_lease import BaostockBudget, BaostockLease, LeaseIdentity


def _status(capsys):
    rc = scheduler.main(["baostock-lock", "status"])
    return rc, json.loads(capsys.readouterr().out)


def test_mcp_status_without_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    out = server.baostock_lock("status")
    assert out["status"] == "ok"
    assert out["action"] == "status"
    assert out["leased"] is False
    assert out["budget"]["total"] == 0


def test_mcp_acquire_release_shares_files_with_cli_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))

    acquired = server.baostock_lock(
        "acquire", owner="qstock-mcp", reason="daily-review"
    )
    assert acquired["status"] == "ok"
    assert acquired["leased"] is True
    assert acquired["lock"]["owner"] == "qstock-mcp"
    assert acquired["lock"]["entry"] == "mcp"
    assert acquired["lock"]["instance_id"] == server.MCP_LEASE_CONTEXT.identity.instance_id
    assert acquired["lock"]["command"] == "mcp:baostock_lock"

    rc, status = _status(capsys)
    assert rc == 0
    assert status["leased"] is True
    assert status["lock"]["owner"] == "qstock-mcp"

    released = server.baostock_lock("release")
    assert released["status"] == "ok"
    assert released["released"] is True
    assert not (tmp_path / "baostock.lock").exists()
    assert not (tmp_path / "baostock.token").exists()


def test_cli_held_lease_rejects_mcp_acquire_and_normal_release(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    holder = BaostockLease(
        tmp_path, identity=LeaseIdentity("cli", 999, "cli-agent")
    )
    holder.acquire(owner="cli-agent")

    locked = server.baostock_lock("acquire", owner="qstock-mcp")
    assert locked["status"] == "error"
    assert locked["error_kind"] == "baostock_locked"
    assert locked["lock"]["owner"] == "cli-agent"

    normal_release = server.baostock_lock("release")
    assert normal_release["status"] == "error"
    assert normal_release["error_kind"] == "baostock_locked"
    assert holder.require()["leased"] is True

    assert server.baostock_lock("release", force=True)["released"] is True
    rc, status = _status(capsys)
    assert rc == 0
    assert status["leased"] is False


def test_mcp_invalid_action_returns_structured_error(tmp_path, monkeypatch):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    out = server.baostock_lock("bogus")
    assert out["status"] == "error"
    assert out["error_kind"] == "invalid_argument"
    assert "acquire/status/release" in out["error"]


def test_mcp_release_closes_current_api_session(tmp_path, monkeypatch):
    class Result:
        error_code = "0"
        error_msg = ""
        fields = []
        cur_page_num = 1

        def next(self):
            return False

    class Client:
        def __init__(self):
            self.login_calls = 0
            self.logout_calls = 0

        def login(self):
            self.login_calls += 1
            return SimpleNamespace(error_code="0", error_msg="")

        def logout(self):
            self.logout_calls += 1
            assert (tmp_path / "baostock.lock").exists(), "logout 完成前不得释放实例租约"
            return SimpleNamespace(error_code="0", error_msg="")

        def query_stock_basic(self):
            return Result()

    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    client = Client()
    BaostockApi.reset_instance()
    api = BaostockApi.get(
        client_factory=lambda: client,
        budget=BaostockBudget(tmp_path / "baostock-budget.json"),
        sleep=lambda _seconds: None,
    )
    try:
        server.baostock_lock("acquire")
        api.fetch_stock_list()

        released = server.baostock_lock("release")

        assert released["status"] == "ok"
        assert client.logout_calls == 1
        server.baostock_lock("acquire")
        api.fetch_stock_list()
        assert client.login_calls == 2
    finally:
        server.baostock_lock("release", force=True)
        BaostockApi.reset_instance()


def test_mcp_baostock_tool_without_instance_lease_stops_before_core(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(
        server.tools_daily,
        "fetch_daily",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("无实例租约不得进入核心工具")
        ),
    )

    out = server.fetch_daily("600519", days=5)

    assert out["status"] == "error"
    assert out["error_kind"] == "baostock_locked"


def test_mcp_holder_can_use_data_tool_and_then_release(tmp_path, monkeypatch):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    expected = {"status": "ok", "rows_upserted": 0}
    monkeypatch.setattr(
        server.tools_daily, "fetch_daily", lambda *args, **kwargs: expected
    )

    assert server.baostock_lock("acquire")["status"] == "ok"
    assert server.fetch_daily("600519", days=5) == expected
    assert server.baostock_lock("release")["released"] is True


def test_mcp_new_price_query_is_pure_pg_and_exempt_from_lease(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("QSTOCK_PRICE_SOURCE", "new")
    expected = {"status": "ok", "rows": []}
    monkeypatch.setattr(
        server.tools_daily, "query_daily", lambda *args, **kwargs: expected
    )

    assert server.query_daily("600519", days=5) == expected


def test_baostock_data_tools_declare_instance_lease_and_user_confirmation():
    tools = asyncio.run(server.mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}

    for name in (
        "init_database",
        "fetch_daily",
        "query_daily",
        "get_fundamentals",
    ):
        description = by_name[name].description or ""
        assert "实例租约" in description
        assert "baostock_locked" in description
        assert "用户确认" in description
        assert "显式 release" in description

    instructions = server.mcp.instructions or ""
    assert "无当前 MCP 实例租约" in instructions
    assert "先询问用户" in instructions
    assert "baostock_locked" in instructions


def test_tool_list_and_instructions_declare_stale_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "baostock_lock" in names
    lock_tool = next(tool for tool in tools if tool.name == "baostock_lock")
    assert "显式租约" in lock_tool.description
    assert "不得自动 acquire" in lock_tool.description
    assert "stale" in lock_tool.description
    assert "release" in lock_tool.description

    instructions = server.mcp.instructions or ""
    assert "baostock_lock" in instructions
    assert "确认" in instructions
    assert "baostock_locked" in instructions
