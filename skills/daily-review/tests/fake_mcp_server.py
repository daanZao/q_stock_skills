"""mcp_dump 测试用 fake MCP stdio server：录制的 JSON-RPC 响应，不依赖真实 PG/网络。

逐行读 stdin 的 JSON-RPC 消息：
- initialize → 录制握手响应（响应前先发一条 server 通知，验证客户端跳过通知）；
- notifications/initialized → 不响应；
- tools/call → 按 params.arguments.trade_date 分派录制载荷：
  "error" → isError 工具错误；"badjson" → text 非 JSON；其余 → 小快照载荷。
"""

import json
import sys

SNAPSHOT_ROWS = [
    {"stock_code": "600519", "latest_price": 10.0, "pre_close": 9.0, "amount": 100.0},
    {"stock_code": "600001", "latest_price": 8.0, "pre_close": 8.5, "amount": 50.0},
    {"stock_code": "000001", "latest_price": 12.0, "pre_close": 12.0, "amount": 200.0},
    {"stock_code": "300750", "latest_price": None, "pre_close": 20.0, "amount": 300.0},
    {"stock_code": "430047", "latest_price": 5.0, "pre_close": 4.0, "amount": 10.0},
    {"stock_code": "830799", "latest_price": 3.0, "pre_close": 4.0, "amount": None},
]

SNAPSHOT_PAYLOAD = {
    "status": "ok",
    "tool": "query_snapshot",
    "params": {"trade_date": "20261014"},
    "trade_date": "20261014",
    "count": len(SNAPSHOT_ROWS),
    "rows": SNAPSHOT_ROWS,
}


def _respond(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


def _tool_result(payload_text, is_error=False):
    result = {"content": [{"type": "text", "text": payload_text}]}
    if is_error:
        result["isError"] = True
    return result


def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        if method == "initialize":
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "method": "notifications/message",
                            "params": {"level": "info", "data": "fake ready"}}) + "\n"
            )
            _respond(message["id"], {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake-qstock-mcp", "version": "0"},
            })
        elif method == "tools/call":
            trade_date = message["params"].get("arguments", {}).get("trade_date")
            if trade_date == "error":
                _respond(message["id"], _tool_result("上游不可达：全部数据源失败", is_error=True))
            elif trade_date == "badjson":
                _respond(message["id"], _tool_result("not json"))
            else:
                _respond(message["id"], _tool_result(json.dumps(SNAPSHOT_PAYLOAD)))
        # notifications/* 与其他方法不响应


if __name__ == "__main__":
    main()
