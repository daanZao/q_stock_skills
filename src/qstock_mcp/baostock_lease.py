"""baostock 显式租约与日请求预算（issue #170，ADR 0019）。

本地文件租约，不是 OS flock：进程退出**不自动释放**，崩溃遗留由
`release --force` 清理；没有 TTL。默认目录 `~/.qstock-mcp/`，可用
`QSTOCK_BAOSTOCK_LOCK_DIR` 覆盖（测试注入临时目录）。

文件：
- `baostock.lock`（0600）：owner/pid/command/acquired_at/reason/token_hash
- `baostock.token`（0600）：token 原文，数据命令读取后与 lock 内哈希比对
- `baostock-budget.json`（0600）：Asia/Shanghai 自然日的协议动作计数

租约是协作锁，不是安全边界：同机同用户可读取 token 并释放。
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .baostock_errors import (
    BaostockBudgetExhausted,
    BaostockLocked,
)

LOCK_FILENAME = "baostock.lock"
TOKEN_FILENAME = "baostock.token"
BUDGET_FILENAME = "baostock-budget.json"

# 保守口径：官方只说“每日 API 请求 ≤ 5 万”，未枚举 login/logout/翻页；
# 本文件按协议动作（login/logout/query/page）全部计入（ADR 0019）。
WARN_AT = 45_000
STOP_AT = 50_000
ACTIONS = ("login", "logout", "query", "page")

logger = logging.getLogger(__name__)

# Asia/Shanghai 当前无夏令时，固定 +08:00 与 IANA 时区在业务日期上等价。
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def default_lock_dir() -> Path:
    override = os.environ.get("QSTOCK_BAOSTOCK_LOCK_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".qstock-mcp"


def _now_china() -> datetime:
    return datetime.now(CHINA_TZ)


def _as_china(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=CHINA_TZ)
    return moment.astimezone(CHINA_TZ)


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _atomic_write_private(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace 原子写，权限 0600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


class BaostockBudget:
    """按协议动作计数的本地日预算；读取/写入 `baostock-budget.json`。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_lock_dir() / BUDGET_FILENAME
        self._now = now or _now_china

    def _today(self) -> str:
        return _as_china(self._now()).date().isoformat()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def snapshot(self) -> dict:
        """只读快照；日期不是今天时按零计数，不写文件。"""
        today = self._today()
        data = self._read()
        same_day = data.get("date") == today
        counts = {
            action: _non_negative_int(data.get(action)) if same_day else 0
            for action in ACTIONS
        }
        total = sum(counts.values())
        return {
            "date": today,
            **counts,
            "total": total,
            "warn_at": WARN_AT,
            "stop_at": STOP_AT,
            "warning": total >= WARN_AT,
            "exhausted": total >= STOP_AT,
        }

    def record(self, action: str, count: int = 1) -> dict:
        """记录协议动作并原子落盘；跨自然日自动从零开始。"""
        if action not in ACTIONS:
            raise ValueError(f"未知 baostock 协议动作: {action!r}")
        if count < 0:
            raise ValueError("count 不能为负")
        today = self._today()
        data = self._read()
        same_day = data.get("date") == today
        counts = {
            key: _non_negative_int(data.get(key)) if same_day else 0
            for key in ACTIONS
        }
        counts[action] += int(count)
        payload = {
            "date": today,
            **counts,
            "total": sum(counts.values()),
            "updated_at": _as_china(self._now()).isoformat(timespec="seconds"),
        }
        _atomic_write_private(
            self.path, json.dumps(payload, ensure_ascii=False) + "\n"
        )
        return self.snapshot()


class BaostockLease:
    """显式文件租约：acquire / status / release / require。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).expanduser() if root is not None else default_lock_dir()
        self.lock_path = self.root / LOCK_FILENAME
        self.token_path = self.root / TOKEN_FILENAME
        self._now = now or _now_china
        self.budget = BaostockBudget(self.root / BUDGET_FILENAME, now=self._now)

    # ------------------------------------------------------------------ status
    def _read_lock(self) -> tuple[dict | None, str | None]:
        if not self.lock_path.exists():
            return None, None
        try:
            raw = self.lock_path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, f"无法读取租约文件: {exc}"
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            return None, f"租约文件损坏: {exc}"
        if not isinstance(payload, dict):
            return None, "租约文件格式非法（不是 JSON 对象）"
        return payload, None

    def _age_seconds(self, acquired_at: object) -> int | None:
        try:
            moment = datetime.fromisoformat(str(acquired_at))
        except (TypeError, ValueError):
            return None
        return max(0, int((_as_china(self._now()) - _as_china(moment)).total_seconds()))

    def status(self) -> dict:
        """只读状态：不创建目录、不申请/释放、不触 baostock。"""
        lock_payload, lock_error = self._read_lock()
        lock_present = self.lock_path.exists()
        token_present = self.token_path.exists()
        token: str | None = None
        if token_present:
            try:
                token = self.token_path.read_text(encoding="utf-8").strip()
            except OSError:
                token_present = False
                token = None
        expected_hash = None
        if lock_payload is not None:
            expected_hash = lock_payload.get("token_hash")
        token_valid = bool(
            token
            and expected_hash
            and hmac.compare_digest(_hash_token(token), str(expected_hash))
        )
        leased = bool(lock_present and lock_payload is not None and token_valid)

        lock_info = None
        if lock_payload is not None:
            lock_info = {
                "owner": lock_payload.get("owner"),
                "pid": lock_payload.get("pid"),
                "command": lock_payload.get("command"),
                "reason": lock_payload.get("reason"),
                "acquired_at": lock_payload.get("acquired_at"),
                "age_seconds": self._age_seconds(lock_payload.get("acquired_at")),
            }

        note = None
        if leased:
            note = None
        elif not lock_present and not token_present:
            note = "无 baostock 租约"
        elif not lock_present and token_present:
            note = "token 存在但锁文件缺失，视为未租约；可直接 acquire"
        elif lock_error is not None:
            note = f"{lock_error}；请 release --force 后重新 acquire"
        elif not token_present:
            note = "租约文件存在但 token 缺失，视为未租约；请 release --force 后重新 acquire"
        elif not token_valid:
            note = "token 与锁文件哈希不匹配，视为未租约；请 release --force 后重新 acquire"

        return {
            "leased": leased,
            "lock_present": lock_present,
            "token_present": token_present,
            "token_valid": token_valid,
            "lock": lock_info,
            "lock_error": lock_error,
            "budget": self.budget.snapshot(),
            "note": note,
        }

    def require(self) -> dict:
        """数据命令/工具的租约检查：无有效租约抛 `BaostockLocked`。"""
        state = self.status()
        if state["leased"]:
            return state
        raise BaostockLocked(
            "未持有有效的 baostock 租约",
            lock=state["lock"],
            token_present=state["token_present"],
            token_valid=state["token_valid"],
            note=(
                "先调用 baostock_lock(acquire)（CLI: qstock-scheduler "
                "baostock-lock acquire）；锁住时请从另一个 CLI/MCP 会话 release"
            ),
        )

    # --------------------------------------------------------------- lifecycle
    def acquire(
        self,
        *,
        owner: str | None = None,
        reason: str | None = None,
        command: str | None = None,
    ) -> dict:
        """显式申请租约；已有锁文件（含损坏/token 不匹配）时拒绝覆盖。"""
        state = self.status()
        if state["lock_present"]:
            note = (
                "先调用 baostock_lock(acquire)（CLI: qstock-scheduler "
                "baostock-lock acquire）；锁住时请从另一个 CLI/MCP 会话 release"
            )
            if not state["leased"]:
                note = (
                    "锁文件存在但租约不可用；请先 release --force 清理，"
                    "再重新 acquire"
                )
            raise BaostockLocked(
                "baostock 租约已被占用"
                if state["leased"]
                else "baostock 锁文件存在但租约不可用",
                lock=state["lock"],
                token_present=state["token_present"],
                token_valid=state["token_valid"],
                note=note,
            )

        budget_state = self.budget.snapshot()
        if budget_state["exhausted"]:
            raise BaostockBudgetExhausted(
                budget_state,
                note="当日协议动作已达 50,000 硬上限；等 Asia/Shanghai 次日重置",
            )

        self.root.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(32)
        payload = {
            "version": 1,
            "owner": owner or _default_owner(),
            "pid": os.getpid(),
            "command": command or _default_command(),
            "reason": reason or "",
            "acquired_at": _as_china(self._now()).isoformat(timespec="seconds"),
            "token_hash": _hash_token(token),
        }
        try:
            fd = os.open(
                self.lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            current = self.status()
            raise BaostockLocked(
                "baostock 租约已被占用",
                lock=current["lock"],
                token_present=current["token_present"],
                token_valid=current["token_valid"],
                note="请从另一个 CLI/MCP 会话 release 后再 acquire",
            ) from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.lock_path, 0o600)
            _atomic_write_private(self.token_path, token + "\n")
        except Exception:
            self.lock_path.unlink(missing_ok=True)
            self.token_path.unlink(missing_ok=True)
            raise

        acquired = self.status()
        acquired["acquired"] = True
        if budget_state["warning"]:
            message = (
                f"baostock 当日协议动作已达 {budget_state['total']:,}，"
                "接近 50,000 上限；优先保留 morning/close，暂停大回填"
            )
            acquired["warning"] = message
            logger.warning(message)
        return acquired

    def release(self, *, force: bool = False) -> dict:
        """显式释放；`force=True` 清理 stale/损坏/换 owner 的租约。"""
        state = self.status()
        if not state["lock_present"]:
            token_removed = False
            if state["token_present"]:
                self.token_path.unlink(missing_ok=True)
                token_removed = True
            return {
                "released": False,
                "forced": force,
                "token_removed": token_removed,
                "lock": None,
                "budget": state["budget"],
                "note": "无 baostock 租约",
            }

        if not state["leased"] and not force:
            raise BaostockLocked(
                "baostock 租约不可用（token 缺失或哈希不匹配），需 release --force",
                lock=state["lock"],
                token_present=state["token_present"],
                token_valid=state["token_valid"],
                note="同机同用户确认无其他会话使用后，执行 release --force",
            )

        prior_lock = state["lock"]
        # 先删 token 再删 lock：反向顺序会让并发 acquire 在两步之间建立新锁，
        # 随后被本次 release 误删新 token；先删 token 时旧 lock 仍在，acquire 会被拒。
        self.token_path.unlink(missing_ok=True)
        self.lock_path.unlink(missing_ok=True)
        return {
            "released": True,
            "forced": force,
            "token_removed": state["token_present"],
            "lock": prior_lock,
            "budget": self.budget.snapshot(),
            "note": "baostock 租约已释放；进程退出不会自动释放，需显式 release",
        }


def _default_owner() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - 极少数无用户环境
        return "qstock"


def _default_command() -> str:
    try:
        return " ".join(sys.argv)
    except Exception:  # noqa: BLE001
        return "qstock"
