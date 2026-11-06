"""baostock 显式租约与日预算（issue #170，ADR 0019）。

主接缝：`BaostockLease.acquire/status/release/require` 与 `BaostockBudget`，
全部通过公开方法观察文件语义；时间注入固定时区时钟，不触网、不读真实 home。
"""

import json
import stat
from datetime import datetime

import pytest

from qstock_mcp.baostock_errors import BaostockBudgetExhausted, BaostockLocked
from qstock_mcp.baostock_lease import (
    CHINA_TZ,
    STOP_AT,
    WARN_AT,
    BaostockBudget,
    BaostockLease,
)


def _now(year=2026, month=11, day=5, hour=9, minute=0):
    moment = datetime(year, month, day, hour, minute, tzinfo=CHINA_TZ)
    return lambda: moment


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class TestLeaseFiles:
    def test_acquire_creates_private_lock_and_token(self, tmp_path):
        lease = BaostockLease(tmp_path, now=_now())
        state = lease.acquire(
            owner="qstock-agent",
            reason="adj-backfill 20230101-20261104",
            command="qstock-scheduler adj-backfill",
        )

        assert state["leased"] is True
        assert state["lock"]["owner"] == "qstock-agent"
        assert state["lock"]["command"] == "qstock-scheduler adj-backfill"
        assert state["lock"]["reason"] == "adj-backfill 20230101-20261104"
        assert state["lock"]["acquired_at"] == "2026-11-05T09:00:00+08:00"
        assert state["lock"]["pid"] > 0
        assert state["token_present"] is True
        assert state["token_valid"] is True
        assert _mode(tmp_path / "baostock.lock") == 0o600
        assert _mode(tmp_path / "baostock.token") == 0o600
        # 锁文件只存 token 哈希，不存明文
        raw_lock = (tmp_path / "baostock.lock").read_text()
        token = (tmp_path / "baostock.token").read_text()
        assert token not in raw_lock
        assert json.loads(raw_lock)["token_hash"]

    def test_second_acquire_rejected_with_holder_info(self, tmp_path):
        lease = BaostockLease(tmp_path, now=_now())
        lease.acquire(owner="qstock-agent", command="qstock-scheduler daily")

        with pytest.raises(BaostockLocked) as exc:
            lease.acquire(owner="other", command="qstock-scheduler index")

        payload = exc.value.to_dict()
        assert payload["error_kind"] == "baostock_locked"
        assert payload["lock"]["owner"] == "qstock-agent"
        assert payload["lock"]["command"] == "qstock-scheduler daily"
        assert "release" in payload["note"]
        # 原租约不被覆盖
        assert lease.status()["lock"]["owner"] == "qstock-agent"

    def test_release_removes_files_and_allows_reacquire(self, tmp_path):
        lease = BaostockLease(tmp_path, now=_now())
        lease.acquire(owner="qstock-agent")

        released = lease.release()

        assert released["released"] is True
        assert not (tmp_path / "baostock.lock").exists()
        assert not (tmp_path / "baostock.token").exists()
        assert lease.status()["leased"] is False
        lease.acquire(owner="second")
        assert lease.status()["lock"]["owner"] == "second"

    def test_token_mismatch_is_unleased_and_needs_force(self, tmp_path):
        lease = BaostockLease(tmp_path, now=_now())
        lease.acquire(owner="qstock-agent")
        (tmp_path / "baostock.token").write_text("not-the-token")

        state = lease.status()
        assert state["leased"] is False
        assert state["token_present"] is True
        assert state["token_valid"] is False
        assert state["lock"]["owner"] == "qstock-agent"  # 持有者信息仍可见
        with pytest.raises(BaostockLocked):
            lease.require()
        with pytest.raises(BaostockLocked):
            lease.release()
        assert (tmp_path / "baostock.lock").exists()

        assert lease.release(force=True)["released"] is True
        assert lease.status()["leased"] is False

    def test_missing_token_is_unleased(self, tmp_path):
        lease = BaostockLease(tmp_path, now=_now())
        lease.acquire(owner="qstock-agent")
        (tmp_path / "baostock.token").unlink()

        state = lease.status()
        assert state["leased"] is False
        assert state["token_present"] is False
        with pytest.raises(BaostockLocked):
            lease.require()
        with pytest.raises(BaostockLocked):
            lease.release(force=False)
        lease.release(force=True)
        assert lease.status()["lock_present"] is False

    def test_status_has_no_ttl_and_reports_age_across_processes(self, tmp_path):
        lease = BaostockLease(tmp_path, now=_now(hour=9))
        lease.acquire(owner="qstock-agent", command="qstock-scheduler adj-backfill")

        # 另一个“进程”/入口只读同一组文件，10 小时后仍然有效（无 TTL）
        later = BaostockLease(tmp_path, now=_now(hour=19))
        state = later.status()
        assert state["leased"] is True
        assert state["lock"]["age_seconds"] == 36000
        assert later.require()["leased"] is True

    def test_orphan_token_without_lock_is_cleaned_by_release(self, tmp_path):
        (tmp_path / "baostock.token").write_text("left-over")
        lease = BaostockLease(tmp_path, now=_now())

        state = lease.status()
        assert state["leased"] is False
        assert state["lock_present"] is False
        assert state["token_present"] is True

        released = lease.release()
        assert released["released"] is False
        assert released["token_removed"] is True
        assert not (tmp_path / "baostock.token").exists()

    def test_status_does_not_create_lock_directory(self, tmp_path):
        root = tmp_path / "missing"
        lease = BaostockLease(root, now=_now())
        state = lease.status()
        assert state["leased"] is False
        assert not root.exists()


class TestBudget:
    def test_records_protocol_actions_by_china_day(self, tmp_path):
        moment = [datetime(2026, 11, 5, 23, 59, tzinfo=CHINA_TZ)]
        budget = BaostockBudget(tmp_path / "baostock-budget.json", now=lambda: moment[0])

        budget.record("login")
        budget.record("query")
        budget.record("page", 2)
        snapshot = budget.snapshot()
        assert snapshot["date"] == "2026-11-05"
        assert snapshot["login"] == 1
        assert snapshot["logout"] == 0
        assert snapshot["query"] == 1
        assert snapshot["page"] == 2
        assert snapshot["total"] == 4
        assert _mode(tmp_path / "baostock-budget.json") == 0o600

        # 跨自然日（Asia/Shanghai）自动归零
        moment[0] = datetime(2026, 11, 6, 0, 1, tzinfo=CHINA_TZ)
        assert budget.snapshot()["total"] == 0
        budget.record("query")
        assert budget.snapshot()["date"] == "2026-11-06"
        assert budget.snapshot()["total"] == 1

    def test_corrupt_budget_file_is_treated_as_zero(self, tmp_path):
        path = tmp_path / "baostock-budget.json"
        path.write_text("{not-json")
        budget = BaostockBudget(path, now=_now())
        assert budget.snapshot()["total"] == 0

    def test_acquire_warns_at_45k_and_rejects_at_50k(self, tmp_path):
        budget = BaostockBudget(tmp_path / "baostock-budget.json", now=_now())
        budget.record("query", WARN_AT)
        lease = BaostockLease(tmp_path, now=_now())

        state = lease.acquire(owner="qstock-agent")
        assert state["budget"]["warning"] is True
        assert "45,000" in state["warning"]
        lease.release()

        budget.record("page", STOP_AT - WARN_AT)
        assert budget.snapshot()["total"] == STOP_AT
        with pytest.raises(BaostockBudgetExhausted) as exc:
            lease.acquire(owner="qstock-agent")
        payload = exc.value.to_dict()
        assert payload["error_kind"] == "baostock_budget_exhausted"
        assert payload["budget"]["total"] == STOP_AT
        assert lease.status()["lock_present"] is False

    def test_budget_is_shared_by_lock_dir(self, tmp_path):
        first = BaostockLease(tmp_path, now=_now())
        first.budget.record("login")
        second = BaostockLease(tmp_path, now=_now())
        assert second.status()["budget"]["total"] == 1


def test_lease_defaults_to_env_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    lease = BaostockLease(now=_now())
    lease.acquire(owner="qstock-agent")
    assert (tmp_path / "baostock.lock").exists()
    assert (tmp_path / "baostock.token").exists()
