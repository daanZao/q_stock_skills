# q_stock_skills

面向通用 AI agent 的证券数据与分析能力：一个 MCP 数据服务（Python，stdio 入口）+ 四个可加载的 agent skill。它是旧 B/S 分析系统 Server 端的替代品——数据由 agent 直接获取，分析结论由 agent 用 skill 产出并持久化。只做 A 股。

**5 分钟全图认知见 [`docs/knowledge-graph.md`](docs/knowledge-graph.md)**；领域词汇见 [`CONTEXT.md`](CONTEXT.md)；设计决策见 [`docs/adr/`](docs/adr/)。

## 组成

- **MCP 数据服务**（`src/qstock_mcp`，14 个工具，五个能力面）：
  - `init_database`：幂等建表 + 轻量初始化（股票清单/全市场快照/主要指数日线），可选 `backfill_history=True` 全量回溯
  - `fetch_daily` / `query_daily`：个股日线抓取与查询，**查询自愈**——库内缺数据自动补抓头尾缺口
  - `query_index_daily`：主要指数历史日线纯读（7 个指数白名单），数据由 `init_database` 写入
  - `fetch_market_snapshot` / `query_snapshot`：全市场快照（约 5000+ 只），业务键幂等 upsert
  - `fetch_board_snapshot` / `query_board_data`：盘面快照（指数/板块/涨跌停池/强势股/龙虎榜），section 级独立成败
  - `get_fundamentals`：基本面透传（proxy），不规格化、不落库
  - `mx_query` / `mx_search` / `query_news`：妙想自然语言查数与资讯搜索（独立日配额，搜索落库 `news_items`）+ 库内资讯纯读
  - `save_conclusion` / `query_conclusions`：分析结论读写，通用结论表 + JSON payload，同键 upsert
- **四个 agent skill**（`skills/`，自包含，任何通用 AI agent 可加载）：
  - `indicator-tools`：指标计算管道脚本（MA/MACD/BOLL/RSI/导数、corr 多标的相关性矩阵、vol_price 量价背离、promote 连板晋级率等），stdin 吃 `query_daily`/`query_index_daily` 输出、stdout 吐指标 JSON，纯标准库不触库
  - `sepa`：SEPA 方法论判断，经 MCP 取数 + indicator-tools 管道算指标
  - `limit-up-analysis`：涨停/连板分析——涨停归因（板块/大小票/业绩题材粗分）+ 连板梯队与晋级率 + 龙头点评，结论落库 `limit_up.close`，可单独调用
  - `daily-review`：每日复盘编排（七块内容框架：指数/广度/板块/涨停连板/成交额前 75 结构/归因与量价/龙虎榜），结论经 `save_conclusion` 落库、markdown 报告落盘；附 launchd/cron 调度配置说明
- **数据源**：efinance → akshare → baostock 多源 fallback（基本面链为 akshare → efinance → baostock）；失败明确报错，绝不伪造数据
- **数据库**：PostgreSQL 独立新库（14 张表，DDL 在 `src/qstock_mcp/sql/`），连接只读 `PG_DSN` 环境变量

## 安装与接入

要求 Python ≥ 3.11 与一个 PostgreSQL 实例。

```bash
pip install -e ".[sources,dev]"   # sources = efinance/akshare/baostock 真实数据源库
```

注意 `qstock-mcp` 入口只装在执行 `pip install` 的那个环境里（本仓库即 `.venv/`），因此 `command` 应写成该环境内的**绝对路径**；或改用 `python -m` 形式指向同一解释器：

```json
{
  "mcpServers": {
    "qstock": {
      "command": "/path/to/q_stock_skills/.venv/bin/qstock-mcp",
      "env": { "PG_DSN": "postgresql://user:pass@host:5432/qstock" }
    }
  }
}
```

```json
{
  "mcpServers": {
    "qstock": {
      "command": "/path/to/q_stock_skills/.venv/bin/python",
      "args": ["-m", "qstock_mcp.server"],
      "env": { "PG_DSN": "postgresql://user:pass@host:5432/qstock" }
    }
  }
}
```

想脱离项目目录全局可用，就装进常驻环境（如 `pipx install .` 或用户级 pip），让 `qstock-mcp` 落在 PATH 上；否则一律写绝对路径。

环境变量一律放进 MCP client 配置的 `env` 块里——GUI 类 client 不一定继承你 shell 里 `export` 的变量。完整清单：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `PG_DSN` | 是 | PostgreSQL 连接串；除 `get_fundamentals`/`mx_query` 外的落库/查询工具都依赖它，缺失报明确错误 |
| `MX_APIKEY` | 否 | 妙想开放平台 apikey，仅 `mx_query`/`mx_search` 需要 |
| `MX_DAILY_LIMIT` | 否 | 妙想工具通用每日配额上限（缺省 20） |
| `MX_DAILY_LIMIT_MX_DATA` / `MX_DAILY_LIMIT_MX_SEARCH` | 否 | 单工具配额覆盖，优先于 `MX_DAILY_LIMIT` |
| `MX_QUOTA_FILE` | 否 | 配额 ledger 路径（缺省 `~/.qstock-mcp/quota.json`） |

`env` 块示例（按需取舍可选项）：

```json
"env": {
  "PG_DSN": "postgresql://user:pass@host:5432/qstock",
  "MX_APIKEY": "your-mx-apikey",
  "MX_DAILY_LIMIT": "20"
}
```

首次使用调一次 `init_database` 即可完成建表与轻量初始化，秒级就绪。

## 启动方式

这是 stdio MCP server：**不需要手动启动**，由 MCP client（Claude Code / Kimi Code / 其他 agent）按上面的配置自动拉起——client 通过子进程的 stdin/stdout 与它通信，会话结束自动回收。配置后重启 client 即可使用 `qstock` 前缀的 14 个工具。

需要脱离 client 手动验证时（冒烟调试），直接运行入口命令：

```bash
qstock-mcp                    # 等价于 python -m qstock_mcp.server
```

进程启动后挂起等待 stdin 上的 JSON-RPC 消息，没有输出属正常（日志只走 stderr）。发一行 initialize 握手可验证服务可用（stdio 通道为换行分隔 JSON）：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' | qstock-mcp
```

Ctrl+C 退出。握手本身不触库；若要手动实测工具调用，先在当前 shell 里 `export PG_DSN`（以及可选的 `MX_APIKEY`）。注意：手动启动走的是前台 stdio 通道，不是常驻后台服务；要让 agent 长期使用，仍应配进 MCP client 由它管理生命周期。

## 输出契约

所有工具与 skill 脚本输出自描述 JSON：成功 `status:"ok"` + 参数回显 + 实际数据源；数据不足 `status:"insufficient_data"`（非错误）；失败 `status:"error"` + 已尝试数据源与原因。日志只走 stderr，不进数据通道。

## 测试

```bash
pytest tests/ skills/
```

两条测试接缝：MCP 工具函数层（注入 fake 适配器 + 真实 PG 测试库，PG 不可达自动 skip）与 skill 脚本 stdin/stdout 契约。网络抓取不进测试。

## 文档导航

- [`docs/knowledge-graph.md`](docs/knowledge-graph.md)：项目知识图谱——总览图、实体清单、关键流程、「想改 X 先看哪里」
- [`CONTEXT.md`](CONTEXT.md)：领域词汇表
- [`docs/adr/`](docs/adr/)：设计决策（MCP 边界 / PG 选型 / 通用结论表 / init 设计 / 妙想接入）
- [`docs/agents/`](docs/agents/)：agent 协作约定（issue 流程、triage 标签）
