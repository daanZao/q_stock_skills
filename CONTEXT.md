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
由 `backfill_history=True` 显式开启的全市场个股历史补全：以股票清单为标的来源，复用逐日全市场 raw + 累计 B 回填管线，从 `QSTOCK_ADJ_WINDOW_START`（缺省 20230101）起采集并重建 hfq/state，不再直接写 qfq；断点、失败隔离与审计产物在 `parts.backfill` 报告，绝不默认执行。
_Avoid_: 回填、补数据（不加限定时）

### 数据口径

**金额单位（万元）**:
库内所有金额列（成交额、市值、龙虎榜买卖净额等）统一以万元存储，换算收敛在适配器映射层（决策见 `docs/adr/0006`）；基本面透传不规格化、保持上游原单位。
_Avoid_: 元（库内金额口径）

**成交量单位（股）**:
库内所有 volume 列统一以股存储，换算收敛在适配器映射层（东财系手 ×100，baostock 原生为股透传；决策见 `docs/adr/0008`）；存量迁移为一次性运维动作（机检互证后分批执行）。
_Avoid_: 手（库内成交量口径）

**成分字典**:
板块成分归属的库内真源表（stock × 板块 × 生效日期）。现内容为申万行业分类变更史（`qstock-scheduler sw-dict` 在线刷新，issue #118：申万官网分类 xls 直链下载、以文件为准清空重载 'sw' 体系，含北交所归属；手工导入入口 import_board_classification 仍保留）；source_system 体系列（'sw'/'em' 等）中 'em' 为东财概念成分预留，其写路径已随概念维度退役摘除（issue #119），存量行保留只读。point-in-time 归属 = 生效日期 ≤ 当日的最新行。
_Avoid_: 板块映射表

**自算板块行情**:
成分字典 × 库内个股日线在**查询时实时聚合**的板块行情（当日成交额加权涨跌幅、涨跌家数、成交额合计），不加列、不物化，任意历史日期可算。东财 boards 缺失时每日复盘板块节的降级来源，降级日整节切换并标注口径。
_Avoid_: 板块回补数据

**自算强势股**:
strong_stocks 的库内派生产出口径（issue #120，ADR 0012）：统一价格读取接缝的 60 日新高 ∪ zt_pool 近 10 日涨停 ≥2 次，并集入选，`reason` 列为规则文本（"60日新高"/"多次涨停（近10日 N 次）"）。无外部强势股池源（东财口径本身为派生定义），台账来源记 `selfcalc`。自算输入缺失（当日无价格事实）按 no_data_for_date 正常语义跳过，非故障。
_Avoid_: 强势股回补、强势股抓取

**采集台账**:
一切写库动作（fetch/init/调度/手动补抓）的 append-only 审计表 `collection_log`：每次采集动作落一行（交易日/section/status/来源/行数/error_kind/起止时间），全历史保留。status 词表 = 采集结果三态 ok/empty/error + 编排第四态 planned_absent（计划缺席）。就绪检查先读台账：无行=未采集、empty=上游确认无数据、error=采集失败、planned_absent=计划缺席（迁移占位，非故障非未采集），表述不得混用。低频纪律的"每日一次"守卫也读台账（当日已 ok/empty 即跳过上游，issue #120）。复权采集另用 section `stock_adjust_factor`（逐日权威因子）/`stock_daily_raw`（raw 写入：回填为逐日全市场，daily/T+0 为单票区间）/`stock_factor_repair`（待修复原因）/`stock_factor_retracted`（事件撤销事实标记）承载审计；repair 的 error_kind 是复权审计原因词表，不复用抓取三态（ADR 0007 补记、issue #163）。
_Avoid_: 采集日志、抓取记录

**计划缺席（planned_absent）**:
采集编排第四态（决策见 #102，实施 #113）：已定方向待接线的面以该态占位——不抓取、零落库、不计失败、不进异常报告、不触发每日复盘中止线；收盘后批每日为其落一条 planned_absent 占位台账，历史模式就绪归因由此区分"计划缺席"与"未采集"。永久弃用面不占位，直接从采集清单删除（如概念维度资金流字段、概念板块维度整体——后者见 #119 准入闸未过后的弃用实施）。核心 section 集维持原样，真故障（error）仍触发中止。
_Avoid_: 跳过 section、禁用 section

**表承载契约**:
每张表的写入方与读取方在其 DDL 文件头注释成文（`sql/NNN_*.sql` 头注"承载：写 X；读 Y"）；新增表或新写入路径必须同步头注。

**停牌占位行**:
`query_daily` 对停牌交易日输出的占位行：`tradestatus=0` 为权威标注（其余源为 null），无标注时启发式兜底（OHLC 全等且 volume/change_percent 为 null），行带 `suspended`/`suspended_basis` 字段。消费方（indicator-tools 等）按"无该交易日数据"剔除，不填充不伪造，剔除明细记 `excluded_suspended`（决策见 `docs/adr/0009`）。
_Avoid_: 停牌零值行

### 数据表语义

**原始事实**:
由外部证券、披露、资讯或参考体系产生并由本地保存的业务事实；表内可同时含上游发布口径或本地派生列，原子性须按字段判断。
_Avoid_: 原始表（暗示全列原子）

**派生结果**:
由本地原始事实按确定规则计算的结果；可重算性以输入完整、规则版本和陈旧行处理为前提。
_Avoid_: 原始事实、独立采集源

**业务产物**:
由 agent 或分析流程生成的结论性内容，不是可从证券上游重取的原始事实。
_Avoid_: 原始数据

**系统记录**:
描述采集、重建、切换等本地过程或控制状态的信息，不属于证券市场事实。
_Avoid_: 业务事实

**字段来源**:
数据字段的来源标记：U=上游原值、UD=上游发布口径、L=本地计算、M=本地元数据；用于表达混合表，不替代表级分类。
_Avoid_: 整表一刀切

**获取方式**:
数据本身的取得边界：按业务日期重取、仅取当前、从本地事实重算，或只能依靠本地备份恢复；验证状态单独记录，不改变数据性质。
_Avoid_: 可重复（不加限定时）

**归档**:
数据表的生命周期状态，不改变其原有数据分类或字段来源。
_Avoid_: 独立数据类别

### 调度

**交易日窗口**:
按 A 股交易日历计数的闭区间；`--trading-days N` 含截止日在内共 N 个交易日，截止日缺省为最近已收盘交易日，也可用 `--end` 指定。与自然日窗口、日线 `days` 的宽松估算区间严格区分。
_Avoid_: 最近 N 天（未限定交易日）

**整窗刷新**:
显式 `--force` 要求窗口内已有数据也重新抓取并覆盖同业务键记录，窗口外业务数据不动；不用于默认断点续跑或头尾自愈，也不表示清库或镜像删除。
_Avoid_: 强制回补、重建数据库

**统计前置 staging**:
历史窗口早期派生统计所需的窗前输入，只参与计算、不写入窗口外业务表；优先复用库内完整日期，只补缺失或来源无法确认的日期；输入不完整时不得把短窗口结果当成完整统计。
_Avoid_: 临时补数、窗外落库

**市场级后备**:
历史回溯中主源仅部分市场失败时，后备源只补失败市场的数据并保留其他市场主源行；主源正常空数据不属于失败，不触发补缺。它不同于整次抓取失败后切换到下一源的通用 fallback 链。
_Avoid_: 全量覆盖、整链兜底（不加限定时）

**深圳历史主采**:
深圳历史回溯及 staging 的 `lhb_basic` 默认请求链为同花顺 → 深交所官方；同花顺成功即完成，失败才尝试官方。历史正式回溯同时保存深圳席位详情，staging 不保存详情（ADR 0016/0017）。
_Avoid_: 官方唯一主源、后备默认关闭（现行深圳历史语义）

**席位详情修复**:
默认历史窗口内 basic 完整但沪深席位详情缺失或部分失败时，复用库内 basic，只整体替换本次取得有效新详情的父记录，其他父记录旧详情保留；完整父记录须同时有买、卖席位行，来源缺字段允许 NULL。basic 与详情均完整才保留，营业部榜失败不触发 basic 重抓，`--force` 同时重抓两者。basic 可独立于详情使用，详情缺口只降级告警、不单独使采集或每日作业失败。北交所、非 A 股及暂时无法识别代码不保存详情，也不计缺失。
_Avoid_: 强制整窗重抓、逐席位 upsert、详情缺失即采集失败

**收盘后批**:
`qstock-scheduler close` 执行的交易日收盘后采集：盘面五 section（boards/zt_pool/strong_stocks/lhb/indices）快照，直调 tools_* 核心层、不经 MCP 协议层，逐 section 独立成败并落采集台账。调度安装（launchd）为用户侧动作，见 `docs/scheduler-launchd.md`。
_Avoid_: 定时任务（不加限定时）

**次晨批**:
`qstock-scheduler morning` 执行的开盘前采集（目标日恒为前一交易日）：baostock 一把抓全市场 raw（含 tradestatus 权威停牌列、turn 换手率，volume 股 / amount 万元口径）+ 逐日累计 B，并重建受影响 hfq/state；另补 7 大指数与申万估值，逐部分独立成败并落采集台账。baostock 当日 bar 晚间才就绪，次晨批不用于当日。
_Avoid_: 早盘补抓

**板块回补**:
`qstock-scheduler backfill` 执行的板块历史补数入 `market_boards`，申万官网 trend 指数全历史回补申万双级口径行（issue #118，spec #111；issue #121 定稿单源——东财 push2his 回补与概念板块回补已整体摘除，BoardHistoryAdapter 协议随之退役）：默认窗口 = 19990101 起、历史深至 1999，解除 5/10 日涨跌"自落库日起积累"限制。回补行带 source 标注（'swsresearch'）与实时采集行（NULL）区分，业务键幂等可重跑；CLI 专用路径，不经 fetch 工具的历史日期防线。存量 'push2his' 回补行为东财退役前产物，保留只读。概念维度已永久退役（issue #119：THS 概念成分端点准入闸未过，#106 弃用方案）——收盘后批的概念成分积累（'em' 体系）已摘除，存量 'em' 行保留只读，无任何采集路径再写概念板块。

**复权事件回填**:
`qstock-scheduler adj-backfill` 执行的复权回填（issue #163，spec #158 §6.1/§6.2）：窗口内逐日全市场累计因子事件集（成功空日/失败日可审计，失败不当作无事件）+ D-1 与窗口内逐日全市场不复权 raw；随后本地按票重建，事件票反推窗口基线 B0、无事件票 B0=1，并重建 hfq/state。断点续跑按因子日/raw 日读采集台账，失败重试，未决项落 `stock_factor_repair` 待修复台账，事件撤销落 `stock_factor_retracted` 标记（与整日替换同事务并标记受影响 state 为 pending）；同 B 非阶跃行不写事件表。`--end` 必须覆盖库内最新复权事实，历史窗口不回退窗口外派生。#164 复用本入口的无事件票 raw 并验收分类/隔离；北交所与替代源全市场回填不在本入口。
_Avoid_: 板块回补

**复权迁移硬验收**:
`qstock-scheduler adj-acceptance` 执行的切换前验收（issue #165，spec #158 §7）：复用迁移产物和库内快照，以全量结构/事件 SQL、小样本逐值定值、100+100 分层样本和每股四表摘要哈希覆盖八项判据；Golden 边界票不占普通样本名额。默认零上游、只读业务表；真实上游 smoke 仅限显式 3–5 个连续交易日。八项全绿才可判 verified，pending、未解释显著差异或未运行 smoke 均不得宣称可切换。
_Avoid_: 全市场新旧逐值对拍、为验收重抓全历史

**个股日线入口**:
`qstock-scheduler daily` 执行的个股日线固定入口：写库时以 baostock 为首选采集 raw，并逐日确认累计 B；仅在可信 B0 已存在时重建 hfq/state，无锚只写事实。单票 raw 可由腾讯/新浪补缺但保持 partial，后续 baostock 可覆盖升级并修订派生；`--all` 固定走 baostock 逐日全市场整窗重刷，不在全市场失败后切换替代源。写库模式的 `adj` 仅兼容回显，`--check` 才按所选口径做三源对拍且不写库。手册见 `docs/daily-source-cli.md`。
_Avoid_: 日线脚本、补数脚本

**指数日线入口**:
`qstock-scheduler index` 执行的指数日线固定入口：默认经腾讯 → baostock 的 fallback 链抓取区间并按业务键幂等写入 `index_daily`，逐指数独立成败；用于断档回填（如科创50 自 2026-10-13 起缺口，腾讯 K 线为首个覆盖 000688 的日线源）与收盘后当晚 T+0 当日行补采；`--verify` 纯读覆盖核验（对照交易日历列缺口），不写库。缺省代码 = 7 大指数白名单。盘中（交易时段）当日行 defer 不落库（issue #78）。手册见 `docs/index-source-cli.md`。
_Avoid_: 指数脚本、回填脚本

**对拍**:
换源/迁移后的数据质量验证动作：逐源直取同一交易日数据并做字段级比较，不落库；总验收入口 `qstock-scheduler parity`（issue #122：五核心面逐面对拍 + 库内历史连续性，复用各面 parity 工具），逐面入口 `daily`/`snapshot`/`sw-boards`/`lhb` 的 `--check` 与 `index --verify`。后复权（hfq）基准因源而异，只比成交量/成交额/换手率。
_Avoid_: 校验、比对（不加限定时）
_Avoid_: 回填、补数据（不加限定时）

**复权回落**:
腾讯 K 线请求 qfq/hfq 但上游只给 `day` 键时按"无复权事件"返回 day 数据的语义（ADR 0010）；响应无法区分"无复权事件"与"上游不提供调整序列"，故回落必须可见：适配器 `adjust_fallback` 标记 + WARNING 日志 + fetch/query 响应 note + 采集台账 detail「复权回落=day」+ 对拍源条目 note 五处同步标注，不静默落库。
_Avoid_: 复权降级、静默回落

**累计因子 B**:
baostock `backAdjustFactor` 的累计复权因子口径，属于旧 raw+B/hfq 模型和原始审计词汇；全局前复权读模型不消费 B，也不以其变化触发 pending。
_Avoid_: 把 B 当作前复权段乘数、以 back 异常阻塞 qfq

**前复权段乘数（fore）**:
baostock `foreAdjustFactor` 的分段前复权乘数；直接价格为 `qfq=raw×fore`，事件重锚只使用相邻 fore 比值，back/adjustFacto(r) 不参与。
_Avoid_: 用 back 或 adjustFactor 代替 fore、永久缓存随新事件重锚的 fore 绝对值

**窗口基线 B0**:
ADR 0018 旧影子模型的窗口归一锚，已随 #206 被 ADR 0021 淘汰；新模型不建立、不消费 B0，也不以窗口外基线决定 qfq 可用性。
_Avoid_: 默认基线、provisional 基线、查询窗基线、新模型建锚

**窗口基线 D-1 输入**:
ADR 0018 旧模型反推 B0 时读取的窗口外前一交易日 raw 输入；新模型不使用该概念。
_Avoid_: 窗口内 D-1、legacy 价格输入、新模型基线输入

**归一累计因子 F**:
ADR 0018 旧模型的 `F=B/B0` 及 hfq 派生因子，已随 B0/hfq 一并淘汰；新模型不存在 F。
_Avoid_: 把事件表 B 与 hfq 表 F 混为同一 factor_cum、新模型归一因子

**前复权读模型**:
以当前最新除权事实为锚的可变 qfq 视图；初始化采用上游 qfq，后续按事件重锚历史，不承诺 point-in-time（ADR 0021）。
_Avoid_: qfq 读时 hfq 派生、point-in-time qfq、固定历史前复权价

**前复权重锚**:
T 日事件以单次因子 `f_T=fore_prev/fore_cur` 只更新 `date<T` 的历史价格；T 日及之后的新行和非价格字段不变，每个事件至多应用一次。
_Avoid_: 重放全部历史事件、每次事件重抓全历史、修改 T 日及之后价格

**前复权锚日期**:
某行 qfq 已纳入的复权版本；初始化行取实际数据截止交易日，日常新增行为空，事件重锚后事件日前历史行取该事件日。它不是该行交易日实际发生除权的声明，也不是历史时点快照。
_Avoid_: 实际除权日、point-in-time 版本、最新 raw 日期与 F_latest 的组合

**价格来源（value_origin）**:
qfq 价格字段的来源分类：`upstream_qfq` 为上游直接 qfq，`raw_identity` 为最新 raw 恒等写入，`local_reanchor` 为本地事件乘算；不据此推断成交量、成交额等非价格字段也被本地计算。
_Avoid_: 整行本地计算、raw 恒等行即上游 qfq

**复权待处理（pending）**:
`factor_unverified` 表示当日事件集未核验，`reanchor_pending` 表示已确认事件但事件日前历史尚未重锚；允许多个 pending，T 日及之后的新行仍然有效。
_Avoid_: 当日数据错误、成功空日即失败、最多一个 pending

**复权覆盖（coverage）**:
新价格模型按 raw/qfq 各自日期覆盖和未决 pending 判定可用性；有行不等于完整，`factor_unverified`、`reanchor_pending` 或已知缺口不得判 complete，不再以 B0/hfq/state 为前提。
_Avoid_: 有行即完整、采集成功即 complete、以旧因子状态代替 qfq 覆盖

**复权切读**:
全局个股价格读取从 legacy/旧影子模型切到 raw/qfq 双口径，覆盖区间序列、横截面、窗口极值、最新日期、coverage、强势股和申万自算，不只切换单个查询工具；回填、硬验收、消费迁移和生产切读分别完成。
_Avoid_: 只切 query_daily、以开发完成代替生产完成、沿用旧 B0 八项验收

**修复回滚**:
ADR 0018 旧影子模型恢复 legacy 读取的历史术语；ADR 0021 不采用该回滚链，新模型异常通过 pending 补偿或人工单票全量刷新处理。
_Avoid_: 只切 flag、恢复 B0/hfq/state、自动重抓全历史

**roll-forward**:
ADR 0018 旧影子模型修复回滚后重新切读的历史术语；ADR 0021 不再使用该状态机。
_Avoid_: 新模型回滚状态、重新启用旧切读状态机

**baostock 实例租约**:
单个 CLI 命令或 MCP 进程实例持有的本地 baostock 互斥许可；其他进程即使属于同一入口也不能共享。正常释放归持有实例，崩溃残留经人工确认 stale 后 force 清理（ADR 0019）。
_Avoid_: 主机级共享许可、同入口共享锁、并发会话

**CLI 命令级自动租约**:
CLI 数据命令首次实际使用 baostock 时自动取得实例租约，并在整个命令结束时自动释放；同命令复用租约与会话，不按请求申请或释放。
_Avoid_: CLI 三段式手工锁、每次请求加锁

**MCP 实例显式租约**:
MCP agent 获得用户确认后为当前进程实例显式申请的 baostock 实例租约；相关数据工具仅在该实例持锁时可用，并由该实例显式释放。
_Avoid_: MCP 自动申请、未经确认申请、跨 MCP 实例共享

**baostock 会话**:
一个实例租约内复用的单例 `BaostockApi` 会话；同一会话串行查询，单请求 60s 超时，`10001001` 重登一次并重试同一请求，`10001011` 致命不重试。租约释放时关闭。
_Avoid_: 每请求 login/logout、跨进程共享会话

**baostock 日请求预算**:
baostock 协议动作（login/logout/query/page）的本地自然日额度；45,000 警告、50,000 停止新租约/新请求，Asia/Shanghai 重置（ADR 0019）。
_Avoid_: 只统计数据查询、5 万后再说

**MCP baostock 租约工具**:
`baostock_lock`（acquire/release/status）；无实例租约的相关数据工具返回 `baostock_locked`，agent 必须先获得用户确认再 acquire（ADR 0019）。
_Avoid_: 数据工具自动申请租约、静默排队

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
