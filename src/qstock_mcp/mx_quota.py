"""妙想（MX）配额 ledger：skill × 日期的本地计数，JSON 文件持久化（issue #23/T1）。

路径：环境变量 MX_QUOTA_FILE 覆盖，默认 ~/.qstock-mcp/quota.json。
每日上限：MX_DAILY_LIMIT 为通用默认，MX_DAILY_LIMIT_<SKILL>（如
MX_DAILY_LIMIT_MX_DATA）单 skill 覆盖；缺省 20（保守）。跨日自动重置
（计数按日期键存储，日期变化即新计数）。构造参数 path/limits/today
为测试接缝。文件缺失/损坏按零计数处理，不抛异常。
"""

import json
import os
from datetime import date
from pathlib import Path

DEFAULT_DAILY_LIMIT = 20

_DEFAULT_PATH = Path.home() / ".qstock-mcp" / "quota.json"


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class MxQuota:
    """按妙想 skill 分别记账的每日配额。

    limits 注入 per-skill 上限（{"mx-data": 3}）；default_limit 注入通用默认
    （None 时读 MX_DAILY_LIMIT env，再缺省 20）。today 注入日期（YYYY-MM-DD）。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        limits: dict[str, int] | None = None,
        default_limit: int | None = None,
        today: str | None = None,
    ) -> None:
        if path is None:
            path = os.environ.get("MX_QUOTA_FILE") or _DEFAULT_PATH
        self._path = Path(path)
        self._limits = dict(limits) if limits else {}
        self._default_limit = (
            default_limit
            if default_limit is not None
            else _env_int("MX_DAILY_LIMIT")
        )
        self._today = today if today is not None else date.today().isoformat()
        self._counts: dict[str, dict[str, int]] | None = None  # 懒加载

    def _load(self) -> dict[str, dict[str, int]]:
        if self._counts is None:
            try:
                data = json.loads(self._path.read_text())
                self._counts = data if isinstance(data, dict) else {}
            except Exception:  # noqa: BLE001 - 缺失/损坏文件按零计数
                self._counts = {}
        return self._counts

    def _save(self) -> None:
        # 只保留当日计数：旧日期天然失效，避免文件无界增长
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({self._today: self._load().get(self._today, {})})
        )

    def limit_for(self, skill: str) -> int:
        if skill in self._limits:
            return self._limits[skill]
        env_key = f"MX_DAILY_LIMIT_{skill.upper().replace('-', '_')}"
        env_val = _env_int(env_key)
        if env_val is not None:
            return env_val
        return (
            self._default_limit
            if self._default_limit is not None
            else DEFAULT_DAILY_LIMIT
        )

    def used(self, skill: str) -> int:
        """当日已用次数（跨日自动重置：只读当日键）。"""
        return self._load().get(self._today, {}).get(skill, 0)

    def is_exhausted(self, skill: str) -> bool:
        return self.used(skill) >= self.limit_for(skill)

    def record(self, skill: str) -> None:
        """记录一次用量并持久化。"""
        day = self._load().setdefault(self._today, {})
        day[skill] = day.get(skill, 0) + 1
        self._save()

    def snapshot(self, skill: str) -> dict:
        """自描述用量回显：{skill, used, limit}。"""
        return {"skill": skill, "used": self.used(skill), "limit": self.limit_for(skill)}
