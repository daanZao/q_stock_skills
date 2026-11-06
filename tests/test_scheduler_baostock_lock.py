"""`qstock-scheduler baostock-lock` 契约（issue #170，ADR 0019）。

CLI 与 MCP 共用同一组文件；无活动会话时租约操作不发 baostock 请求。
"""

import json
from pathlib import Path

import qstock_mcp.baostock_api as baostock_api
from qstock_mcp import scheduler
from qstock_mcp.baostock_lease import BaostockBudget


def _run(capsys, argv):
    rc = scheduler.main(argv)
    return rc, json.loads(capsys.readouterr().out)


def _status(capsys):
    return _run(capsys, ["baostock-lock", "status"])


def test_status_without_lease_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))

    rc, out = _status(capsys)

    assert rc == 0
    assert out["status"] == "ok"
    assert out["job"] == "baostock-lock"
    assert out["action"] == "status"
    assert out["leased"] is False
    assert out["budget"]["total"] == 0
    assert not (tmp_path / "baostock.lock").exists()


def test_acquire_status_release_cycle(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))

    rc, acquired = _run(capsys, [
        "baostock-lock", "acquire",
        "--owner", "qstock-agent",
        "--reason", "adj-backfill 20230101-20261104",
    ])
    assert rc == 0
    assert acquired["leased"] is True
    assert acquired["acquired"] is True
    assert acquired["lock"]["owner"] == "qstock-agent"
    assert acquired["lock"]["reason"] == "adj-backfill 20230101-20261104"
    assert acquired["lock"]["command"].endswith(
        "baostock-lock acquire --owner qstock-agent "
        "--reason adj-backfill 20230101-20261104"
    )

    rc, status = _status(capsys)
    assert rc == 0
    assert status["leased"] is True
    assert status["lock"]["owner"] == "qstock-agent"
    assert status["lock"]["age_seconds"] is not None

    rc, released = _run(capsys, ["baostock-lock", "release"])
    assert rc == 0
    assert released["released"] is True
    assert released["lock"]["owner"] == "qstock-agent"
    assert not (tmp_path / "baostock.lock").exists()
    assert not (tmp_path / "baostock.token").exists()


def test_second_acquire_returns_locked_holder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    _run(capsys, ["baostock-lock", "acquire", "--owner", "first"])

    rc, out = _run(capsys, ["baostock-lock", "acquire", "--owner", "second"])

    assert rc == 1
    assert out["status"] == "error"
    assert out["error_kind"] == "baostock_locked"
    assert out["lock"]["owner"] == "first"
    assert "release" in out["note"]
    assert _status(capsys)[1]["lock"]["owner"] == "first"


def test_release_broken_token_needs_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    _run(capsys, ["baostock-lock", "acquire", "--owner", "first"])
    (tmp_path / "baostock.token").write_text("broken")

    rc, out = _run(capsys, ["baostock-lock", "release"])
    assert rc == 1
    assert out["error_kind"] == "baostock_locked"
    assert out["token_valid"] is False
    assert (tmp_path / "baostock.lock").exists()

    rc, out = _run(capsys, ["baostock-lock", "release", "--force"])
    assert rc == 0
    assert out["released"] is True
    assert not (tmp_path / "baostock.lock").exists()


def test_acquire_rejected_when_budget_exhausted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    BaostockBudget(Path(tmp_path) / "baostock-budget.json").record("query", 50_000)

    rc, out = _run(capsys, ["baostock-lock", "acquire"])

    assert rc == 1
    assert out["error_kind"] == "baostock_budget_exhausted"
    assert out["budget"]["total"] == 50_000
    assert not (tmp_path / "baostock.lock").exists()


def test_status_rejects_acquire_only_options(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))

    rc, out = _run(capsys, ["baostock-lock", "status", "--owner", "ignored"])
    assert rc == 1
    assert out["error_kind"] == "invalid_argument"
    assert "acquire" in out["error"]

    rc, out = _run(capsys, ["baostock-lock", "status", "--force"])
    assert rc == 1
    assert out["error_kind"] == "invalid_argument"
    assert "release" in out["error"]


def test_release_closes_active_api_session(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from qstock_mcp.baostock_api import BaostockApi

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
        assert _run(capsys, ["baostock-lock", "acquire"])[0] == 0
        api.fetch_stock_list()

        rc, released = _run(capsys, ["baostock-lock", "release"])

        assert rc == 0
        assert released["released"] is True
        assert client.logout_calls == 1
        assert _run(capsys, ["baostock-lock", "acquire"])[0] == 0
        api.fetch_stock_list()
        assert client.login_calls == 2
    finally:
        BaostockApi.reset_instance()


def test_lease_commands_do_not_touch_baostock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))

    def explode():
        raise AssertionError("租约命令不得创建 baostock 会话")

    monkeypatch.setattr(baostock_api, "_load_baostock", explode)

    assert _run(capsys, ["baostock-lock", "acquire"])[0] == 0
    assert _status(capsys)[0] == 0
    assert _run(capsys, ["baostock-lock", "release"])[0] == 0
