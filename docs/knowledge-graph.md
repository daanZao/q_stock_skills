# 项目知识图谱

面向人类与 AI agent 的 5 分钟全图认知。术语遵循 `CONTEXT.md`（抓取/落库/透传/自愈/回溯/五个能力面）。
每条论断尽量标注来源（`path:line`）。设计决策的"为什么"在 `docs/adr/0001-0007`，本图只讲"是什么、在哪、和谁相连"。

## 总览图

```mermaid
flowchart LR
    subgraph skills["agent skill 侧（不直连数据库）"]
        IT["indicator-tools<br/>工具函数 skill<br/>stdin/stdout 管道"]
        SEPA["sepa<br/>方法论 skill"]
        DR["daily-review<br/>每日复盘编排 skill"]
        LUA["limit-up-analysis<br/>涨停/连板分析编排 skill"]
    end

    subgraph mcp["MCP 数据服务（src/qstock_mcp，stdio）"]
        direction TB
        SRV["server.py<br/>14 工具薄包装"]
        subgraph faces["五个能力面（tools_* 核心层 = 测试接缝）"]
            INIT["tools_init<br/>init_database 轻量初始化/全量回溯"]
            DAILY["tools_daily<br/>fetch_daily / query_daily 自愈"]
            SNAP["tools_snapshot<br/>fetch/query_market_snapshot"]
            BOARD["tools_board<br/>fetch_board_snapshot / query_board_data"]
            FUND["tools_fundamentals<br/>get_fundamentals 透传"]
            CONC["tools_conclusions<br/>save/query_conclusions"]
            MX["tools_mx（妙想接入，跨三面）<br/>mx_query=proxy 透传 /<br/>mx_search=抓取+落库 /<br/>query_news=查询"]
        end
        CHAIN["fetch_chain<br/>fallback 编排 + 重试"]
        GAPS["gaps<br/>头尾缺口分段"]
        REPO["repository<br/>业务键 upsert / 查询"]
        DB["db.py<br/>PG_DSN 连接 + 幂等建表"]
        MXC["mx_client<br/>妙想网关客户端（懒加载）"]
        MXQ["mx_quota<br/>配额 ledger（skill×日）"]
    end

    subgraph src["数据源适配层 adapters/（懒加载真实库）"]
        EF["efinance（第 1 位）"]
        AK["akshare（第 2 位）"]
        BS["baostock（第 3 位兜底）"]
    end

    subgraph pg["PostgreSQL 独立新库（sql/001-011，15 张表）"]
        T1["stock_daily / index_daily / stock_list"]
        T2["market_snapshot"]
        T3["盘面表：market_indices / market_boards /<br/>zt_pool / strong_stocks / lhb_*×5"]
        T4["conclusions（通用结论表，jsonb payload）"]
        T5["news_items（资讯表，mx-search 落库）"]
    end

    AGENT["外部 AI agent（stdio 客户端）"] --> SRV
    SRV --> faces
    faces --> CHAIN
    DAILY & INIT --> GAPS
    CHAIN -->|"fallback: efinance→akshare→baostock<br/>（基本面链 akshare→efinance→baostock）"| src
    faces --> REPO --> DB --> pg
    FUND -.->|"透传：不规格化、不落库"| CHAIN
    MX -.->|"mx_query 透传 / mx_search 抓取<br/>不进 fallback 链（ADR 0005）"| MXC
    MXC --> MXQ
    DAILY -->|"query_daily JSON 输出<br/>（跨仓库稳定契约，ADR 0001）"| IT
    SEPA -->|"管道消费指标 + get_fundamentals"| IT
    DR & LUA -->|"corr / vol_price / promote / deviation 指标管道"| IT
    DR -->|"数据就绪检查/读数"| BOARD & SNAP
    SEPA -->|"mx_query 妙想验证"| MX
    DR -->|"mx_search / query_news 取资讯"| MX
    DR -->|"涨停/连板子模块（close 模式）"| LUA
    SEPA & DR & LUA -->|"save_conclusion 结论 upsert"| CONC
    CONC --> T4
```

## 实体清单

### MCP server 入口与公共层

- **`server.py`**：stdio MCP server，14 个工具只做薄包装，核心逻辑全在 `tools_*` 模块（`src/qstock_mcp/server.py:1-5`）；数据库连接按工具调用建立，启动不依赖 PG 可达（`server.py:4`）。入口 `qstock-mcp = qstock_mcp.server:main`（`pyproject.toml:17`）。
- **`output.py`**：统一错误输出契约——`status:"error"` + 工具名 + 参数回显 + 原因，工具层不抛异常（`output.py:4-6`、`db.py:31-41`）。
- **`dates.py`**：日期统一 `yyyymmdd`（接受 `yyyy-mm-dd`）；`days` 按交易日≈自然日 7/5 加 buffer 换算区间，宁多抓不少抓（`dates.py:14-50`）。
- **`db.py`**：连接串只从 `PG_DSN` 环境变量读（ADR 0002）；`ensure_schema` 幂等执行 `sql/*.sql` 全部 DDL（`db.py:21-28`、`db.py:44-54`）。

### 五个能力面（核心层 = 第一测试接缝，测试注入 fake 适配器 + 真实 PG 测试库）

- **init — `tools_init.py`**：轻量初始化 = 幂等建表 + 股票清单 + 全市场快照 + 7 个主要指数全历史日线，三部分独立成败，输出 `parts`/`failed_parts`，仅全败才整体 `error`（`tools_init.py:1-13`、`tools_init.py:41-49`、ADR 0004）。`backfill_history=True` 显式开启全量回溯：以 `stock_list` 为标的来源、固定 qfq、复用日线头尾自愈、单股失败不中断（`tools_init.py:117-165`）。`query_index_daily` 为 index_daily 纯读查询（issue #33/T1）：index_code 限 7 个主要指数（白名单校验，未知代码 error 并列可选项），日期闭区间 + limit 取最近 N 行（缺省 120）升序返回，空结果 `count:0` 非错误，不触发补抓（`tools_init.py:222-287`）。**能力面归统一查询**（`CONTEXT.md`），实现暂驻 tools_init：与写入方 `_init_index_daily` 同模块共享 `MAJOR_INDICES` 白名单/指数名，迁往 tools_daily 会与 tools_init→tools_daily 的 `ensure_coverage` 依赖成环。
- **fetch/query 日线 — `tools_daily.py`**：`ensure_coverage` 只补请求区间头尾缺口（中段缺口视为停牌），是全量回溯与查询自愈共用的公共能力（`tools_daily.py:24-48`）；`fetch_daily` 与 `query_daily` 共用 `_run` 骨架（参数解析→连接→自愈），前者报告 upsert 行数，后者返回完整区间行（`tools_daily.py:67-147`）。
- **fetch/query 快照 — `tools_snapshot.py`**：全市场快照单次调用约 5000+ 只；交易日优先级：API 报告 > 传入参数 > 当天（`tools_snapshot.py:51-56`）。三态错误契约首个落地（issue #46，ADR 0007）：全源失败按逐源 `failure_kind` 聚合 `error_kind`——任一抓取源连接级故障即 `upstream_unreachable`（带 `retry_advice`），否则 `fetch_failed`；by-design 不支持的源不参与判定；上游正常响应空结果为 `no_data_for_date`（status ok + `empty_kind` + note，非失败）。
- **fetch/query 盘面 — `tools_board.py`**：五 section（indices/boards/zt_pool/strong_stocks/lhb）独立 fallback、独立成败，仅全部失败才整体 `error`；盘中 lhb 空数据返回 `rows:0 + note`（盘后发布，非失败）；lhb 子项部分失败落已得数据并报 `partial_error`（`tools_board.py:30-32`、`tools_board.py:88-122`）。
- **proxy — `tools_fundamentals.py`**：基本面透传，`{section: 原始记录}` 键名保留上游、仅 JSON 安全化，不规格化、不连数据库、不落库；全失败 `status:"error"` 绝不伪造（`tools_fundamentals.py:1-8`）。
- **conclusions — `tools_conclusions.py`**：结论读写。写入校验日期与 payload 可 JSON 序列化；同业务键重复写为 upsert，`outcome` 报告 `inserted/updated`（`tools_conclusions.py:21-59`、ADR 0003）。
- **妙想接入 — `tools_mx.py`**：三工具共享 `_mx_exchange` 骨架（配额先检、触顶不调上游、上游触达即记账、code!=0 走统一 error 契约）。`mx_query` 妙想 mx-data 透传（同 proxy 语义：不规格化、不落库、不进 fallback 链）；`mx_search` 资讯搜索并落库 `news_items`（响应内按 news_code 去重、业务键幂等 upsert，subject 缺省 `market`/`_market`）；`query_news` 库内资讯纯读（subject + 发布时间范围过滤、倒序、limit，空结果 `count:0` 非错误，不调上游不计配额）（ADR 0005、`tools_mx.py:60-116`、`tools_mx.py:142-180`、`tools_mx.py:183-230`）。

### 编排与落库

- **`fetch_chain.py`**：fallback 编排核心。每源最多重试 2 次（3 次尝试）；空结果视为成功（停牌/无交易日）不触发 fallback；全失败抛 `AllSourcesFailed` 携带 `attempted_sources`（每源尝试次数、错误与 `failure_kind` 故障分类——unsupported/connection/other，ADR 0007）（`fetch_chain.py:22-52`）。只依赖适配器协议，不感知具体第三方库（`fetch_chain.py:1-9`）。
- **`gaps.py`**：头尾缺口规则——库内已有日期集合的最早/最晚之外才补抓，最多两段（`gaps.py:10-21`）。
- **`repository.py`**：全部 upsert 按业务键幂等。`stock_daily` 键 `(stock_code, trade_date, adj)`、`market_snapshot` 键 `(trade_date, stock_code)`（`repository.py:26-36`、`repository.py:87-98`）；盘面表注册表 `BOARD_SECTION_TABLES`（section→表/日期列/业务键/列集）与 `BOARD_QUERY_TABLES`（可查询表白名单，表名列名只来自内部常量，不接外部输入）（`repository.py:274-290`、`repository.py:330-343`）；`upsert_board_rows` 写入前按业务键去重（last-wins，上游脏数据防御——东财板块同代码重名、yyb 同席位多排名行实测存在），丢弃数记 log.warning。结论 upsert 用 CTE 预查业务键判定 inserted/updated（`repository.py:377-414`）。盘面表无 `source` 列（本项目 DDL 决策，`repository.py:271-273`）。资讯表 `news_items` 业务键 `(news_code, subject_type, subject_code)`（同一资讯可挂多主体），上游缺条目 id 时标题+发布时间复合兜底；`publish_time` 按东八区落 timestamptz；`select_news_items` 发布时间倒序 + 范围过滤 + limit，不返回内部列与 `raw`（`repository.py:445-477`、`repository.py:497-516`、`repository.py:556-605`）。

### 数据源适配层

- **`adapters/base.py`**：协议接缝。六个 Protocol（Daily/Snapshot/Board/Fundamentals/List/IndexDaily）合成 `DataAdapter`；失败一律抛 `FetchError`，返回空列表不是错误（`base.py:174-260`）。`FetchError(kind="unsupported")` 显式标注 by-design 不支持（不算抓取故障）；`fetch_failure_kind` 沿异常链按类名判定连接级故障（connection/other，ADR 0007 判定内核）。`BAR_FIELDS` 等列集是行 dict 与落库列的共同词汇（`base.py:20-171`）。北交所代码（4/8/9 开头）`is_bse_code` 明确拒绝（`base.py:263-265`）；`json_safe` 做透传序列化（NaN/numpy/日期→JSON 安全值，不改键名）（`base.py:268-294`）。
- **三个真实适配器**：懒加载第三方库，未安装报明确 `FetchError`（`pyproject.toml:13-14`）。日线/快照/清单链顺序 efinance→akshare→baostock（`adapters/__init__.py:38-44`）；基本面链按数据丰富度排序 akshare→efinance→baostock（`adapters/__init__.py:47-57`）。能力差异：efinance 盘面仅支持 indices 且无干净指数历史接口；akshare 五 section 全支持（东财接口）；baostock 无全市场快照、无振幅/换手列（置 null 不伪造）（`efinance_adapter.py:1-2`、`akshare_adapter.py:1-5`、`baostock_adapter.py:1-7`）。
- **`_eastmoney.py` / `_board_em.py`**：东财中文列→落库行 dict 的纯函数映射，不 import 第三方库，测试无需触网；`-`/空/nan 统一映射为 None（`_eastmoney.py:1-6`、`_board_em.py:1-8`）。金额统一规约为万元：元值经 `_wan`、中文金额串（"X亿"/"X万"/无单位按万元）经 `_money_cn`（ADR 0006）。
- **妙想不经适配层**：`mx_client.py` 是独立 HTTP 客户端（懒加载、`MX_APIKEY` 缺失报明确 `MXError`、query/search 两方法各对一个 endpoint，`mx_client.py:20-56`）；`mx_quota.py` 是本地 JSON 配额 ledger（skill×日期计数、默认 20 次/日、环境变量 `MX_DAILY_LIMIT*`/`MX_QUOTA_FILE` 覆盖、时钟与路径可注入，`mx_quota.py:15-54`）。两者只服务 `tools_mx`，不进 fallback 链（ADR 0005）。

### PostgreSQL 表（`sql/001-011`，独立新库，ADR 0002）

- 日线类：`stock_daily`（带 `adj` 与 `source` 列）、`index_daily`（指数与个股分表——`000001` 代码冲突，指数无复权）、`stock_list`（主键 `stock_code`，全量回溯标的来源）（`sql/001`、`sql/010`、`sql/009`、ADR 0004）。
- 快照类：`market_snapshot`（`sql/002`）。
- 盘面类：`market_indices`/`market_boards`/`zt_pool`/`strong_stocks` + lhb 五表（`lhb_basic`/`lhb_stock_detail`/`lhb_stock_statistic`/`lhb_yyb_capital`/`lhb_yyb_most`；`lhb_basic` 业务键 `(trade_date, stock_code, lhb_reason)`——同股当日多榜单按原因各一行；`lhb_stock_detail` 无业务唯一键，仅随父表存在；yyb 两表上游无卖出/净额等列、恒 null 不伪造，`lhb_yyb_most.buy_amount` 口径为"合计动用资金"非纯买入）（`sql/003-007`、`repository.py:330-331`）。
- 金额口径：全库金额列统一万元（成交额/市值/龙虎榜金额等），换算在适配器映射层（`_wan`/`_money_cn`）；基本面透传除外（ADR 0006）。
- `conclusions`：业务唯一键 `(subject_type, subject_code, trade_date, conclusion_type)` 在 schema 层钉死，`payload` 为 jsonb、结构由写入方 skill 自报、server 不校验（`sql/008_conclusions.sql:3-17`、ADR 0003）。
- `news_items`：资讯表（mx-search 落库，ADR 0005）。业务唯一键 `(news_code, subject_type, subject_code)`；列含资讯类型/标题/正文截断/`publish_time`（timestamptz）/来源/链接（可空，研报无原文链接）/研报机构评级（可空）/`raw` jsonb 原文（`sql/011_news_items.sql:1-24`）。

### agent skill（`skills/`，第二测试接缝：脚本 stdin/stdout）

- **indicator-tools（工具函数 skill）**：`scripts/indicators.py` 纯标准库，stdin 吃 `query_daily` 输出 JSON（`corr`/`promote`/`deviation` 例外，为专用输入）、stdout 吐指标 JSON，不访问数据库、现算现用不物化（ADR 0001）。指标目录：MA/MACD/BOLL/RSI/diff1/diff2/rolling_max/rolling_min/maxdd/chg/corr（多标的相关性矩阵）/vol_price（量价判定）/promote（晋级率与连板梯队）/deviation（个股对基准指数的偏离值窗口累计，严重异动监控口径，新股前 5 日经 `--skip-leading` 剔除），边界规则（null 头部）随输出 `boundary_rule` 回显。退出码 0=ok/insufficient_data（数据不足非错误）、1=输入错误、2=参数错误（`skills/indicator-tools/SKILL.md:20-49`）。测试在 `skills/indicator-tools/tests/`。
- **sepa（方法论 skill）**：判断逻辑为主体，无脚本。取数链路：`query_daily(days=400)` → 管道给 indicator-tools；`get_fundamentals` 取基本面；指数基线不可经 MCP 获得时用 `query_board_data("market_indices")` 快照降级并标注。`count<250` 或数据缺失即报告缺口、不硬算、不编造。结论可经 `save_conclusion(..., conclusion_type="sepa.full")` 落库。妙想验证小节：三源选股后 `mx_query` 抽样复核财务指标（宽问句省配额、单季/累计口径换算、未披露期静默回退须先校验披露状态、按 `secuCode` 过滤港股），验证结论落 `sepa.mx_verified`（`skills/sepa/SKILL.md:12-26`、`skills/sepa/SKILL.md:84-110`）。
- **daily-review（每日复盘编排 skill）**：轻脚本重编排，agent 手写结论 JSON 与 markdown 报告。流程：就绪检查（核心表缺则先补抓；指数历史经 `query_index_daily` 逐指数查 60 根；可选 `mx_search` 搜资讯落库，配额触顶跳过入 `data_gaps`）→ 读数（含 `query_news` 读近期资讯、market_boards 逐日历史供板块 5/10 日涨跌、mx_query 批量查前 75 概念归属）→ 涨停/连板子模块（close 模式调 limit-up-analysis）→ corr/vol_price/deviation 指标管道（连板股偏离值累计对照交易所严重异动阈值预警）→ 报告落盘 `skills/daily-review/reports/`（资讯引用约定：标题+发布时间+来源，链接可空兜底）→ `save_conclusion(subject_type="market", subject_code="_market", conclusion_type="daily_review.<mode>")`（payload 必含 `report_path`）→ 落库失败即中止，不算复盘完成。报告骨架：总体立场（≤5 行摘要）+ 六节（指数表现 / 板块轮动 / 涨停连板与龙虎榜 / 成交额前 75 / 归因与量价复盘 / 风险提示仅增量），数据点全报告唯一出处。技能不内置调度，收盘后定时执行的现成 launchd/cron 配置见 SKILL.md 末节（`skills/daily-review/SKILL.md:17-45`、`skills/daily-review/SKILL.md:60-75`）。
- **limit-up-analysis（涨停/连板分析编排 skill）**：轻脚本重编排，可单独调用、也被 daily-review（close 模式）编排调用。晋级率与连板梯队只认 indicator-tools `promote` 管道输出（不手算）；归因三维（板块聚类/大小票/业绩题材粗分）与龙头点评由 agent 基于盘面/快照/资讯生成。不写独立报告文件：结论落库 `limit_up.close`（`summary` 供 daily-review 原样引用），分析文本在对话内呈现；落库失败即中止（`skills/limit-up-analysis/SKILL.md:18-44`、`skills/limit-up-analysis/SKILL.md:58-77`）。

### 测试与协作

- **`tests/`**：`conftest.py` 的 `pg_test` fixture 连真实 PG 测试库 `qstock_test`（不可达自动 skip，`QSTOCK_TEST_PG_DSN`/`QSTOCK_TEST_MAINT_DSN` 可覆盖）；清库走 `reset_test_db`——DROP SCHEMA 前先 `pg_terminate_backend` 清掉本库残留会话，上个用例断言失败泄漏的连接不会阻塞后续 fixture（issue #30）（`tests/conftest.py:27-58`）；`fakes.py` 的 fake 适配器与真实适配器实现同一协议，另含 `FakeMxClient` 与 mx 实测响应 fixture。测试直接打 `tools_*` 核心层，不经 MCP 协议层；mx 三工具的接缝在 `test_mx_tools.py` / `test_mx_search.py` / `test_query_news.py` / `test_mx_quota.py`。
- **协作面**：issue 走 GitHub Issues（`gh` CLI），triage 标签与 domain docs 消费约定见 `docs/agents/`（`AGENTS.md:1-13`）。`.agents/skills/` 下的协作 skill（来自 `skills-lock.json`）仅服务本仓库开发流程，不属于交付物。

## 关键流程

- **查询自愈（头尾缺口）**：`query_daily`/`fetch_daily` → `resolve_range` 解析区间 → `select_dates` 取库内已有日期 → `head_tail_gaps` 算头尾分段（最多两段，中段缺口=停牌）→ 逐段 `fetch_with_fallback` → `upsert_daily` → `select_daily` 返回完整区间，自愈分段在输出 `healed`/`segments` 中报告（`tools_daily.py:67-99`、`gaps.py:10-21`）。
- **fallback 与重试**：每种抓取（日线/快照/五 section/基本面/清单/指数日线）各有一个 `fetch_*_with_fallback`，共享同一 `_with_fallback` 内核：按链顺序尝试、每源 3 次尝试、空结果即成功、全失败抛 `AllSourcesFailed`（`fetch_chain.py:34-167`）。
- **section 级独立成败**：`fetch_board_snapshot` 与 `init_database` 同构——逐项独立 fallback、逐项成败进 `sections`/`parts`，仅全部失败才整体 `error`（`tools_board.py:108-122`、`tools_init.py:192-219`）。重试方式就是再调一次（幂等，ADR 0004）。
- **结论 upsert**：业务键在 schema 层钉死；CTE 预查判定 `inserted/updated`；payload 不校验，结论类型（`daily_review.close`、`sepa.full` 等）的字段约定由各 skill 文档承载（ADR 0003）。
- **mx 调用与资讯沉淀**：`mx_query`/`mx_search` 走 `_mx_exchange` 骨架——配额先检（触顶不调上游直接 error）→ 调上游 → 触达即记账 → 业务码判定（code!=0 走 error 契约）；`mx_search` 成功即按 news_code 去重并业务键 upsert `news_items`；`query_news` 纯读库（倒序 + limit），不经骨架、不计配额（`tools_mx.py:60-116`、`tools_mx.py:142-230`）。
- **skill 间数据流**：`query_daily` JSON（跨仓库稳定契约，ADR 0001）→ indicator-tools stdin → 指标 JSON → 方法论 skill（sepa）判断 → `save_conclusion`；编排 skill 同样经管道消费指标（daily-review 用 corr/vol_price/deviation，limit-up-analysis 用 promote，专用输入按各自契约组装）；daily-review 的涨停/连板章节取自 limit-up-analysis 落库的 `limit_up.close` 结论。

## 入口指引（想改 X 先看哪里）

- **改表结构/schema**：`sql/00X_*.sql`（DDL 拥有权在本项目）→ `repository.py`（列集/业务键/注册表）→ `adapters/base.py` 的 `*_FIELDS` → 相关 ADR（0002 选型、0003 结论表、0004 init 表）。改完跑 `tests/test_repository.py` + 对应 fetch/query 测试。
- **改 `query_daily` 输出结构**：这是跨仓库稳定契约（ADR 0001）——`tools_daily.py` + `skills/indicator-tools/SKILL.md` 输入契约 + `skills/indicator-tools/tests/` 必须同步，变更需谨慎。
- **加数据源**：实现 `adapters/base.py` 的协议 → 新建 `adapters/<name>_adapter.py` → 调整 `adapters/__init__.py` 两条链的顺序 → `tests/fakes.py`/`test_adapters.py` 覆盖。
- **加盘面 section/表**：`sql/` 新 DDL → `repository.BOARD_SECTION_TABLES`/`BOARD_QUERY_TABLES` → `BoardAdapter` 协议与 `_board_em.py` 映射 → `tools_board.ALL_SECTIONS`。
- **加指标**：只动 `skills/indicator-tools/scripts/indicators.py` 与其测试，不改 MCP server（ADR 0001）；新增指标不得改变既有契约（`skills/indicator-tools/SKILL.md:95`）。
- **加结论类型**：零 schema 变更（ADR 0003），只在对应 skill 文档约定 payload 字段并调用 `save_conclusion`。
- **改涨停/连板分析**：编排逻辑与归因口径在 `skills/limit-up-analysis/SKILL.md`；晋级率/梯队数字只认 indicator-tools 的 `promote` 输出，改口径走"加指标"路径。每日复盘收盘后定时执行：技能不内置调度，现成 launchd/cron 配置见 `skills/daily-review/SKILL.md` 末节。
- **加 MCP 工具**：核心逻辑进 `tools_<面>.py`（可注入适配器、返回自描述 dict），`server.py` 只加薄包装；测试打核心层。
- **改妙想接入/资讯**：边界在 ADR 0005——`tools_mx.py`（三工具与 `_mx_exchange` 骨架）→ `mx_client.py`/`mx_quota.py`（客户端与配额 ledger）→ `sql/011_news_items.sql` 与 `repository.py` 资讯段；测试 `tests/test_mx_tools.py`/`test_mx_search.py`/`test_query_news.py`/`test_mx_quota.py`（fake 注入 + 真实 PG 测试库，不触网）；消费方守则在 `skills/sepa/SKILL.md`、`skills/daily-review/SKILL.md`。
- **改 fallback/重试策略**：`fetch_chain.py`（注意"最多 3 次"按尝试次数口径，`fetch_chain.py:122-124`）+ `tests/test_fetch_chain.py`；错误分类（failure_kind/三态契约）的判定标准与约束力在 ADR 0007，落地先例 `tests/test_snapshot_error_contract.py`。
