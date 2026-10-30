# q_stock_skills

**让 AI agent 直接获取 A 股数据、执行分析并沉淀结论。**

一个项目，两类交付：可接入 Claude Code / Kimi Code 等客户端的 **MCP 数据服务**，以及四个可独立加载的 **agent skill**。数据落 PostgreSQL，采集与补录由统一的 `qstock-scheduler` CLI 执行。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-336791)](src/qstock_mcp/sql/)
[![MCP](https://img.shields.io/badge/Protocol-MCP-6f42c1)](src/qstock_mcp/server.py)

## 为什么用它

- **数据与分析一站式接入**：个股、指数、板块、龙虎榜、涨停池、资讯、基本面与分析结论。
- **为 agent 设计**：统一 MCP 工具、自描述 JSON、明确的错误分类，无需理解底层数据源细节。
- **历史数据可准备**：按交易日窗口回补与整窗刷新，用覆盖核验确认数据是否就绪。
- **日常采集可调度**：次晨批、收盘后批和单数据面命令共用同一 CLI。
- **结果可复用**：分析结论持久化，四个 skill 覆盖指标计算、SEPA、每日复盘与涨停分析。

## 核心组成

### MCP 数据服务

| 能力 | 内容 |
| --- | --- |
| 抓取与落库 | 多源 fallback、业务键幂等写入、采集台账 |
| 统一查询 | 个股/指数日线、全市场快照、盘面与板块数据 |
| 基本面与资讯 | 基本面透传、妙想自然语言查数、资讯搜索与查询 |
| 结论存储 | 通用 JSON 结论表，供不同分析 skill 复用 |
| 初始化与补录 | 幂等建表、轻量初始化、交易日窗口回补与覆盖核验 |

### Agent skill

| Skill | 用途 |
| --- | --- |
| [`indicator-tools`](skills/indicator-tools/) | MA、MACD、BOLL、RSI、相关性、晋级率等指标计算管道 |
| [`sepa`](skills/sepa/) | SEPA 方法论选股与判断 |
| [`daily-review`](skills/daily-review/) | 每日市场复盘、数据就绪检查、报告与结论落库 |
| [`limit-up-analysis`](skills/limit-up-analysis/) | 涨停结构、连板梯队、题材归因与龙头分析 |

## 安装与接入

要求 **Python ≥3.11** 和一个 **PostgreSQL** 数据库。

### 1. 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[sources,dev]"
```

同一次安装会注册 `qstock-mcp` 和 `qstock-scheduler`。

### 2. 配置 MCP client

```json
{
  "mcpServers": {
    "qstock": {
      "command": "/path/to/q_stock_skills/.venv/bin/qstock-mcp",
      "env": {
        "PG_DSN": "postgresql://user:pass@host:5432/qstock"
      }
    }
  }
}
```

把 `command` 换成实际绝对路径，并设置自己的数据库连接串。保存后重启 client。

> 这是 stdio MCP server，由 client 自动拉起，**不需要手动启动常驻服务**。GUI client 不一定继承 shell 环境变量，因此 `PG_DSN` 应写进配置的 `env`。

<details>
<summary>可选环境变量</summary>

| 变量 | 说明 |
| --- | --- |
| `MX_APIKEY` | 妙想开放平台 apikey，仅 `mx_query` / `mx_search` 需要 |
| `MX_DAILY_LIMIT` | 妙想工具每日配额，默认 20 |
| `MX_DAILY_LIMIT_MX_DATA` / `MX_DAILY_LIMIT_MX_SEARCH` | 单工具配额覆盖 |
| `MX_QUOTA_FILE` | 配额记录路径，默认 `~/.qstock-mcp/quota.json` |

</details>

### 3. 初始化并准备历史数据

在 MCP client 中调用一次 `init_database`：

```json
{"backfill_history": false}
```

这一步完成建表和轻量初始化，**不等于全市场历史数据已经就绪**。

随后按照 **[首次使用快速引导](docs/first-run.md)** 准备最近 90 个交易日的数据。指南包含空库/已有库路径、20 只冒烟、全量刷新和最终覆盖验收。

## 常用命令

`qstock-scheduler` 直调核心工具层，不经过 MCP 协议；MCP server 本身不会按点采集。

| 任务 | 命令 |
| --- | --- |
| 次晨全市场日线与指数补采 | `qstock-scheduler morning` |
| 收盘后盘面采集 | `qstock-scheduler close` |
| 个股日线补采 | `qstock-scheduler daily --codes 600519,000001 --date 20261023` |
| 最近 90 日个股整窗刷新 | `qstock-scheduler daily --all --trading-days 90 --force --throttle 1` |
| 指数日线回补 | `qstock-scheduler index --trading-days 90` |
| 申万板块历史回补 | `qstock-scheduler backfill --source sw --trading-days 90 --force` |
| 龙虎榜历史回补 | `qstock-scheduler lhb --trading-days 90` |
| 历史覆盖核验 | `qstock-scheduler coverage --trading-days 90` |

**[查看全部 14 个子命令及参数说明](docs/first-run.md#第二章调度-cli-命令手册)** · **[launchd 定时采集配置](docs/scheduler-launchd.md)**

建议交易日 **19:30 之后**执行每日复盘：此时涨停池、龙虎榜与当日日线通常已经就绪。全市场 `daily --all` 通常需要约 1.5–2 小时，请保留节流。

> 历史分析就绪不等于历史每日复盘完整可重做。全市场快照、实时板块快照、营业部累计榜等数据无法全部事后恢复；涨停池的历史深度也有限。日常采集与告警不能由事后补录替代。

## 数据源与可靠性

| 数据面 | 主要承载 |
| --- | --- |
| 个股日线 | 腾讯 → 新浪 → baostock |
| 全市场快照 | 腾讯批量报价 → 腾讯 rank → 新浪 |
| 指数 | 腾讯 → baostock / 新浪（按日线或快照能力区分） |
| 申万板块 | 申万官网；故障时复盘可降级到库内自算 |
| 龙虎榜 | 单日为交易所官方源；深圳历史同花顺主采，校验通过即完整，失败才由官方补缺并保留完整官方日；其他市场可显式后备；新浪用于互补对拍 |
| 涨停池 | 同花顺涨停揭秘，带低频纪律与封禁探测 |

- **不伪造数据**：失败明确报错，空数据与连接故障分开表达。
- **可重复执行**：业务键幂等写入，采集动作进入 append-only 台账。
- **结果机器可读**：stdout 输出结构化 JSON，日志与进度走 stderr。
- **质量可核验**：支持逐源对拍、指数覆盖检查与历史窗口统一覆盖核验。

## 测试

日常变更按 [分层测试门禁](docs/agents/testing.md) 选择 `docs` 或 `impacted`，不机械执行全量。阶段性/nightly 回归命令为：

```bash
.venv/bin/pytest tests/
.venv/bin/pytest skills/indicator-tools/tests/
```

自动化测试使用 fake 适配器与 PostgreSQL 测试库；PG 不可达时自动跳过，真实网络抓取不进入自动化测试。真实 live 仅执行门禁指南定义的隔离抽样，不要求真实全市场流程。

## 文档导航

- **[首次使用与 CLI 手册](docs/first-run.md)**：90 日数据准备、全部调度命令
- **[项目知识图谱](docs/knowledge-graph.md)**：架构、实体、关键流程与修改入口
- **专项 CLI 手册**：[daily](docs/daily-source-cli.md) · [index](docs/index-source-cli.md) · [sw-boards](docs/sw-boards-cli.md) · [lhb](docs/lhb-source-cli.md) · [limitpool](docs/limitpool-cli.md) · [snapshot](docs/snapshot-source-cli.md) · [stock-list](docs/stock-list-cli.md) · [parity](docs/parity-cli.md)
- **[领域词汇](CONTEXT.md)**：统一术语与数据口径
- **[架构决策记录](docs/adr/)**：关键设计选择及其原因
- **[Agent 协作约定](docs/agents/)**：Issue 流程与 triage 标签
