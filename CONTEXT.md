# q_stock_skills

面向通用 AI agent 的证券数据与分析能力项目的领域语言。本文件只是词汇表，不记录实现细节与设计决策——结构与入口在 `docs/knowledge-graph.md`，"为什么"在 `docs/adr/`。

## Language

### 项目定位

**MCP 数据服务**:
本项目的核心交付物：一个 MCP server，作为旧 B/S 分析系统 Server 端的替代品，直接向 AI agent 提供数据与分析结论持久化能力。由五个能力面组成（见下）。
_Avoid_: 后端、B/S 服务

### MCP 数据服务的五个能力面

**抓取（fetch）**:
执行 web 数据源接口获取证券数据，多源 fallback（按能力面定序；个股日线为 腾讯 → 新浪 → baostock、全市场快照为 腾讯批量报价 → 腾讯 rank 榜单 → 新浪直连，见 `docs/knowledge-graph.md`）。
_Avoid_: 爬虫

**抓取三态错误契约**:
抓取结果中"抓不到"与"没有"的机械区分（决策见 `docs/adr/0007`）：`upstream_unreachable`（连接级故障——请求未获得任何 HTTP 响应：连接重置/空响应/超时/DNS 失败；错误语义，携带重试建议）/ `no_data_for_date`（上游正常响应但该日期无数据，如非交易日、盘后未发布；正常语义非失败）/ `empty_ok`（查询条件合理的空集）。by-design 不支持某能力的源（`FetchError(kind="unsupported")`）不算抓取故障。
_Avoid_: 抓取失败（不加限定时）

**核心 section**:
`fetch_board_snapshot` 盘面 section 中必须可用的显式子集（indices/boards/zt_pool/strong_stocks；lhb 及未来新增 section 默认非核心，入核心集是被记录的决策）。任一核心 section 因 `upstream_unreachable` 失败 → 工具整体 error 并携带 per-section 明细；中途失败不回滚，可用 `sections` 参数分段幂等重试。
_Avoid_: 必需 section、关键 section

**规格化落库（ingest）**:
对基础证券数据做清洗/规格化，写入本地数据库。

**原始数据封装（proxy）**:
对 web 接口提供的非标准化数据做透传封装，不规格化、不落库。封装基本面数据（财务指标、估值等）与妙想 mx-data 自然语言查数网关（三源选股后的抽样验证源），供方法论 skill（如 SEPA 四要素）使用。
_Avoid_: 落库

**统一查询（query）**:
基于库内数据的统一数据获取接口。指数历史日线经 `query_index_daily` 纯读（主要指数白名单，不触发补抓）；该工具能力面归本面，实现暂驻 init 面模块（与写入方同模块、避循环依赖，归属说明见 `docs/knowledge-graph.md`）。

**结论存储（conclusions）**:
分析结论的读写接口。结论以 JSON 保存在一张通用结论表中，结论类型由写入方 skill 自报。

### 资讯

**资讯（news）**:
个股或大盘的新闻、公告、研报等。横跨五个能力面中的抓取、规格化落库与统一查询：经妙想 mx-search 抓取并规格化落库（标注标准代码与资讯发布时间），经统一查询按标准代码 + 时间范围读取复用。妙想为单源抓取，不进三源 fallback 链。

### init 能力面词汇

**轻量初始化**:
`init_database` 默认执行的初始化路径：幂等建表 + 股票清单 + 全市场快照 + 主要指数日线，各部分独立成败报告，重复调用幂等。
_Avoid_: 全量初始化

**全量回溯**:
由 `backfill_history=True` 显式开启的全市场个股历史日线补全（数千只 × 全历史），复用日线头尾自愈，单股失败不中断；绝不默认执行。
_Avoid_: 回填、补数据（不加限定时）

### 数据口径

**金额单位（万元）**:
库内所有金额列（成交额、市值、龙虎榜买卖净额等）统一以万元存储，换算收敛在适配器映射层（决策见 `docs/adr/0006`）；基本面透传不规格化、保持上游原单位。
_Avoid_: 元（库内金额口径）

**成交量单位（股）**:
库内所有 volume 列统一以股存储，换算收敛在适配器映射层（东财系手 ×100，baostock 原生为股透传；决策见 `docs/adr/0008`）；存量迁移为一次性运维动作（机检互证后分批执行）。
_Avoid_: 手（库内成交量口径）

**成分字典**:
板块成分归属的库内真源表（stock × 板块 × 生效日期）。现内容为申万行业分类变更史（`qstock-scheduler sw-dict` 在线刷新，issue #118：申万官网分类 xls 直链下载、以文件为准清空重载 'sw' 体系，含北交所归属；手工导入入口 import_board_classification 仍保留）；预留 source_system 体系列（'sw'/'em' 等）以扩展东财概念成分。point-in-time 归属 = 生效日期 ≤ 当日的最新行。
_Avoid_: 板块映射表

**自算板块行情**:
成分字典 × 库内个股日线在**查询时实时聚合**的板块行情（当日成交额加权涨跌幅、涨跌家数、成交额合计），不加列、不物化，任意历史日期可算。东财 boards 缺失时每日复盘板块节的降级来源，降级日整节切换并标注口径。
_Avoid_: 板块回补数据

**自算强势股**:
strong_stocks 的库内派生产出口径（issue #120，ADR 0012）：stock_daily 60 日新高 ∪ zt_pool 近 10 日涨停 ≥2 次，并集入选，`reason` 列为规则文本（"60日新高"/"多次涨停（近10日 N 次）"）。无外部强势股池源（东财口径本身为派生定义），台账来源记 `selfcalc`。自算输入缺失（stock_daily 当日无数据）按 no_data_for_date 正常语义跳过，非故障。
_Avoid_: 强势股回补、强势股抓取

**采集台账**:
一切写库动作（fetch/init/调度/手动补抓）的 append-only 审计表 `collection_log`：每次采集动作落一行（交易日/section/status/来源/行数/error_kind/起止时间），全历史保留。status 词表 = 采集结果三态 ok/empty/error + 编排第四态 planned_absent（计划缺席）。就绪检查先读台账：无行=未采集、empty=上游确认无数据、error=采集失败、planned_absent=计划缺席（迁移占位，非故障非未采集），表述不得混用。低频纪律的"每日一次"守卫也读台账（当日已 ok/empty 即跳过上游，issue #120）。
_Avoid_: 采集日志、抓取记录

**计划缺席（planned_absent）**:
采集编排第四态（决策见 #102，实施 #113）：已定方向待接线的面以该态占位——不抓取、零落库、不计失败、不进异常报告、不触发每日复盘中止线；收盘后批每日为其落一条 planned_absent 占位台账，历史模式就绪归因由此区分"计划缺席"与"未采集"。永久弃用面不占位，直接从采集清单删除（如概念维度资金流字段）。核心 section 集维持原样，真故障（error）仍触发中止。
_Avoid_: 跳过 section、禁用 section

**表承载契约**:
每张表的写入方与读取方在其 DDL 文件头注释成文（`sql/NNN_*.sql` 头注"承载：写 X；读 Y"）；新增表或新写入路径必须同步头注。

**停牌占位行**:
`query_daily` 对停牌交易日输出的占位行：`tradestatus=0` 为权威标注（其余源为 null），无标注时启发式兜底（OHLC 全等且 volume/change_percent 为 null），行带 `suspended`/`suspended_basis` 字段。消费方（indicator-tools 等）按"无该交易日数据"剔除，不填充不伪造，剔除明细记 `excluded_suspended`（决策见 `docs/adr/0009`）。
_Avoid_: 停牌零值行

### 调度

**收盘后批**:
`qstock-scheduler close` 执行的交易日收盘后采集：盘面五 section（boards/zt_pool/strong_stocks/lhb/indices）快照，直调 tools_* 核心层、不经 MCP 协议层，逐 section 独立成败并落采集台账。调度安装（launchd）为用户侧动作，见 `docs/scheduler-launchd.md`。
_Avoid_: 定时任务（不加限定时）

**次晨批**:
`qstock-scheduler morning` 执行的开盘前采集（目标日恒为前一交易日）：baostock 一把抓全市场日线（含 tradestatus 权威停牌列、turn 换手率，volume 股 / amount 万元口径）+ 7 大指数尾行缺失即补，逐部分独立成败并落采集台账。baostock 当日 bar 晚间才就绪，次晨批不用于当日。
_Avoid_: 早盘补抓

**板块回补**:
`qstock-scheduler backfill` 执行的板块历史补数入 `market_boards`，按源分两种：`--source em`（缺省）为东财 push2his 板块 K 线回补行业+概念板块（默认窗口 = 前一交易日前推约 3 个月）；`--source sw`（issue #118，spec #111）为申万官网 trend 指数全历史回补申万双级口径行（默认窗口 = 19990101 起、历史深至 1999，解除 5/10 日涨跌"自落库日起积累"限制）。回补行带 source 标注（'push2his'/'swsresearch'）与实时采集行（NULL）区分，业务键幂等可重跑；CLI 专用路径，不经 fetch 工具的历史日期防线。收盘后批东财健康时附带概念成分积累入成分字典 'em' 体系（随 close 执行，独立成败不拖垮主批；申万承接后当日无概念板块，该步空转）。

**个股日线入口**:
`qstock-scheduler daily` 执行的个股日线固定入口：默认经腾讯 → 新浪 → baostock 的 fallback 链抓取并按业务键幂等写入 `stock_daily`；`--check` 只做三源对拍、不写库。北交所旧码 43x/83x/87x 请求前归一化到 920 新码。手册见 `docs/daily-source-cli.md`。
_Avoid_: 日线脚本、补数脚本

**指数日线入口**:
`qstock-scheduler index` 执行的指数日线固定入口：默认经腾讯 → baostock 的 fallback 链抓取区间并按业务键幂等写入 `index_daily`，逐指数独立成败；用于断档回填（如科创50 自 2026-10-13 起缺口，腾讯 K 线为首个覆盖 000688 的日线源）与收盘后当晚 T+0 当日行补采；`--verify` 纯读覆盖核验（对照交易日历列缺口），不写库。缺省代码 = 7 大指数白名单。盘中（交易时段）当日行 defer 不落库（issue #78）。手册见 `docs/index-source-cli.md`。
_Avoid_: 指数脚本、回填脚本

**对拍**:
换源/迁移后的数据质量验证动作：逐源直取同一交易日数据并做字段级比较，不落库；`daily --check` 入口。后复权（hfq）基准因源而异，只比成交量/成交额/换手率。
_Avoid_: 校验、比对（不加限定时）
_Avoid_: 回填、补数据（不加限定时）

**复权回落**:
腾讯 K 线请求 qfq/hfq 但上游只给 `day` 键时按"无复权事件"返回 day 数据的语义（ADR 0010）；响应无法区分"无复权事件"与"上游不提供调整序列"，故回落必须可见：适配器 `adjust_fallback` 标记 + WARNING 日志 + fetch/query 响应 note + 采集台账 detail「复权回落=day」+ 对拍源条目 note 五处同步标注，不静默落库。
_Avoid_: 复权降级、静默回落

### Skill 家族（继承自旧项目词汇）

**Agent skill**:
独立的、自包含的 agent 能力包（SKILL.md + 可选脚本），由外部 AI agent 在会话中加载使用。本项目交付的 skill 均属此类，供通用 agent 使用，不绑定特定仓库。
_Avoid_: skill（不加限定时）

**协作 skill**:
本仓库 `.agents/skills/` 下的纯 Markdown 流程指引，仅服务于本仓库的开发协作过程。
_Avoid_: agent skill

**工具函数 skill**:
基础证券计算能力的 agent skill，内含 Python 脚本。指标计算（MA/MACD/BOLL/RSI 等）与序列数学工具（一阶/二阶导数）。经管道组合消费统一查询吐出的 JSON（stdin 进 stdout 出），不直接访问数据库；现算现用，结果不物化。
_Avoid_: 指标库

**方法论 skill**:
表达一套完整分析/选股方法（如 SEPA）的 agent skill，以判断逻辑与框架指引为主体，调用工具函数 skill 获取计算结果。
_Avoid_: 策略 skill、分析框架

**编排 skill**:
跨工具与子技能编排一个完整分析工作流的 agent skill（如每日复盘、涨停/连板分析），轻脚本重编排：编排逻辑在 SKILL.md，结论由 agent 生成，计算仍经工具函数 skill 管道，结论经结论存储落库。

### 分析工作流

**每日复盘**:
收盘后（午市或全天）基于当日盘面数据做的市场级总结，结论经结论存储持久化。技能不内置调度，由用户手动触发 agent 执行；附调度配置说明（launchd/cron），供用户自行安装收盘后定时任务。核心数据抓取故障（三态契约的 upstream_unreachable / fetch_failed）即中止复盘、落**异常报告**（`files/daily_review_<date>_anomaly.md`），不落库结论；降级规则仅适用正常空数据（no_data_for_date / empty_ok）。
_Avoid_: 大盘复盘

**涨停/连板分析**:
收盘后基于当日涨停池与强势股池做的市场级涨停结构分析：连板梯队与晋级率（经工具函数 skill 的 promote 指标管道计算，不手算）、涨停归因、龙头点评，结论以 `limit_up.close` 类型经结论存储落库。可单独触发，也由每日复盘（close 模式）编排调用，其摘要编进复盘结论。
