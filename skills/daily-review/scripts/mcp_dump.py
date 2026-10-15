"""daily-review 落盘通道：stdio JSON-RPC 驱动同一 qstock-mcp server 的 tools/call，
大结果落盘为文件，可选本地聚合。

绕开 agent 工具结果的 50KB 预算，但仍走 MCP 工具层、不直连数据库。server 端
聚合/排序（query_snapshot aggregate/order_by/limit）可用时优先走聚合，本脚本
是聚合不可用时的备用标准通道。纯标准库，Python 3.11+。

用法：
    python mcp_dump.py --tool query_snapshot --params '{"trade_date":"20261014"}' \
        --out /tmp/qstock_dump/snapshot_20261014.json [--local-aggregate breadth,amount] \
        [--server ".venv/bin/qstock-mcp"] [--timeout 120]

--server 为 server 启动命令行（shlex 切分），缺省 .venv/bin/qstock-mcp（相对执行目录）。
--local-aggregate 对落盘 rows 本地聚合（breadth/amount，口径与 server 聚合一致：
涨跌家数按最新价 vs 昨收、最新价或昨收缺失的行进 no_price 桶、分市场按代码前缀
6→sh / 4,8,9→bj / 其余→sz，金额为库内万元口径），聚合结果打 stdout。

退出码：0 = ok；1 = 传输/工具错误（server 启动失败、握手失败、超时、isError）；
2 = 参数错误（argparse、params 非 JSON 对象、未知聚合项）。
"""

import argparse
import json
import os
import select
import shlex
import subprocess
import sys
from pathlib import Path

_PROTOCOL_VERSION = "2024-11-05"
_AGGREGATE_KINDS = ("breadth", "amount")


class DumpError(Exception):
    """传输/工具错误（exit 1）。"""


def market_bucket(stock_code: str) -> str:
    """分市场口径（与 server 一致）：6 开头沪市，4/8/9 开头北交所，其余深市。"""
    if stock_code.startswith("6"):
        return "sh"
    if stock_code.startswith(("4", "8", "9")):
        return "bj"
    return "sz"


def aggregate_rows(rows: list[dict], kinds: tuple[str, ...]) -> dict:
    """对快照 rows 本地聚合，结构与 server aggregate 模式一致。

    上涨/下跌/平盘按 latest_price vs pre_close 判定；任一缺失的行计入 no_price
    桶（停牌等），up+down+flat+no_price = 总行数。无行时各项为 0 / 0.0；
    by_market 恒含 sh/sz/bj 三键。
    """
    grouped = {
        m: {"up": 0, "down": 0, "flat": 0, "no_price": 0, "amount": 0.0}
        for m in ("sh", "sz", "bj")
    }
    for row in rows:
        bucket = grouped[market_bucket(str(row.get("stock_code", "")))]
        latest, pre = row.get("latest_price"), row.get("pre_close")
        if latest is None or pre is None:
            bucket["no_price"] += 1
        else:
            key = "up" if latest > pre else "down" if latest < pre else "flat"
            bucket[key] += 1
        amount = row.get("amount")
        if amount is not None:
            bucket["amount"] += float(amount)
    out = {}
    if "breadth" in kinds:
        keys = ("up", "down", "flat", "no_price")
        by_market = {m: {k: grouped[m][k] for k in keys} for m in grouped}
        out["breadth"] = {
            "total": {k: sum(v[k] for v in by_market.values()) for k in keys},
            "by_market": by_market,
        }
    if "amount" in kinds:
        by_market = {m: grouped[m]["amount"] for m in grouped}
        out["amount"] = {"total_amount": sum(by_market.values()), "by_market": by_market}
    return out


class McpStdioClient:
    """最小 stdio JSON-RPC 客户端：initialize → notifications/initialized → tools/call。"""

    def __init__(self, server_cmd: list[str], timeout: float):
        try:
            self._proc = subprocess.Popen(
                server_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            raise DumpError(f"server 启动失败: {server_cmd!r}: {e}") from e
        self._timeout = timeout
        self._next_id = 0
        self._buf = b""  # 自带行缓冲：select 直接作用裸 fd，不能用带缓冲的 text 模式

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
        self._proc.wait()

    def _send(self, message: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def _readline(self) -> str:
        assert self._proc.stdout is not None
        while b"\n" not in self._buf:
            ready, _, _ = select.select([self._proc.stdout], [], [], self._timeout)
            if not ready:
                raise DumpError(f"等待 server 响应超时（{self._timeout}s）")
            chunk = os.read(self._proc.stdout.fileno(), 65536)
            if not chunk:
                raise DumpError(f"server 异常退出（exit {self._proc.poll()}），无响应")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8")

    def _read_response(self, request_id: int) -> dict:
        """读到 id 匹配的响应为止（跳过 server 通知）；超时或 server 退出报错。"""
        while True:
            line = self._readline().strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # server 可能往 stdout 打非 JSON 行，跳过
            if message.get("id") == request_id:
                if "error" in message:
                    raise DumpError(f"JSON-RPC 错误: {message['error']}")
                return message.get("result", {})

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return self._read_response(self._next_id)

    def handshake(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "daily-review-mcp-dump", "version": "0.1.0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, tool: str, params: dict) -> dict:
        """tools/call → 解析 content 首个 text 为工具返回 JSON；isError 报 DumpError。"""
        result = self._request("tools/call", {"name": tool, "arguments": params})
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        if result.get("isError"):
            raise DumpError(f"工具返回错误: {texts[0] if texts else result}")
        if not texts:
            raise DumpError(f"工具响应无 text content: {result}")
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError as e:
            raise DumpError(f"工具返回非 JSON: {e}") from e


def main() -> int:
    parser = argparse.ArgumentParser(description="stdio 落盘通道：tools/call 大结果落盘 + 本地聚合")
    parser.add_argument("--tool", required=True, help="MCP 工具名（如 query_snapshot）")
    parser.add_argument("--params", default="{}", help="工具参数 JSON 对象")
    parser.add_argument("--out", required=True, help="结果落盘路径（父目录自动创建）")
    parser.add_argument("--local-aggregate", default=None, help="逗号分隔聚合项：breadth,amount")
    parser.add_argument("--server", default=".venv/bin/qstock-mcp", help="server 启动命令行")
    parser.add_argument("--timeout", type=float, default=120.0, help="单次等待 server 响应秒数")
    args = parser.parse_args()

    try:
        params = json.loads(args.params)
        if not isinstance(params, dict):
            raise ValueError("params 必须是 JSON 对象")
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return 2
    kinds: tuple[str, ...] = ()
    if args.local_aggregate is not None:
        kinds = tuple(k.strip() for k in args.local_aggregate.split(","))
        unknown = [k for k in kinds if k not in _AGGREGATE_KINDS]
        if not kinds or unknown:
            print(f"参数错误: 未知聚合项 {args.local_aggregate}（可选: {', '.join(_AGGREGATE_KINDS)}）", file=sys.stderr)
            return 2

    client = None
    try:
        client = McpStdioClient(shlex.split(args.server), args.timeout)
        client.handshake()
        payload = client.call_tool(args.tool, params)
    except DumpError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    summary: dict = {"status": "ok", "out": str(out_path), "count": payload.get("count")}
    if kinds:
        rows = payload.get("rows")
        if not isinstance(rows, list):
            print("错误: 落盘结果无 rows，无法本地聚合", file=sys.stderr)
            return 1
        summary["aggregates"] = aggregate_rows(rows, kinds)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
