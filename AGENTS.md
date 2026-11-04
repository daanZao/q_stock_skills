# q_stock_skills

证券数据 MCP 服务 + agent skill 家族。核心交付物是 `src/qstock_mcp` 的 stdio MCP server（`server.py` 只做 18 个工具的薄包装，逻辑全在 `tools_*` 核心层），数据落 PostgreSQL 独立库，`skills/` 下是交付给外部 agent 的 skill（indicator-tools / sepa / daily-review / limit-up-analysis）。

## Commands

- 核心层测试：`.venv/bin/pytest tests/`（打 `tools_*` 层，fake 适配器 + 真实 PG 测试库，PG 不可达自动 skip）
- indicator-tools 脚本测试：`.venv/bin/pytest skills/indicator-tools/tests/`

## 数据源与性能纪律

- 方案设计必须评估数据源的可用性与持续服务能力，明确请求节奏、最短请求间隔、上游可承载 TPS、并发支持及上限。已有实测的指标用实测值；缺失项标为“待验证”，先采用串行、低频的保守参数，经限定样本验证后再放量。
- 实测记录须注明日期、接口、样本范围、请求条件与成败。单次或单轮结果只代表当次样本，不据此推断长期可用性或全市场性能。
- 开发时在对应数据源客户端或限流参数旁，以注释记录已验证的请求节奏、最短间隔、TPS/并发上限及验证日期、条件；实现参数与实测结论保持一致，未验证指标不得写成已验证结论。

## 数据契约

- 库内金额列统一为万元，成交量统一为股；baostock amount 在适配层由元除以 10000。复权影子模型只以 `backAdjustFactor` 为累计 B，`adjustFacto(r)` 不参与派生（ADR 0018）。

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
- **决策原因：`docs/adr/0001-0018`** — "为什么"只在 ADR；输出与 ADR 冲突时显式指出。

## 协作

### Spec 纠偏（结论被一手证据推翻）

按以下顺序处理；完成标准是所有受影响内容都有新归属或可见阻断，而不是只改主 spec：

1. **止损与定界**：暂停受影响实施；写清被推翻的命题、直接证据和样本边界，区分“已证实 / 已推翻 / 待验证”。旧实测数据不因解释错误而自动失效。
2. **建影响图**：从主 spec 追溯 map、研究、决策、下游 task，以及代码、DDL、迁移、验收和回滚。独立 bug 与不依赖旧结论的现役任务保留，不随父 map 一并废弃。
3. **隔离旧结论**：整体失效的报告在标题和正文标 `【已作废】`，issue 标 `【废弃的】` 并链接新入口；保留原文、评论和原始响应/缓存供审计，只禁止继续引用其结论。
4. **重做一手研究**：沿用旧研究的问题和证据定位，重新依据官方文档、当前源码和原始响应推导；新报告注明证据条件与待验证项，不继承旧报告结论。
5. **重锚 spec**：已有主 spec 直接修订；独立能力域新建 `Spec（待核实）`。每份研究关联一个主 spec；事实由 agent 查证，未拍板的选择写成实施阻断项，不静默选边。
6. **同步下游**：更新受影响 task 的公式、字段、迁移与验收；未决 spec 保持 `needs-triage`，并给下游加 `Blocked by`。涉及术语、决策原因或项目入口时同步 `CONTEXT.md`、ADR、`docs/knowledge-graph.md`。
7. **核验闭环**：全库搜索旧公式、字段映射、迁移口径和 issue 链接；逐字核对线上 issue 正文并检查本地 diff/status。代码受影响时跑覆盖该行为的测试；交付说明列出已纠正内容与剩余阻断。

### Issue tracker

智能体必须先关联一个 GitHub Issue 任务，再修改代码；没有任务时先创建 Issue，再进入实现。Issues are tracked in GitHub Issues for this repo (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` at the repo root plus `docs/adr/`; project map for fast onboarding at `docs/knowledge-graph.md`. See `docs/agents/domain.md`.
