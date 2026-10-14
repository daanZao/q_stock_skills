"""配额 ledger 接缝测试（issue #23/T1）：skill × 日期计数，纯本地 JSON 文件。

契约：按 skill 独立计数；跨日自动重置；每日上限默认 20，MX_DAILY_LIMIT 通用
覆盖、MX_DAILY_LIMIT_<SKILL> 单 skill 覆盖；构造参数 path/limits/today 为
测试接缝；进程重启不丢（文件持久化）。
"""

import json

from qstock_mcp.mx_quota import MxQuota


def test_counts_are_independent_per_skill(tmp_path):
    q = MxQuota(tmp_path / "quota.json", today="2026-09-01")
    q.record("mx-data")
    q.record("mx-data")
    q.record("mx-search")
    assert q.used("mx-data") == 2
    assert q.used("mx-search") == 1


def test_cross_day_counts_reset(tmp_path):
    path = tmp_path / "quota.json"
    MxQuota(path, today="2026-09-01").record("mx-data")
    # 日期变化即新计数（同一文件）
    q_next_day = MxQuota(path, today="2026-09-02")
    assert q_next_day.used("mx-data") == 0
    assert not q_next_day.is_exhausted("mx-data")


def test_persistence_roundtrip_across_instances(tmp_path):
    path = tmp_path / "quota.json"
    MxQuota(path, today="2026-09-01").record("mx-data")
    MxQuota(path, today="2026-09-01").record("mx-data")
    q_reloaded = MxQuota(path, today="2026-09-01")
    assert q_reloaded.used("mx-data") == 2
    # 文件确实是 JSON，结构为 日期 → skill → 计数
    raw = json.loads(path.read_text())
    assert raw["2026-09-01"]["mx-data"] == 2


def test_default_limit_is_conservative_20(tmp_path, monkeypatch):
    monkeypatch.delenv("MX_DAILY_LIMIT", raising=False)
    monkeypatch.delenv("MX_DAILY_LIMIT_MX_DATA", raising=False)
    q = MxQuota(tmp_path / "quota.json", today="2026-09-01")
    assert q.limit_for("mx-data") == 20
    assert q.limit_for("mx-search") == 20


def test_limit_injection_per_skill(tmp_path):
    q = MxQuota(tmp_path / "quota.json", limits={"mx-data": 3}, today="2026-09-01")
    assert q.limit_for("mx-data") == 3
    assert q.limit_for("mx-search") == 20  # 未注入的走默认


def test_limit_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MX_DAILY_LIMIT", "7")
    monkeypatch.setenv("MX_DAILY_LIMIT_MX_DATA", "5")
    q = MxQuota(tmp_path / "quota.json", today="2026-09-01")
    assert q.limit_for("mx-data") == 5  # per-skill env 优先于通用 env
    assert q.limit_for("mx-search") == 7


def test_exhaustion_at_limit(tmp_path):
    q = MxQuota(tmp_path / "quota.json", limits={"mx-data": 2}, today="2026-09-01")
    assert not q.is_exhausted("mx-data")
    q.record("mx-data")
    assert not q.is_exhausted("mx-data")
    q.record("mx-data")
    assert q.is_exhausted("mx-data")
    assert q.snapshot("mx-data") == {"skill": "mx-data", "used": 2, "limit": 2}


def test_missing_or_corrupt_file_starts_fresh(tmp_path):
    q = MxQuota(tmp_path / "nonexistent.json", today="2026-09-01")
    assert q.used("mx-data") == 0
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    q2 = MxQuota(bad, today="2026-09-01")
    assert q2.used("mx-data") == 0  # 损坏文件不炸，按零计数
