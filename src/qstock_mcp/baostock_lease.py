"""baostock 进程实例租约与日请求预算（issue #173，ADR 0019）。

CLI 在命令上下文首次实际抓取时自动 acquire/finally release；MCP 经用户确认后
显式 acquire/release。数据与正常 release 均校验 entry/PID/instance_id；崩溃残留
由人工确认 stale 后 force，没有 TTL 或自动存活判定。

文件：
- `baostock.lock`（0600）：实例身份、owner/command/acquired_at/reason/token_hash
- `baostock.token`（0600）：token 原文；哈希正确仍不代表其他实例可共享
- `baostock-budget.json`（0600）：Asia/Shanghai 自然日的协议动作计数
- `baostock-operation.lock`：稳定 inode 的租约操作互斥，不作为逻辑租约

租约是协作锁，不是安全边界；release/acquire/force 按代际防止误删新锁。
"""

from __future__ import annotations

import fcntl
import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from .baostock_errors import (
    BaostockBudgetExhausted,
    BaostockLocked,
)

LOCK_FILENAME = "baostock.lock"
TOKEN_FILENAME = "baostock.token"
BUDGET_FILENAME = "baostock-budget.json"
OPERATION_FILENAME = "baostock-operation.lock"

# 保守口径：官方只说“每日 API 请求 ≤ 5 万”，未枚举 login/logout/翻页；
# 本文件按协议动作（login/logout/query/page）全部计入（ADR 0019）。
WARN_AT = 45_000
STOP_AT = 50_000
ACTIONS = ("login", "logout", "query", "page")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeaseIdentity:
    entry: str
    pid: int
    instance_id: str


@dataclass
class LeaseContext:
    identity: LeaseIdentity
    command: str
    auto_acquire_attempted: bool = False
    auto_acquired: bool = False

    @classmethod
    def create(cls, entry: str, command: str) -> "LeaseContext":
        return cls(
            LeaseIdentity(entry, os.getpid(), secrets.token_hex(16)),
            command,
        )


_DEFAULT_CONTEXT = LeaseContext.create("mcp", "mcp:tools")
_CURRENT_CONTEXT: ContextVar[LeaseContext] = ContextVar(
    "qstock_baostock_lease_context", default=_DEFAULT_CONTEXT
)


def current_lease_context() -> LeaseContext:
    return _CURRENT_CONTEXT.get()


@contextmanager
def use_lease_context(context: LeaseContext) -> Iterator[None]:
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)

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
    """进程实例租约：acquire / status / release / require。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        identity: LeaseIdentity | None = None,
    ) -> None:
        self.root = Path(root).expanduser() if root is not None else default_lock_dir()
        self.lock_path = self.root / LOCK_FILENAME
        self.token_path = self.root / TOKEN_FILENAME
        self._now = now or _now_china
        self.identity = identity or current_lease_context().identity
        self.budget = BaostockBudget(self.root / BUDGET_FILENAME, now=self._now)

    def _owns(self, lock: dict | None) -> bool:
        return bool(
            lock
            and lock.get("entry") == self.identity.entry
            and lock.get("pid") == self.identity.pid
            and lock.get("instance_id") == self.identity.instance_id
        )

    def _generation(self) -> str | None:
        try:
            raw = self.lock_path.read_bytes()
        except FileNotFoundError:
            return None
        return hashlib.sha256(raw).hexdigest()

    @contextmanager
    def _operation_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / OPERATION_FILENAME
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

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
                "entry": lock_payload.get("entry"),
                "pid": lock_payload.get("pid"),
                "instance_id": lock_payload.get("instance_id"),
                "token_hash": lock_payload.get("token_hash"),
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
            "generation": self._generation(),
            "budget": self.budget.snapshot(),
            "note": note,
        }

    def require(self, *, auto: bool = False) -> dict:
        """CLI 可在命令上下文懒申请；任何路径都只认可当前实例租约。"""
        context = current_lease_context()
        if (
            auto
            and self.identity == context.identity
            and not context.auto_acquire_attempted
        ):
            context.auto_acquire_attempted = True
            if not self.status()["lock_present"]:
                self.acquire(command=context.command)
                context.auto_acquired = True
        state = self.status()
        if state["leased"] and self._owns(state["lock"]):
            return state
        message = (
            "baostock 租约由其他进程实例持有"
            if state["leased"]
            else "当前进程实例未持有有效的 baostock 租约"
        )
        note = (
            "MCP 无锁时先询问用户，确认后调用 baostock_lock(acquire)；"
            if self.identity.entry == "mcp"
            else "CLI 已在首次实际抓取时尝试自动 acquire；"
        )
        raise BaostockLocked(
            message,
            lock=state["lock"],
            token_present=state["token_present"],
            token_valid=state["token_valid"],
            note=note + "其他实例持锁时等待持有方 release，确认终止后才可 release --force",
        )

    # --------------------------------------------------------------- lifecycle
    def acquire(
        self,
        *,
        owner: str | None = None,
        reason: str | None = None,
        command: str | None = None,
    ) -> dict:
        with self._operation_lock():
            return self._acquire(owner=owner, reason=reason, command=command)

    def _acquire(
        self,
        *,
        owner: str | None,
        reason: str | None,
        command: str | None,
    ) -> dict:
        """申请当前实例租约；已有锁文件（含损坏/token 不匹配）时拒绝覆盖。"""
        state = self.status()
        if state["lock_present"]:
            note = (
                "其他实例仍活跃时等待持有方 release；确认持有进程终止后"
                "才可 release --force，再重新 acquire"
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
            "version": 2,
            "owner": owner or _default_owner(),
            "entry": self.identity.entry,
            "pid": self.identity.pid,
            "instance_id": self.identity.instance_id,
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
                note="等待持有方 release；确认其终止后才可 force 清理 stale",
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

    def release(
        self,
        *,
        force: bool = False,
        expected: dict | str | None = None,
    ) -> dict:
        """持有实例正常释放；force 清理已人工确认 stale 的指定代际。"""
        initial = self.status()
        expected = expected if expected is not None else initial["generation"]
        with self._operation_lock():
            state = self.status()
            current = state["lock"]
            if isinstance(expected, dict):
                matches = bool(
                    current
                    and all(
                        current.get(key) == expected.get(key)
                        for key in ("entry", "pid", "instance_id", "token_hash")
                    )
                )
            else:
                matches = state["generation"] == expected
            if not state["lock_present"]:
                if not matches:
                    raise BaostockLocked("baostock 租约代际已经变化")
                if state["token_present"] and not force:
                    raise BaostockLocked(
                        "孤儿 token 没有持有实例，不能正常 release",
                        token_present=True,
                        token_valid=False,
                        note="确认残留来源后使用 release --force",
                    )
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
            if not matches:
                raise BaostockLocked(
                    "baostock 租约代际已经变化，拒绝删除其他实例的新租约",
                    lock=current,
                    token_present=state["token_present"],
                    token_valid=state["token_valid"],
                    note="重新 status 核对当前持有实例；不得重复执行旧 release/force",
                )
            if not force and (not state["leased"] or not self._owns(current)):
                raise BaostockLocked(
                    "正常 release 只允许持有有效租约的当前进程实例",
                    lock=current,
                    token_present=state["token_present"],
                    token_valid=state["token_valid"],
                    note="确认持有进程已经终止后，才可执行 release --force",
                )

            self.token_path.unlink(missing_ok=True)
            self.lock_path.unlink(missing_ok=True)
            return {
                "released": True,
                "forced": force,
                "token_removed": state["token_present"],
                "lock": current,
                "budget": state["budget"],
                "note": "当前实例的 baostock 租约已释放",
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
