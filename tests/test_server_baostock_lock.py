"""MCP `baostock_lock` 工具契约（issue #170，ADR 0019）。

MCP 与 CLI 共用同一组租约文件；无活动会话时工具不触 baostock。
"""

import asyncio
import json

from qstock_mcp import scheduler, server


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


def test_mcp_acquire_release_shares_files_with_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))

    acquired = server.baostock_lock(
        "acquire", owner="qstock-mcp", reason="daily-review"
    )
    assert acquired["status"] == "ok"
    assert acquired["leased"] is True
    assert acquired["lock"]["owner"] == "qstock-mcp"
    assert acquired["lock"]["command"] == "mcp:baostock_lock"

    # CLI 只读看到同一把锁
    rc, status = _status(capsys)
    assert rc == 0
    assert status["leased"] is True
    assert status["lock"]["owner"] == "qstock-mcp"

    # CLI 试图再申请被拒，持有者信息来自 MCP 写入的 lock
    rc = scheduler.main(["baostock-lock", "acquire", "--owner", "cli"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["error_kind"] == "baostock_locked"
    assert payload["lock"]["owner"] == "qstock-mcp"

    released = server.baostock_lock("release")
    assert released["status"] == "ok"
    assert released["released"] is True
    assert not (tmp_path / "baostock.lock").exists()
    assert not (tmp_path / "baostock.token").exists()


def test_cli_acquire_then_mcp_requires_release(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    rc = scheduler.main(["baostock-lock", "acquire", "--owner", "cli-agent"])
    assert rc == 0
    capsys.readouterr()

    locked = server.baostock_lock("acquire", owner="qstock-mcp")
    assert locked["status"] == "error"
    assert locked["error_kind"] == "baostock_locked"
    assert locked["lock"]["owner"] == "cli-agent"

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
    from types import SimpleNamespace

    from qstock_mcp.baostock_api import BaostockApi
    from qstock_mcp.baostock_lease import BaostockBudget

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
        BaostockApi.reset_instance()


def test_tool_list_and_instructions_declare_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "baostock_lock" in names
    lock_tool = next(tool for tool in tools if tool.name == "baostock_lock")
    assert "显式租约" in lock_tool.description
    assert "不会自动释放" in lock_tool.description
    assert "release" in lock_tool.description

    instructions = server.mcp.instructions or ""
    assert "baostock_lock" in instructions
    assert "release" in instructions
    assert "baostock_locked" in instructions
