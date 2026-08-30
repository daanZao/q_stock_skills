"""工具输出的公共构造器（输出契约见 issue #1：status + 参数回显 + 明确原因）。"""


def error(tool: str, params: dict, msg: str, **extra) -> dict:
    """统一错误输出：status:"error" + 工具名 + 参数回显 + 原因。"""
    return {"status": "error", "tool": tool, "params": params, "error": msg, **extra}
