# 妙想（东方财富 Choice 妙想）skill / MCP 使用现状调研

日期：2026-10-14

**存放位置说明**：`docs/` 下现有 `adr/`（决策）、`agents/`（协作约定）与 `knowledge-graph.md`（结构地图），均无调研类笔记的目录约定；按任务建议新建 `docs/research/`，本文件为首篇。

## 调查方法

- 关键词全局搜索（两个仓库，ripgrep 默认排除 .git/.venv/node_modules）：`miaoxiang`、`妙想`、`mx_`、`MX_`、`eastmoney`、`dfcfs`、`emquant`、`choice`（含大小写不敏感）。`choice` 命中均为 LLM API 的 `choices` 字段，与 Choice 数据接口无关；`eastmoney` 命中为三源抓取适配器（efinance/akshare 底层走东财接口），非妙想。
- MCP 配置核查：`~/.claude.json`（各项目 `mcpServers` 均为空 `{}`，无任何妙想官方 MCP server 注册）、`~/.kimi-code/config.toml`（仅 `[mcp.client]` 超时设置，无 server 注册）、两仓库 `.claude/settings*.json`、`.kimi-code/`、`SKILLProject/`、`.agents/skills`。未发现 `.mcp.json`。未读取任何密钥文件；配置中未见 token 字段。
- skill 引用核查：逐个检查 `q_stock_skills/skills/` 四个 agent skill 与 `q_stock_analysis/SKILLProject/skills/` 四个 skill 的 SKILL.md。

## 现状清单

### 一、q_stock_skills：妙想经自建 qstock MCP server 接入（非妙想官方 MCP）

妙想能力以**自建 MCP server（qstock）内的三个工具**形式提供，底层是直连妙想开放平台 HTTP API 的独立客户端，**不是**妙想官方 skill/MCP server：

| MCP 工具 | 对应妙想 skill | 性质 | 核心实现 |
| --- | --- | --- | --- |
| `mx_query` | mx-data（金融数据自然语言查数） | proxy 面透传：不规格化、不落库、不进 fallback 链 | `src/qstock_mcp/tools_mx.py:119-139`，薄包装在 `src/qstock_mcp/server.py:130-140` |
| `mx_search` | mx-search（资讯搜索：新闻/公告/研报） | 抓取 + 规格化落库 `news_items` 表（业务键幂等 upsert） | `src/qstock_mcp/tools_mx.py:142-180`，`server.py:144-156` |
| `query_news` | —（库内资讯纯读，不调妙想） | query 面：按 subject + 时间范围读 news_items | `src/qstock_mcp/tools_mx.py:183-230` |

支撑模块：

- `src/qstock_mcp/mx_client.py`：妙想 HTTP 客户端（标准库 urllib，懒加载；端点 `https://mkapi2.dfcfs.com/finskillshub/api/claw/query` 与 `.../news-search`，见 `mx_client.py:15-16`；apikey 读环境变量 `MX_APIKEY`，缺失抛 `MXError`）。
- `src/qstock_mcp/mx_quota.py`：本地 JSON 配额 ledger（skill × 日期计数，默认 20 次/日，`MX_DAILY_LIMIT` / `MX_DAILY_LIMIT_MX_DATA` / `MX_DAILY_LIMIT_MX_SEARCH` / `MX_QUOTA_FILE` 环境变量覆盖；触顶不调上游）。
- 设计决策：`docs/adr/0005-miaoxiang-proxy-integration.md` —— mx-data 定位为"三源选股后的交互式验证源"，mx-search 落库复用；**mx-zixuan / mx-moni（账号操作类）明确不接入**。
- 词汇定义：`CONTEXT.md:23`（proxy 面含妙想 mx-data 网关）、`CONTEXT.md:35`（资讯经妙想 mx-search 单源抓取）。
- 测试：`tests/test_mx_tools.py` / `test_mx_search.py` / `test_query_news.py` / `test_mx_quota.py`，fake client 在 `tests/fakes.py:375-477`。
- 实测资产：`.scratch/mx-skills/`（mx-data / mx-search 调研与对比报告、原始响应、可复跑脚本 `run_mx_query.py`，ADR 0005 指明可作测试 fixture 模板；属本地产物）。
- 环境变量契约：`README.md:63-79`。

### 二、agent skill 对妙想工具的消费（q_stock_skills/skills/）

| skill | 用到的妙想相关 MCP 工具 | 引用位置 | 用途 |
| --- | --- | --- | --- |
| sepa（方法论） | `mx_query` + `save_conclusion` | `skills/sepa/SKILL.md:3, 82-110` | 三源选股结论的抽样复核；问句按库内口径组装、宽问句省配额、须校验披露状态、按 `secuCode` 过滤港股；验证结论落 `sepa.mx_verified` |
| daily-review（编排） | `mx_search`、`query_news`、`mx_query` | `skills/daily-review/SKILL.md:8, 26, 34, 68` | 可选资讯搜索落库（配额触顶跳过入 `data_gaps`）；读近期资讯；`mx_query` 批量查前 75 个股概念归属与行业分类补齐 |
| limit-up-analysis（编排） | `mx_search`、`query_news` | `skills/limit-up-analysis/SKILL.md:8, 55` | 涨停归因资讯：库内不足再搜（可选降级），配额触顶/未配置时跳过入 `data_gaps` |
| indicator-tools（工具函数） | 无 | — | 纯计算管道，不触妙想 |

### 三、q_stock_analysis：仅有规划，零接入

- `SKILLProject/skills/stock-data/SKILL.md:71`："可选增强：配置妙想 AI MCP 后可作为补充取数通道；未配置时全部功能照常可用（**当前未接入**）"。
- `openspec/changes/skill-decomposition/proposal.md:10`：`stock-data` skill 规划中"妙想 AI MCP 作为辅助数据源（实现期接入）"。
- `openspec/changes/skill-decomposition/design.md:90`（Open Questions）："妙想 AI MCP 的接入方式（端点/认证/工具面）——实现期由所有者提供"。
- `openspec/changes/skill-decomposition/specs/stock-data/spec.md:59`：配置外部 MCP 时作为补充取数通道的需求条款。
- 该 change 目录仍在 `openspec/changes/` 未归档（`archive/` 为空），即妙想接入在该仓库是**悬而未决的开放问题**。
- 两仓库各 skill 目录（`.agents/skills`、`.claude/skills`、`.kimi-code/skills`、`SKILLProject/skills`）中无任何妙想官方 skill 副本。

## 已配置/可用但未被使用的妙想能力

- **妙想官方 MCP server**：所有已查的 MCP client 配置（`~/.claude.json`、`~/.kimi-code/config.toml`、两仓库项目级配置）中均未注册任何妙想官方 server；当前唯一通路是自建 qstock server 内的 `mx_query`/`mx_search`。
- **mx-zixuan / mx-moni**：ADR 0005 明确判定为账号操作类、超出数据服务定位，不接入——属"评估后放弃"而非"未发现"。
- **mx-data 的已知盲区**（ADR 0005 记录的实测约束，影响拓展面）：无结构化参数、每日配额无用量回显需本地记账（code=113 触顶）、未披露期间静默回退旧值、响应有两种变体、`secuList` 实测不存在——这些决定了它只能做交互式抽查，不能批量对账。

## 结论与拓展空间观察

1. **现状集中度高**：妙想使用全部收敛在 q_stock_skills 的 `tools_mx` 一条链路（mx_client + mx_quota + 三工具），消费方是三个 agent skill；q_stock_analysis 一行代码级接入都没有，只有 openspec 里的规划条款。
2. **自建而非官方**：两仓库均未使用妙想官方 skill/MCP server。若"扩大妙想 skill 使用范围"指的是接妙想官方 MCP，那将是**从零开始的新通路**（需注册 server、管理凭据），与现有自建链路的配额 ledger、统一 error 契约、落库约定如何共存需先决策。
3. **顺势拓展方向**（在现有自建链路上加宽）：
   - mx-data 目前只服务 SEPA 抽样验证与 daily-review 概念/行业归属查询；ADR 0005 的"三源交互式验证源"定位可推广到其他方法论 skill。
   - mx-search 资讯落库 + `query_news` 已建好沉淀闭环，新 skill 复用资讯只需调 `query_news`，边际成本为零。
   - q_stock_analysis 的 `stock-data` skill 已预留妙想增强通道（spec 已写"未配置时照常可用"的降级语义），且其 openspec open question 可在了解 q_stock_skills 的 mx_client 实现后直接收口——两仓库的接入方式宜统一。
4. **配额是硬约束**：妙想各 skill 独立日配额（默认各 20 次/日）且上游不回显用量；任何扩大使用面的方案都应复用 `MxQuota` 记账语义，避免多通路绕过 ledger 导致 code=113 被动触顶。
