# q_stock_skills

面向通用 AI agent 的证券数据与分析能力：一个 MCP 数据服务（Python，stdio 入口）+ 三个可加载的 agent skill。它是旧 B/S 分析系统 Server 端的替代品——数据由 agent 直接获取，分析结论由 agent 用 skill 产出并持久化。只做 A 股。

**5 分钟全图认知见 [`docs/knowledge-graph.md`](docs/knowledge-graph.md)**；领域词汇见 [`CONTEXT.md`](CONTEXT.md)；设计决策见 [`docs/adr/`](docs/adr/)。

## 组成

- **MCP 数据服务**（`src/qstock_mcp`，10 个工具，五个能力面）：
  - `init_database`：幂等建表 + 轻量初始化（股票清单/全市场快照/主要指数日线），可选 `backfill_history=True` 全量回溯
  - `fetch_daily` / `query_daily`：个股日线抓取与查询，**查询自愈**——库内缺数据自动补抓头尾缺口
  - `fetch_market_snapshot` / `query_snapshot`：全市场快照（约 5000+ 只），业务键幂等 upsert
  - `fetch_board_snapshot` / `query_board_data`：盘面快照（指数/板块/涨跌停池/强势股/龙虎榜），section 级独立成败
  - `get_fundamentals`：基本面透传（proxy），不规格化、不落库
  - `save_conclusion` / `query_conclusions`：分析结论读写，通用结论表 + JSON payload，同键 upsert
- **三个 agent skill**（`skills/`，自包含，任何通用 AI agent 可加载）：
  - `indicator-tools`：指标计算管道脚本（MA/MACD/BOLL/RSI/导数等），stdin 吃 `query_daily` 输出、stdout 吐指标 JSON，纯标准库不触库
  - `sepa`：SEPA 方法论判断，经 MCP 取数 + indicator-tools 管道算指标
  - `daily-review`：每日复盘编排，结论经 `save_conclusion` 落库、markdown 报告落盘
- **数据源**：efinance → akshare → baostock 多源 fallback（基本面链为 akshare → efinance → baostock）；失败明确报错，绝不伪造数据
- **数据库**：PostgreSQL 独立新库（14 张表，DDL 在 `src/qstock_mcp/sql/`），连接只读 `PG_DSN` 环境变量

## 安装与接入

要求 Python ≥ 3.11 与一个 PostgreSQL 实例。

```bash
pip install -e ".[sources,dev]"   # sources = efinance/akshare/baostock 真实数据源库
export PG_DSN="postgresql://user:pass@host:5432/qstock"
```

MCP stdio 配置（任何 MCP client 通用）：

```json
{
  "mcpServers": {
    "qstock": {
      "command": "qstock-mcp",
      "env": { "PG_DSN": "postgresql://user:pass@host:5432/qstock" }
    }
  }
}
```

首次使用调一次 `init_database` 即可完成建表与轻量初始化，秒级就绪。

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
- [`docs/adr/`](docs/adr/)：设计决策（MCP 边界 / PG 选型 / 通用结论表 / init 设计）
- [`docs/agents/`](docs/agents/)：agent 协作约定（issue 流程、triage 标签）
