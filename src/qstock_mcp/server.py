"""MCP server 入口：MCPServer（mcp 2.x 高层 API）+ stdio。

工具层只做薄包装：核心逻辑在 tools_* 模块，测试直接打核心层（接缝见 tests/）。
数据库连接在工具调用时按需建立，server 启动本身不依赖 PG 可达。
"""

from typing import Any

from mcp.server.mcpserver import MCPServer

from . import (
    tools_board,
    tools_conclusions,
    tools_daily,
    tools_fundamentals,
    tools_init,
    tools_mx,
    tools_snapshot,
)

mcp = MCPServer("qstock-mcp")


@mcp.tool()
def init_database(backfill_history: bool = False) -> dict:
    """初始化数据库：幂等建表 + 轻量初始化数据（股票清单、全市场快照、主要指数日线），各部分独立成败报告；重复调用幂等（无重复行）。

    backfill_history=True 追加全市场个股历史日线回溯（重操作：数千只 × 全历史，
    显式开启，绝非默认；进度与失败在 parts.backfill 中报告，单股失败不中断）。
    """
    return tools_init.init_database(backfill_history)


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
def query_index_daily(
    index_code: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 120,
) -> dict:
    """库内主要指数历史日线查询（纯读，不触发补抓）：按日期范围过滤，按 trade_date 升序返回最近 limit 行（缺省 120）。

    index_code 限 7 个主要指数：000001(上证指数)/399001(深证成指)/399006(创业板指)/
    000300(沪深300)/000016(上证50)/000905(中证500)/000688(科创50)，数据由
    init_database 轻量初始化写入全历史。start/end 为闭区间（yyyymmdd 或
    yyyy-mm-dd），缺省不限；空结果返回 count:0（非错误）。
    """
    return tools_init.query_index_daily(index_code, start=start, end=end, limit=limit)


@mcp.tool()
def fetch_market_snapshot(trade_date: str | None = None) -> dict:
    """抓取全市场快照（约 5000+ 只 A 股）并落库：单次调用，efinance → akshare → baostock fallback，按 (trade_date, stock_code) 幂等 upsert。

    API 返回的最新交易日优先于传入的 trade_date（yyyymmdd 或 yyyy-mm-dd）。
    错误三态（ADR 0007）：全源失败时 error_kind=upstream_unreachable（连接级故障，
    带 retry_advice 与逐源 attempted_sources 明细）或 fetch_failed；上游正常响应但
    该日期无数据返回 status ok + empty_kind=no_data_for_date（非失败）。
    """
    return tools_snapshot.fetch_market_snapshot(trade_date)


@mcp.tool()
def query_snapshot(
    trade_date: str | None = None,
    stock_code: str | None = None,
    aggregate: str | None = None,
    order_by: str | None = None,
    order: str = "desc",
    limit: int | None = None,
) -> dict:
    """库内快照查询：按日期/代码过滤；日期缺省取库内最新交易日。日期格式 yyyymmdd 或 yyyy-mm-dd。

    aggregate（逗号分隔，可选 breadth,amount）为服务端聚合模式：返回上涨/下跌/平盘
    家数（最新价 vs 昨收判定，含沪/深/北分市场口径）与两市成交额合计（万元，ADR 0006
    口径），不返回行；环比由调用方对当日/前一日各调一次完成。order_by（快照列名，如
    amount）+ limit=N 为服务端排序取前 N（order asc/desc，缺省 desc），行字段与整表
    查询一致（不裁剪列）。不提供通用 offset 分页。不传新参数时行为与整表查询一致。
    """
    return tools_snapshot.query_snapshot(
        trade_date, stock_code, aggregate, order_by, order, limit
    )


@mcp.tool()
def fetch_board_snapshot(trade_date: str | None = None, sections: str | None = None) -> dict:
    """抓取盘面快照并落库：指数/板块/涨跌停池/强势股/龙虎榜五 section 独立抓取、独立 commit（中途失败不回滚，按 trade_date 幂等，可用 sections 分段重试）。

    trade_date: yyyymmdd 或 yyyy-mm-dd，缺省今天；sections: 逗号分隔子集
    （可选 indices,boards,zt_pool,strong_stocks,lhb），缺省全量。
    核心 section（indices/boards/zt_pool/strong_stocks，ADR 0007 三态契约）因上游不可达
    失败 → 整体 error（error_kind=upstream_unreachable + retry_advice + per-section
    成败/行数/逐源明细）；lhb 永不进核心集。盘中龙虎榜无数据返回
    rows:0 + note + empty_kind=no_data_for_date（盘后发布，非失败）；lhb 抓取失败返回
    status error + error_kind + attempted_sources，与"未发布"可区分。
    """
    return tools_board.fetch_board_snapshot(trade_date, sections)


@mcp.tool()
def query_board_data(
    table: str, trade_date: str | None = None, code: str | None = None
) -> dict:
    """库内盘面数据查询：按表/日期/代码过滤；日期缺省取该表库内最新日期。

    table 可选：market_indices/market_boards/zt_pool/strong_stocks/lhb_basic/
    lhb_stock_detail/lhb_stock_statistic/lhb_yyb_capital/lhb_yyb_most。
    code 为各表业务代码（指数代码/板块名称/股票代码/营业部名称）。
    """
    return tools_board.query_board_data(table, trade_date, code)


@mcp.tool()
def get_fundamentals(stock_code: str) -> dict:
    """基本面数据透传（proxy 能力面）：按个股代码返回上游原始数据（财务指标、估值等），不规格化、不落库。

    输出自描述 JSON：data 为 {section: 原始记录}（字段名保留上游），source 为实际
    数据源（akshare → efinance → baostock fallback）；全失败返回 status:"error"
    与各源错误，绝不伪造数据。stock_code: 6 位 A 股代码（如 600519）。
    """
    return tools_fundamentals.get_fundamentals(stock_code)


@mcp.tool()
def mx_query(tool_query: str) -> dict:
    """妙想 mx-data 透传（proxy 能力面）：把自然语言问句原样发给 mx-data，返回上游原始 JSON（data 为完整响应 body），不规格化、不落库、不进 fallback 链。

    需要环境变量 MX_APIKEY（妙想开放平台 apikey），未配置返回 status:"error" 与
    明确原因。本地每日配额：默认上限 20 次/日（MX_DAILY_LIMIT 通用覆盖，
    MX_DAILY_LIMIT_MX_DATA 单 skill 覆盖），按日自动重置，持久化于
    ~/.qstock-mcp/quota.json（MX_QUOTA_FILE 覆盖）；触顶时不调上游直接报错。
    每次响应回显 quota:{skill,used,limit}；上游业务码非 0（100 参数错误 /
    113 配额上限 / 114 密钥无效）走统一 error 契约，绝不伪造数据。
    """
    return tools_mx.mx_query(tool_query)


@mcp.tool()
def mx_search(
    query: str, subject_type: str = "market", subject_code: str = "_market"
) -> dict:
    """妙想 mx-search 资讯搜索并落库（proxy 能力面）：搜索新闻/公告/研报等资讯，按 news_code 去重后写入 news_items 表（业务键 (news_code, subject_type, subject_code)，重复调用幂等 upsert，报告 inserted/updated/skipped 计数）。

    需要环境变量 MX_APIKEY 与 PG_DSN，未配置返回 status:"error" 与明确原因。
    subject 缺省大盘级（market/_market）；个股资讯传 subject_type="stock" +
    subject_code=6 位代码，同一资讯可挂多个 subject。本地每日配额独立于
    mx_query：默认上限 20 次/日（MX_DAILY_LIMIT 通用覆盖，
    MX_DAILY_LIMIT_MX_SEARCH 单 skill 覆盖），触顶不调上游；每次响应回显
    quota:{skill,used,limit}；上游业务码非 0 走统一 error 契约，绝不伪造数据。
    """
    return tools_mx.mx_search(
        query, subject_type=subject_type, subject_code=subject_code
    )


@mcp.tool()
def query_news(
    subject_type: str | None = None,
    subject_code: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 20,
) -> dict:
    """库内资讯查询（统一查询能力面）：按 subject + 发布时间范围读 news_items 已沉淀资讯，按资讯发布时间倒序返回，limit 限量（默认 20）。

    纯读：不调上游、不计配额。subject_type/subject_code 缺省不过滤（全 subject）；
    个股传 subject_type="stock" + subject_code=6 位代码，大盘级为 market/_market。
    start/end 为闭区间（yyyymmdd 或 yyyy-mm-dd，按东八区整日计），缺省不限。
    空结果返回 count:0（非错误）。
    """
    return tools_mx.query_news(
        subject_type=subject_type,
        subject_code=subject_code,
        start=start,
        end=end,
        limit=limit,
    )


@mcp.tool()
def save_conclusion(
    subject_type: str,
    subject_code: str,
    trade_date: str,
    conclusion_type: str,
    payload: Any,
) -> dict:
    """写入一条分析结论：业务唯一键 (subject_type, subject_code, trade_date, conclusion_type)，同键重复写入为 upsert（outcome 报告 inserted/updated）。

    payload 为任意 JSON，结构由写入方 skill 自行约定，server 不校验。
    subject_type: market | stock；market 级结论 subject_code 用 '_market'。
    trade_date: yyyymmdd 或 yyyy-mm-dd；conclusion_type 如 daily_review.close / sepa.stage。
    """
    return tools_conclusions.save_conclusion(
        subject_type, subject_code, trade_date, conclusion_type, payload
    )


@mcp.tool()
def query_conclusions(
    subject_type: str | None = None,
    subject_code: str | None = None,
    trade_date: str | None = None,
    conclusion_type: str | None = None,
) -> dict:
    """库内结论查询：按 subject_type / subject_code / trade_date / conclusion_type 任意组合过滤，全部缺省返回全表。日期格式 yyyymmdd 或 yyyy-mm-dd。"""
    return tools_conclusions.query_conclusions(
        subject_type, subject_code, trade_date, conclusion_type
    )


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
