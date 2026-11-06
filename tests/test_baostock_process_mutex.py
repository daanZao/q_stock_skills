"""#173 真实双进程实例租约验收；fake 上游，不发 baostock 请求。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from qstock_mcp import scheduler, server


def _child_code(root: Path, *, hold: bool) -> str:
    return f'''
import json
import time
from pathlib import Path
from types import SimpleNamespace
from qstock_mcp import scheduler
from qstock_mcp.adapters.baostock_adapter import BaostockAdapter
import qstock_mcp.adapters.baostock_adapter as adapter_module

root = Path({str(root)!r})
class Api:
    def fetch_fundamentals(self, code):
        (root / "api-called").write_text(code)
        if {hold!r}:
            (root / "ready").write_text(code)
            deadline = time.monotonic() + 10
            while not (root / "finish").exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("test hold timeout")
                time.sleep(0.02)
        return {{"profit": [{{"code": code}}], "growth": []}}

adapter_module.BaostockApi = SimpleNamespace(get=lambda: Api())
rc = scheduler.main(
    ["fundamentals", "--codes", "600519"],
    fundamentals_adapters=[BaostockAdapter()],
)
raise SystemExit(rc)
'''


def _start_child(root: Path, *, hold: bool) -> subprocess.Popen:
    env = os.environ.copy()
    env["QSTOCK_BAOSTOCK_LOCK_DIR"] = str(root)
    return subprocess.Popen(
        [sys.executable, "-c", _child_code(root, hold=hold)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_ready(root: Path, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 5
    while not (root / "ready").exists():
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(f"holder exited early: {proc.returncode} {stdout} {stderr}")
        if time.monotonic() > deadline:
            raise AssertionError("holder did not reach fake baostock call")
        time.sleep(0.02)


def test_active_cli_lease_blocks_other_cli_and_mcp_until_finally_release(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(
        server.tools_daily,
        "fetch_daily",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("MCP 不得越过实例租约门禁")
        ),
    )
    holder = _start_child(tmp_path, hold=True)
    try:
        _wait_ready(tmp_path, holder)
        locked_mcp = server.fetch_daily("600519", days=5)
        assert locked_mcp["error_kind"] == "baostock_locked"
        assert locked_mcp["lock"]["entry"] == "cli"

        other = _start_child(tmp_path, hold=False)
        stdout, stderr = other.communicate(timeout=10)
        assert other.returncode == 1, stderr
        assert json.loads(stdout)["error_kind"] == "baostock_locked"

        (tmp_path / "finish").write_text("done")
        stdout, stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, stderr
        assert json.loads(stdout)["status"] == "ok"
        assert not (tmp_path / "baostock.lock").exists()

        acquired = server.baostock_lock("acquire")
        assert acquired["status"] == "ok"
        assert server.baostock_lock("release")["released"] is True
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        server.baostock_lock("release", force=True)
        capsys.readouterr()


def test_crashed_cli_leaves_stale_lease_until_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QSTOCK_BAOSTOCK_LOCK_DIR", str(tmp_path))
    holder = _start_child(tmp_path, hold=True)
    try:
        _wait_ready(tmp_path, holder)
        holder.kill()
        holder.wait(timeout=5)

        assert (tmp_path / "baostock.lock").exists()
        other = _start_child(tmp_path, hold=False)
        stdout, stderr = other.communicate(timeout=10)
        assert other.returncode == 1, stderr
        assert json.loads(stdout)["error_kind"] == "baostock_locked"

        rc = scheduler.main(["baostock-lock", "release"])
        normal = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert normal["error_kind"] == "invalid_argument"
        assert (tmp_path / "baostock.lock").exists()

        rc = scheduler.main(["baostock-lock", "release", "--force"])
        forced = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert forced["released"] is True
        assert not (tmp_path / "baostock.lock").exists()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        capsys.readouterr()
