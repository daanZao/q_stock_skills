# q_stock_skills

证券数据 MCP 服务 + agent skill 家族。核心交付物是 `src/qstock_mcp` 的 stdio MCP server（`server.py` 只做 18 个工具的薄包装，逻辑全在 `tools_*` 核心层），数据落 PostgreSQL 独立库，`skills/` 下是交付给外部 agent 的 skill（indicator-tools / sepa / daily-review / limit-up-analysis）。

## Commands

- 核心层测试：`.venv/bin/pytest tests/`（打 `tools_*` 层，fake 适配器 + 真实 PG 测试库，PG 不可达自动 skip）
- indicator-tools 脚本测试：`.venv/bin/pytest skills/indicator-tools/tests/`

## 数据源与性能纪律

- 方案设计必须评估数据源的可用性与持续服务能力，明确请求节奏、最短请求间隔、上游可承载 TPS、并发支持及上限。已有实测的指标用实测值；缺失项标为“待验证”，先采用串行、低频的保守参数，经限定样本验证后再放量。
- 实测记录须注明日期、接口、样本范围、请求条件与成败。单次或单轮结果只代表当次样本，不据此推断长期可用性或全市场性能。
- 开发时在对应数据源客户端或限流参数旁，以注释记录已验证的请求节奏、最短间隔、TPS/并发上限及验证日期、条件；实现参数与实测结论保持一致，未验证指标不得写成已验证结论。

## 数据库纪律

本项目必然涉及存量数据处理与历史回补。所有对 PG 业务库的写入都走**可重复、可还原的工件**，不徒手改库：

- **小变更** → 交付数据库变更脚本 + 操作手册（适用场景、执行命令、回滚方法）
- **大变更 / 批量回补** → 交付固定 CLI 入口 + 操作手册（同上）
- **测试触库** → 单次处理不超过 100 行；动手前先备好数据还原脚本，结束即还原，不留永久改动

**测试环境 `qstock_test`**：pytest 经维护库 `postgres` 自动创建、每个测试前清空表数据并保留预建 schema（`tests/conftest.py`，可用 `QSTOCK_TEST_PG_DSN` / `QSTOCK_TEST_MAINT_DSN` 覆盖）。CLI 工具与脚本的手工验证直接指向它：

```bash
PG_DSN=postgresql://localhost/qstock_test qstock-scheduler daily --codes 600519 --date 20261020
```

对 `qstock_test` 的操作豁免上面的 ≤100 行/还原纪律——该库与业务库隔离，可随时 drop 重建（pytest 会重新供给）。业务库纪律原文不变。

## 文档分工

- **结构与入口：`docs/knowledge-graph.md`** — 项目全图；"想改 X 先看哪里"覆盖改 schema / 加数据源 / 加指标 / 加 MCP 工具 / 改 fallback 等分支。改代码前先读。
- **词汇：`CONTEXT.md`** — 领域术语的唯一权威（五个能力面、轻量初始化/全量回溯、skill 家族等）。命名时用这里的词，不用 `_Avoid_` 列出的同义词。
- **决策原因：`docs/adr/0001-0006`** — "为什么"只在 ADR；输出与 ADR 冲突时显式指出。

## 协作

### Issue tracker

智能体必须先关联一个 GitHub Issue 任务，再修改代码；没有任务时先创建 Issue，再进入实现。Issues are tracked in GitHub Issues for this repo (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` at the repo root plus `docs/adr/`; project map for fast onboarding at `docs/knowledge-graph.md`. See `docs/agents/domain.md`.
