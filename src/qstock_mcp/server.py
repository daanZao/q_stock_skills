"""MCP server 入口：MCPServer（mcp 2.x 高层 API）+ stdio。

工具层只做薄包装：核心逻辑在 tools_* 模块，测试直接打核心层（接缝见 tests/）。
数据库连接在工具调用时按需建立，server 启动本身不依赖 PG 可达。
"""

from mcp.server.mcpserver import MCPServer

from . import tools_init

mcp = MCPServer("qstock-mcp")


@mcp.tool()
def init_database() -> dict:
    """初始化数据库：幂等建表（stock_daily / market_snapshot / 盘面表 / conclusions 等）。"""
    return tools_init.init_database()


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
