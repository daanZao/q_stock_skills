"""MCP server 入口：MCPServer（mcp 2.x 高层 API）+ stdio。

工具层只做薄包装：核心逻辑在 tools_* 模块，测试直接打核心层（接缝见 tests/）。
数据库连接在工具调用时按需建立，server 启动本身不依赖 PG 可达。
"""

from mcp.server.mcpserver import MCPServer

from . import tools_daily, tools_init, tools_snapshot

mcp = MCPServer("qstock-mcp")


@mcp.tool()
def init_database() -> dict:
    """初始化数据库：幂等建表（stock_daily / market_snapshot / 盘面表 / conclusions 等）。"""
    return tools_init.init_database()


@mcp.tool()
def fetch_daily(
    stock_code: str,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    adj: str = "qfq",
) -> dict:
    """抓取个股日线并落库：只补请求区间头尾缺口，efinance → akshare → baostock fallback，按 (stock_code, trade_date, adj) 幂等 upsert。

    stock_code: 6 位 A 股代码（如 600519）。days 与 start/end 二选一；
    日期格式 yyyymmdd 或 yyyy-mm-dd；adj: qfq/hfq/none。
    """
    return tools_daily.fetch_daily(stock_code, days=days, start=start, end=end, adj=adj)


@mcp.tool()
def query_daily(
    stock_code: str,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    adj: str = "qfq",
) -> dict:
    """查询个股日线：库里缺数据自动补抓（自愈）后返回完整区间，参数同 fetch_daily。"""
    return tools_daily.query_daily(stock_code, days=days, start=start, end=end, adj=adj)


@mcp.tool()
def fetch_market_snapshot(trade_date: str | None = None) -> dict:
    """抓取全市场快照（约 5000+ 只 A 股）并落库：单次调用，efinance → akshare → baostock fallback，按 (trade_date, stock_code) 幂等 upsert。

    API 返回的最新交易日优先于传入的 trade_date（yyyymmdd 或 yyyy-mm-dd）。
    """
    return tools_snapshot.fetch_market_snapshot(trade_date)


@mcp.tool()
def query_snapshot(trade_date: str | None = None, stock_code: str | None = None) -> dict:
    """库内快照查询：按日期/代码过滤；日期缺省取库内最新交易日。日期格式 yyyymmdd 或 yyyy-mm-dd。"""
    return tools_snapshot.query_snapshot(trade_date, stock_code)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
