"""CLI 实例租约契约（#173，ADR 0019）。

CLI 数据命令自动 acquire/finally release；公开锁命令仅 status 与 stale force。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import qstock_mcp.adapters.baostock_adapter as adapter_module
import qstock_mcp.baostock_api as baostock_api
from qstock_mcp import scheduler
from qstock_mcp.adapters.baostock_adapter import BaostockAdapter
from qstock_mcp.baostock_lease import BaostockBudget, BaostockLease, LeaseIdentity


def _run(capsys, argv, **kwargs):
    rc = scheduler.main(argv, **kwargs)
    return rc, json.loads(capsys.readouterr().out)


def _status(capsys):
    return _run(capsys, ["baostock-lock", "status"])


class FundamentalsApi:
    def __init__(self):
        self.calls = 0

    def fetch_fundamentals(self, code):
        self.calls += 1
        return {"profit": [{"code": code}], "growth": []}


def _install_fundamentals_api(monkeypatch, tmp_path, api):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(
        adapter_module, "BaostockApi", SimpleNamespace(get=lambda: api)
    )


def _run_fundamentals(capsys, codes="600519"):
    return _run(
        capsys,
        ["fundamentals", "--codes", codes],
        fundamentals_adapters=[BaostockAdapter()],
    )


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
    assert not (tmp_path / "baostock-operation.lock").exists()


def test_cli_status_and_stale_force_sees_instance_metadata(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    holder = BaostockLease(
        tmp_path, identity=LeaseIdentity("cli", 999, "cli-holder")
    )
    holder.acquire(
        owner="qstock-agent",
        reason="adj-backfill 20230101-20261104",
        command="qstock-scheduler adj-backfill",
    )

    rc, status = _status(capsys)
    assert rc == 0
    assert status["leased"] is True
    assert status["lock"]["owner"] == "qstock-agent"
    assert status["lock"]["entry"] == "cli"
    assert status["lock"]["pid"] == 999
    assert status["lock"]["instance_id"] == "cli-holder"
    assert status["lock"]["reason"] == "adj-backfill 20230101-20261104"

    rc, released = _run(capsys, ["baostock-lock", "release", "--force"])
    assert rc == 0
    assert released["released"] is True
    assert not (tmp_path / "baostock.lock").exists()
    assert not (tmp_path / "baostock.token").exists()


def test_release_broken_token_requires_stale_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    holder = BaostockLease(
        tmp_path, identity=LeaseIdentity("cli", 999, "broken")
    )
    holder.acquire(owner="first")
    (tmp_path / "baostock.token").write_text("broken")

    rc, out = _run(capsys, ["baostock-lock", "release"])
    assert rc == 1
    assert out["error_kind"] == "invalid_argument"
    assert (tmp_path / "baostock.lock").exists()

    rc, out = _run(capsys, ["baostock-lock", "release", "--force"])
    assert rc == 0
    assert out["released"] is True
    assert not (tmp_path / "baostock.lock").exists()


def test_auto_acquire_rejected_when_budget_exhausted(tmp_path, monkeypatch, capsys):
    api = FundamentalsApi()
    _install_fundamentals_api(monkeypatch, tmp_path, api)
    BaostockBudget(Path(tmp_path) / "baostock-budget.json").record("query", 50_000)

    rc, out = _run_fundamentals(capsys)

    assert rc == 1
    assert out["error_kind"] == "baostock_budget_exhausted"
    assert out["budget"]["total"] == 50_000
    assert api.calls == 0
    assert not (tmp_path / "baostock.lock").exists()


def test_removed_acquire_options_are_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        scheduler.main(["baostock-lock", "status", "--owner", "ignored"])
    assert exc.value.code == 2
    capsys.readouterr()

    rc, out = _status(capsys)
    assert rc == 0
    rc, out = _run(capsys, ["baostock-lock", "status", "--force"])
    assert rc == 1
    assert out["error_kind"] == "invalid_argument"
    assert "release" in out["error"]


def test_cli_data_command_auto_acquires_and_releases_instance_lease(
    tmp_path, monkeypatch, capsys
):
    api = FundamentalsApi()
    _install_fundamentals_api(monkeypatch, tmp_path, api)

    rc, out = _run_fundamentals(capsys)

    assert rc == 0, out
    assert api.calls == 1
    assert out["status"] == "ok"
    assert not (tmp_path / "baostock.lock").exists()
    assert not (tmp_path / "baostock.token").exists()


def test_cli_command_acquires_once_for_multiple_baostock_calls(
    tmp_path, monkeypatch, capsys
):
    api = FundamentalsApi()
    _install_fundamentals_api(monkeypatch, tmp_path, api)
    acquire_calls = 0
    original = BaostockLease.acquire

    def counted_acquire(self, **kwargs):
        nonlocal acquire_calls
        acquire_calls += 1
        return original(self, **kwargs)

    monkeypatch.setattr(BaostockLease, "acquire", counted_acquire)

    rc, out = _run_fundamentals(capsys, "600519,000001")

    assert rc == 0, out
    assert api.calls == 2
    assert acquire_calls == 1
    assert not (tmp_path / "baostock.lock").exists()


def test_cli_lock_conflict_stops_before_api_without_fallback(
    tmp_path, monkeypatch, capsys
):
    api = FundamentalsApi()
    _install_fundamentals_api(monkeypatch, tmp_path, api)
    holder = BaostockLease(
        tmp_path, identity=LeaseIdentity("mcp", 999, "mcp-holder")
    )
    holder.acquire(owner="mcp-owner")

    rc, out = _run_fundamentals(capsys)

    assert rc == 1
    assert out["status"] == "error"
    assert out["error_kind"] == "baostock_locked"
    assert out["lock"]["owner"] == "mcp-owner"
    assert api.calls == 0
    assert holder.require()["leased"] is True
    holder.release()


def test_data_command_preserves_existing_baostock_locked_error(
    pg_test, tmp_path, monkeypatch, capsys
):
    from qstock_mcp.baostock_errors import BaostockLocked

    class LockedAdapter:
        name = "baostock"

        def fetch_stock_list(self):
            raise BaostockLocked("未持有有效的 baostock 租约")

    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    rc, out = _run(capsys, ["stock-list"], list_adapters=[LockedAdapter()])

    assert rc == 1
    assert out["status"] == "error"
    assert out["error_kind"] == "baostock_locked"
    assert out["job"] == "stock-list"


def test_public_cli_lock_only_exposes_status_and_stale_force(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        scheduler.main(["baostock-lock", "acquire"])
    assert exc.value.code == 2
    capsys.readouterr()

    rc, out = _run(capsys, ["baostock-lock", "release"])
    assert rc == 1
    assert out["error_kind"] == "invalid_argument"
    assert "stale" in out["error"]
    assert _status(capsys)[0] == 0


def test_cli_cleanup_failure_is_structured_and_still_releases(
    tmp_path, monkeypatch, capsys
):
    api = FundamentalsApi()
    _install_fundamentals_api(monkeypatch, tmp_path, api)
    monkeypatch.setattr(
        scheduler.BaostockApi,
        "reset_instance",
        lambda: (_ for _ in ()).throw(RuntimeError("logout failed")),
    )

    rc, out = _run_fundamentals(capsys)

    assert rc == 1
    assert out["status"] == "error"
    assert out["error_kind"] == "baostock_cleanup_error"
    assert out["succeeded"] == 1
    assert "logout failed" in out["error"]
    assert not (tmp_path / "baostock.lock").exists()


def test_lease_commands_do_not_touch_baostock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))

    def explode():
        raise AssertionError("租约命令不得创建 baostock 会话")

    monkeypatch.setattr(baostock_api, "_load_baostock", explode)
    holder = BaostockLease(
        tmp_path, identity=LeaseIdentity("mcp", 999, "control")
    )
    holder.acquire()

    assert _status(capsys)[0] == 0
    assert _run(capsys, ["baostock-lock", "release", "--force"])[0] == 0
