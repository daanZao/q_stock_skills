# q_stock_skills

证券数据 MCP 服务 + agent skill 家族。核心交付物是 `src/qstock_mcp` 的 stdio MCP server（`server.py` 只做 10 个工具的薄包装，逻辑全在 `tools_*` 核心层），数据落 PostgreSQL 独立库，`skills/` 下是交付给外部 agent 的 skill（indicator-tools / sepa / daily-review）。

## Commands

- 核心层测试：`.venv/bin/pytest tests/`（打 `tools_*` 层，fake 适配器 + 真实 PG 测试库，PG 不可达自动 skip）
- indicator-tools 脚本测试：`.venv/bin/pytest skills/indicator-tools/tests/`

## 文档分工

- **结构与入口：`docs/knowledge-graph.md`** — 项目全图；"想改 X 先看哪里"覆盖改 schema / 加数据源 / 加指标 / 加 MCP 工具 / 改 fallback 等分支。改代码前先读。
- **词汇：`CONTEXT.md`** — 领域术语的唯一权威（五个能力面、轻量初始化/全量回溯、skill 家族等）。命名时用这里的词，不用 `_Avoid_` 列出的同义词。
- **决策原因：`docs/adr/0001-0004`** — "为什么"只在 ADR；输出与 ADR 冲突时显式指出。

## 协作

### Issue tracker

Issues are tracked in GitHub Issues for this repo (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` at the repo root plus `docs/adr/`; project map for fast onboarding at `docs/knowledge-graph.md`. See `docs/agents/domain.md`.
