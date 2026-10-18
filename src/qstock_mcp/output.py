"""工具输出的公共构造器（输出契约见 issue #1：status + 参数回显 + 明确原因）。"""


def error(tool: str, params: dict, msg: str, **extra) -> dict:
    """统一错误输出：status:"error" + 工具名 + 参数回显 + 原因。"""
    return {"status": "error", "tool": tool, "params": params, "error": msg, **extra}


def aggregate_batch_status(total: int, failed: int) -> str:
    """批次三态聚合：全失败→error，有失败→partial_error，否则 ok。

    total=0（无事项可办）归 ok——空批次不是失败。各批命令（次晨批/回补/
    成分积累）共用本函数，状态词汇不得各自为政。
    """
    if failed and failed == total:
        return "error"
    if failed:
        return "partial_error"
    return "ok"
