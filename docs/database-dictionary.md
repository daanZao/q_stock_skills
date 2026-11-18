# 数据库字典（评审草案）

> **PROTOTYPE / 评审草案**：本文件用于确认数据库字典的信息架构和内容粒度，尚未成为正式消费契约。
> 当前实现基线为 `9868ddd`；业务库形态核验日期为 2026-11-18。正式版见 issue #193 的后续收口。

本字典回答五个问题：这张表是什么、从哪里来、谁在读、按什么日期查询、以后如何重新取得或重算。列名、类型和约束以 `src/qstock_mcp/sql/` 为准；本文件不复制整段 DDL。

## 1. 阅读约定

### 1.1 当前实现与目标消费分开

- **当前实现**：代码中已经存在的写方、读方和存储方式。
- **目标消费**：后续 MCP / skill 应遵守的数据使用边界，不代表代码已经改造。
- 数据是否完整、某日是否可复盘，不在本字典中判断；完备等级和水位归 `data-status` 地图 #174。

### 1.2 表级只分四类

| 分类 | 含义 |
| --- | --- |
| **原始事实** | 外部证券、披露、资讯或参考体系产生并由本地保存的业务事实。表内可以含本地计算列。 |
| **派生结果** | 由本地事实按确定规则计算的结果，必须说明输入、窗口和陈旧行处理。 |
| **业务产物** | agent 或分析流程生成的结论性内容，不能从证券上游重新取得。 |
| **系统记录** | 描述采集、重建、切换等本地过程或控制状态的信息。 |

`归档` 是生命周期状态，不是第五种数据类别。

### 1.3 字段来源与表级分类正交

| 标记 | 含义 |
| --- | --- |
| **U** | 上游原值；单位换算和代码归一化不改变其来源。 |
| **UD** | 上游已经计算或分类的发布口径，例如 PE、题材和排行榜。它是上游事实，不是更底层的原子观测。 |
| **L** | 本项目从其他字段或表计算、复制或派生。 |
| **M** | 本地来源、审计时间、pending、控制状态等元数据。 |

表级标为“原始事实”不表示所有列都是原子值。混合表必须看字段说明。

### 1.4 获取能力与验证证据分开

每表使用以下获取方式，不再用 R1–R5 给数据本身贴标签：

- **按业务日期重取**：上游提供日期或事件寻址；仍须记录覆盖范围和修订条件。
- **仅取当前**：只能取得执行时的当前清单、快照、搜索或累计排行。
- **本地重算**：固定完整输入、代码/种子版本和陈旧行策略后可重建。
- **只能备份恢复**：精确历史是本地运行证据、控制状态、业务产物或归档整体。

“代码存在历史入口”和“已经做过同口径重取验证”是两件事。后者必须另记验证日期、样本和结果。

## 2. 日期轴、来源与单位

### 2.1 日期轴不能互换

| 日期 | 含义 |
| --- | --- |
| `trade_date` | 行情、榜单或结论所针对的业务交易日，不一定是物理写入日。 |
| `ex_date` | 复权事件的除权日。 |
| `effective_date` | 行业成分归属的生效日；point-in-time 查询取不晚于目标日的最新行。 |
| `fetch_date` | 当前累计榜的本地快照日期，不能当作历史 `trade_date`。 |
| `publish_time` | 资讯发布时间。 |
| `fetched_at` | 资讯入库抓取时间。 |
| `created_at / updated_at` | 本地写入或修订时间。 |
| `latest_trade_date` | 当前状态对应的事实尾日，不是逐日历史轴。 |
| `fact_watermark` | 切读控制中的本地时间戳，不是交易日水位。 |

### 2.2 来源有三个层级

- **行级 `source`**：可以归属该行，但唯一键通常不含 source，同键复核可能覆盖旧来源。
- **采集台账 `collection_log.source`**：归属一次采集动作，不能自动回填所有历史行。
- **没有来源列**：不能从其他表或单条台账猜整表来源。

### 2.3 单位与空值

- 库内金额统一为**万元**，`volume` 统一为**股**；基本面透传不规格化，见 ADR 0006、0008。
- 上游不提供的字段保持 `NULL`。停牌、无事件、非交易日、未发布、未采集和采集失败是不同语义。
- 业务键 upsert 只保证同键不重复，不保证整日集合完整，也不保证消失行被清理。

## 3. 全表速查

| 表 | 分类 | 日期轴 | 获取方式 | 主要写方 | 主要读方 |
| --- | --- | --- | --- | --- | --- |
| `stock_daily` | 原始事实（混合） | `trade_date` | 按日期重取，受口径/修订影响 | legacy 自愈、legacy morning、回滚修复 | LegacyPriceReader 及其 MCP/skill 消费方 |
| `stock_daily_legacy` | 保持原分类；归档 | `trade_date` | 精确整体只能备份恢复 | cutover / roll-forward 改名 | LegacyPriceReader |
| `index_daily` | 原始事实（混合） | `trade_date` | 按日期重取，T+0/次晨可修订 | init、index、morning | query_index_daily、daily-review、SEPA |
| `market_indices` | 原始事实（混合） | `trade_date` | 仅取当前 | close、fetch_board_snapshot(indices) | query_board_data、当前 daily-review/SEPA |
| `market_snapshot` | 原始事实（混合） | `trade_date` | 仅取当前 | snapshot、init、fetch_market_snapshot | query_snapshot、当前 daily-review/limit-up-analysis |
| `stock_daily_raw` | 原始事实（混合） | `trade_date` | 按日期重取，允许修订 | daily、morning、backfill、init、自愈 | NewPriceReader、审计与切换 |
| `stock_adjust_factor` | 原始事实 | `ex_date` | 按日期重取，允许补录/撤销 | 同 raw 管线 | rebuild_stock、NewPriceReader、审计 |
| `stock_daily_hfq` | 派生结果 | `trade_date` | 本地重算 | rebuild_stock | NewPriceReader、审计与回滚 |
| `stock_factor_state` | 系统记录（含派生锚点） | `latest_trade_date` | 本地重算 + 控制恢复 | rebuild_stock、事件撤销 | NewPriceReader、审计与切换 |
| `stock_adjustment_switch_state` | 系统记录 | 无业务日期轴 | 只能备份恢复/受控迁移 | adj-switch | 切换预检和状态报告 |
| `market_boards` | 原始事实（混合，含留档） | `trade_date` | trend 按日期重取；current 仅取当前 | sw-boards、close、backfill | query_board_data、daily-review、limit-up-analysis |
| `zt_pool` | 原始事实（混合） | `trade_date` | 可按日期重取；深史待验证 | close、limitpool、fetch_board_snapshot | limit-up-analysis、daily-review、强势股自算 |
| `strong_stocks` | 派生结果 | `trade_date` | 本地重算 | close、limitpool | daily-review、limit-up-analysis |
| `sw_board_valuation` | 原始事实（UD 为主） | `trade_date` | 按日期重取，T+1 修订口径 | morning | query_board_data、次日修订分析 |
| `sw_industry_meta` | 原始事实（参考种子） | 无业务日期轴 | 固定种子本地重放 | import_sw_industry_meta | 自算板块、coverage |
| `sw_industry_class` | 原始事实（含 `em` 留档） | `effective_date` | 固定文件重放；在线文件仅当前版本 | import_board_classification、sw-dict | 自算板块、行业归属 |
| `lhb_basic` | 原始事实 + L 汇总列 | `trade_date` | 按日期重取，源/详情覆盖有边界 | close、lhb、fetch_board_snapshot | daily-review、statistic、coverage |
| `lhb_stock_detail` | 原始事实 + L 兼容列 | `trade_date` | 按日期重取，受市场覆盖影响 | 同 basic | 席位分析、详情修复 |
| `lhb_stock_statistic` | 派生结果 | `trade_date` | 本地重算 | LHB 写入后的 recompute | daily-review、lhb --check、coverage |
| `lhb_yyb_capital` | 原始事实（上游累计排行） | `fetch_date` | 仅取当前 | close、单日 lhb | 当前 daily-review |
| `lhb_yyb_most` | 原始事实（上游当前排行） | `fetch_date` | 仅取当前 | close、单日 lhb | 当前 daily-review |
| `stock_list` | 原始事实（当前参考清单） | 无历史业务日期轴 | 仅取当前 | init、stock-list | 全量回溯、快照清单、coverage、名称补充 |
| `news_items` | 原始事实 + 本地主体关联 | `publish_time` | 当前搜索；已存 raw 可再投影 | mx_search | query_news、daily-review、limit-up-analysis、SEPA |
| `conclusions` | 业务产物 | `trade_date` | 只能备份/原产物恢复 | save_conclusion | query_conclusions、skill 间结论引用 |
| `collection_log` | 系统记录 | `trade_date` + 起止时间 | 只能备份恢复 | 各采集/导入/自算路径 | 就绪归因、断点、完备性和低频守卫 |

## 4. 逐表明细

### 4.1 个股、指数与复权

#### `stock_daily`

- **含义/键**：legacy 个股日线，键 `(stock_code, trade_date, adj)`；同一股票日期可有 qfq/hfq/none 多口径。
- **写/读**：legacy 查询自愈和 legacy morning 写入；回滚可修复三种口径。LegacyPriceReader 服务 query_daily、强势股和申万自算。
- **字段来源**：行情为 U，部分源的涨跌额、涨跌幅和振幅为 L；`adj/source/timestamps` 为 M。上游 qfq/hfq 不是 raw 原子价格。
- **获取边界**：可按日期重取部分来源，但换源、复核和复权锚点会改变结果。2023 年以前 qfq 仍依赖 legacy，不能反推 raw。

#### `stock_daily_legacy`（条件存在）

- **含义/键**：cutover 时由 `stock_daily` 改名形成的冻结留档，结构与业务键不变。
- **写/读**：cutover、rollback、roll-forward 控制表名；LegacyPriceReader 优先读取归档表。
- **字段来源**：继承 `stock_daily`，归档不会把旧 qfq 变成原子事实。
- **获取边界**：精确归档整体只能由备份或归档恢复；重新采集不能恢复原行集合、来源和当时的 qfq 锚点。

#### `index_daily`

- **含义/键**：主要指数不复权日线，键 `(index_code, trade_date)`，指数与个股代码冲突因此分表。
- **写/读**：init、`index` 和 morning 写入，腾讯 T+0、baostock 次晨复核；query_index_daily、daily-review 和 SEPA 读取。
- **字段来源**：OHLCV、amount、turnover 为 U；腾讯涨跌和振幅为 L，baostock 部分字段为 U 或 NULL；source 为 M。
- **获取边界**：按指数和日期重取，T+0/次晨和换源可修订；upsert 不清理上游撤掉的历史行。

#### `market_indices`

- **含义/键**：指数实时/收盘截面，键 `(trade_date, index_code)`；不是指数历史日线。
- **写/读**：close 和 `fetch_board_snapshot(indices)` 写；当前 daily-review、SEPA 降级和 query_board_data 读。
- **字段来源**：腾讯行情和量比为 U/UD，新浪部分涨跌字段为 L；本表没有行级 source。
- **获取边界**：仅取当前，历史日期被实时 section 防线拒绝。close 目标消费不再使用本表。

#### `market_snapshot`

- **含义/键**：全市场个股实时/收盘截面，键 `(trade_date, stock_code)`，含行情、PE/PB 和市值。
- **写/读**：snapshot、init 和 fetch_market_snapshot 写；query_snapshot、当前 daily-review 与 limit-up-analysis 读。
- **字段来源**：行情、估值、市值为 U/UD；广度和成交额聚合在查询时计算，不是本表物化列。
- **获取边界**：仅取当前；API 日期与请求日期不符即拒收。daily-review 目标消费从个股日线派生所需市场截面，PE/市值启发式需另行处理。

#### `stock_daily_raw`

- **含义/键**：新模型个股不复权日线输入事实，键 `(stock_code, trade_date)`；D-1 可仅作基线计算输入。
- **写/读**：daily、morning、adj-backfill、init 和 new 查询自愈写；NewPriceReader、审计、切换与回滚读。
- **字段来源**：baostock OHLC、preclose、量额、pctChg、tradestatus、turn 为 U；替代源部分涨跌字段为 L；source 为 M。
- **获取边界**：按日期重取并支持修订，替代源字段集不同。preclose 是除权参考价，不恒等于上一 raw close；请求失败不能当空日。

#### `stock_adjust_factor`

- **含义/键**：每股除权日累计 B 事件证据，键 `(stock_code, ex_date)`。
- **写/读**：与 raw 同一管线写；rebuild_stock、NewPriceReader、审计和切换读。
- **字段来源**：`factor_cum` 只取 baostock `backAdjustFactor`，`adjustFacto(r)` 不参与派生；kind/detail/reg_date 可空。
- **获取边界**：可按日期重取，但补录、改期和撤销会修订下游。事件表本身不能证明“无事件”，还需全窗口逐日成功/空日台账。

#### `stock_daily_hfq`

- **含义/键**：本地物化后复权日线，键 `(stock_code, trade_date)`。
- **写/读**：rebuild_stock 写；NewPriceReader、审计、切换和回滚读。
- **字段来源**：OHLC 和 `factor_cum=F` 为 L，量额、换手、停牌状态复制 raw；qfq 不落表，而由 hfq 按最新锚点读时派生。
- **重算条件**：raw、完整事件、可信 B0、代码版本和无未决异常必须固定。新事件可重锚全历史 qfq。

#### `stock_factor_state`

- **含义/键**：每股复权锚点及重建控制状态，键 `stock_code`；`latest_trade_date` 是状态尾日。
- **写/读**：rebuild_stock 和事件撤销写；NewPriceReader、审计和切换读。
- **字段来源**：B0、F_latest 和尾日为 L；pending、重建时间为 M/控制值。
- **重算条件**：需要同一建锚权限、窗口和完整事实。无锚、pending 与 complete 不能互换；本表分类为系统记录，同时保存派生锚点。

#### `stock_adjustment_switch_state`

- **含义/键**：单行复权切读状态机，键固定 `id=1`，mode 为 legacy/new。
- **写/读**：adj-switch 的 cutover、rollback、roll-forward 写；状态报告和预检读。
- **字段来源**：全部为 M/控制值；数据库 mode 不会自动修改进程 `QSTOCK_PRICE_SOURCE`。
- **获取边界**：动作可重入不等于历史可重取。精确状态与时间戳只能靠本地记录和备份恢复。

### 4.2 板块、涨停与强势股

#### `market_boards`

- **含义/键**：行业/概念板块行情，物理键 `(trade_date, board_type, board_name)`，**不是 board_code**。申万双级和退役口径可并存。
- **写/读**：sw-boards、close 写 current，backfill 写 trend；query_board_data、daily-review 和 limit-up-analysis 读。自算板块不写本表。
- **字段来源**：current 的点位和量额为 U、涨跌为 L；trend 的涨跌为 U、preclose/振幅含 L；source 为 M。
- **获取边界**：trend 可按日期重取，current 仅取当前。实时、回补和退役留档不能混成同口径；upsert 不清理改名或消失板块。

#### `zt_pool`

- **含义/键**：涨停 zt、跌停 dt、炸板 zb 事件池，键 `(trade_date, pool_type, stock_code)`。
- **写/读**：close、limitpool 和 fetch_board_snapshot 写；limit-up-analysis、daily-review 和强势股自算读。
- **字段来源**：价格、首封、板型、封单额、题材为 U/UD；连板数含本地解析。THS 不提供行业等字段，保持 NULL。
- **获取边界**：THS 有 date 参数，但仅实测 4 天历史，更深覆盖和修订稳定性待验证；每日一次守卫使重跑可能故意零请求。

#### `strong_stocks`

- **含义/键**：本地规则选股池，键 `(trade_date, stock_code)`；规则为 60 交易日新高 ∪ 近 10 交易日涨停至少 2 次。
- **写/读**：close、limitpool 自算并写；daily-review 和 limit-up-analysis 读。
- **字段来源**：入选集合和 reason 为 L；行情、连板和名称复制其他事实；市值和行业无真源保持 NULL。
- **重算条件**：需要完整价格、zt_pool、stock_list 和窗口。当前写路径只 upsert、不清修订后落选旧行，因此从空表重算与旧表重跑必须分开验证。

#### `sw_board_valuation`

- **含义/键**：申万双级板块每日估值报表，键 `(trade_date, board_code)`，T+1 获取。
- **写/读**：morning 写；query_board_data 和次日修订分析读，当前收盘版不作为当晚必需输入。
- **字段来源**：收盘指数、换手、PE/PB、流通市值和股息率为 U/UD，金额换算为万元。
- **获取边界**：可按日期区间重取，但属于 T+1 修订口径，不能与 current/trend 混作同字段版本。

#### `sw_industry_meta`

- **含义/键**：申万行业代码、名称和级别参考表，键 `industry_code`。
- **写/读**：import_sw_industry_meta 从仓库 CSV 种子写；自算板块和 coverage 读。
- **字段来源**：来自固定种子，不是某日官网快照；种子合并多版本，新版名称优先。
- **获取边界**：同一 commit 的种子可本地重放；跨版本集合可变化，upsert 不删除种子已移除代码。

#### `sw_industry_class`

- **含义/键**：股票到行业的生效日期变更史，键 `(source_system, stock_code, board_code, effective_date)`。
- **写/读**：固定文件导入或 sw-dict 在线下载后整体系替换；自算板块和行业归属读取 point-in-time 版本。
- **字段来源**：sw 来自申万 xls，代码和日期经本地映射；em 为退役留档。
- **获取边界**：固定同一文件可确定重放并清陈旧行；在线 URL 只提供当前发布版本，不能恢复过去某次文件版本。

### 4.3 龙虎榜

#### `lhb_basic`

- **含义/键**：按上榜原因拆分的龙虎榜父事件，键 `(trade_date, stock_code, lhb_reason)`；同股同日可多原因。
- **写/读**：close、lhb 和 fetch_board_snapshot 写；daily-review、statistic 和 coverage 读。
- **字段来源**：代码、原因和可得行情为 U；buy/sell 是买卖前五席位的 L 汇总，net=buy−sell、total=buy+sell，均为 L。
- **获取边界**：在上游覆盖范围内按日期重取；换源、详情子集和保留策略会影响汇总列。整表不能标为本地重算。

#### `lhb_stock_detail`

- **含义/键**：basic 父事件下的席位记录，代理键 id + 外键 basic_id；逻辑父键同 basic，席位行无物理业务唯一键。
- **写/读**：与 basic 同一 LHB 链写，按父组替换；席位分析和详情修复读。
- **字段来源**：席位、方向和可得金额为 U；`amount` 为方向金额的 L 兼容列。官方未提供的字段保持 NULL。
- **获取边界**：可按日期重取，但受父事件、市场覆盖和详情保留规则约束；北交所、非 A 股和未知代码不强行补席位。

#### `lhb_stock_statistic`

- **含义/键**：按股票物化的近 **60 日历日** LHB 聚合快照，键 `(trade_date, stock_code)`。
- **写/读**：basic 写入后重算；daily-review、lhb --check 和 coverage 读。
- **字段来源**：全部为 L/M；`appear_count_3m` 实际是窗口 basic 行数，同股多原因分别计，不是自然三个月。
- **重算条件**：目标日前 60 日历日 basic 必须完整；当日 DELETE+INSERT 可随窗口和修订自愈，缺输入时不能把短窗结果当完整统计。

#### `lhb_yyb_capital`

- **含义/键**：同花顺营业部当前累计资金榜快照，键 `(fetch_date, seat_name)`。
- **写/读**：close 和单日 lhb 写；当前 daily-review 读，历史 LHB 回溯不写。
- **字段来源**：排名、席位和累计金额为 U/UD；卖出、净额和均额无来源保持 NULL。
- **获取边界**：接口无日期参数，仅取当前；fetch_date 不揭示上游统计窗口，upsert 不清已消失席位。

#### `lhb_yyb_most`

- **含义/键**：同花顺营业部当前上榜排行，键 `(fetch_date, seat_name)`。
- **写/读**：同 capital。
- **字段来源**：排名、上榜次数和年内买入股票只数为 U/UD；`buy_amount` 实为“合计动用资金”（买+卖），不是纯买入。
- **获取边界**：仅取当前，历史回溯不补。目标若改为本地营业部排行，必须另备完整历史席位、统计窗口和排名规则。

### 4.4 清单、资讯、结论与台账

#### `stock_list`

- **含义/键**：当前 A 股代码和名称清单，键 `stock_code`，没有历史 Universe 或上市/退市生效轴。
- **写/读**：init 和 stock-list 写；全量回溯、快照清单、coverage 和名称补充读。
- **字段来源**：代码名称为 U，source/updated_at 为 M；当前清单主源为交易所官网，baostock 后备能力不同。
- **获取边界**：仅取当前；按 code upsert 不删除缺席代码，不能重建历史 Universe。

#### `news_items`

- **含义/键**：资讯按市场/股票主体沉淀，键 `(news_code, subject_type, subject_code)`；同一资讯挂多主体会产生多行。
- **写/读**：mx_search 写；query_news 和 daily-review、limit-up-analysis、SEPA 读。
- **字段来源**：内容和发布时间为 U，主体关联为调用方输入，fallback 键和时间解析含 L/M。
- **获取边界**：搜索是问句驱动的当前结果，同问句不保证同集合。已存 raw 可再投影，不能恢复未曾搜到的资讯或历史排名。

#### `conclusions`

- **含义/键**：agent 生成的 JSON 结论，键 `(subject_type, subject_code, trade_date, conclusion_type)`。
- **写/读**：save_conclusion 写；query_conclusions 和 skill 间引用读。payload 结构由 skill 自报，server 不校验语义。
- **字段来源**：主体、业务日、类型和 payload 都是本地业务产物；created/updated 为 M。
- **获取边界**：同键 upsert 会覆盖旧 payload且无版本历史；可重复保存相同 JSON，但不能从证券上游重建 agent 文本和判断。

#### `collection_log`

- **含义/键**：采集、导入、自算、拒收、计划缺席和复权修复的 append-only 台账，仅代理 id，无业务唯一键。
- **写/读**：各写路径记录；就绪归因、断点续跑、复权完备性和 THS 低频守卫读。
- **字段来源**：section/status/source/rows/error_kind/detail 都是一次动作的 M 摘要，detail 不是原始响应。
- **获取边界**：故意非幂等，每次动作新增行；台账可能缺行且历史 repair 不等于当前 pending，精确运行史只能由本地备份恢复。

## 5. 每日复盘：当前实现与目标消费

> 本节是已确认的目标使用边界，不表示当前 skill 已完成改造。实施需另起 issue。

| 逻辑事实 | 当前实现 | 目标消费 |
| --- | --- | --- |
| 指数收盘与历史 | 当日读 `market_indices`，历史读 `index_daily` | close 统一以 `index_daily` 为指数事实；量比另定派生或删除 |
| 市场广度与成交额 | `market_snapshot` 聚合 | 从全市场个股日线事实本地聚合 |
| 成交额前 75 | `market_snapshot` 排序 | 从个股日线事实排序，`stock_list` 补名称 |
| 指数实时截面 | close 必需 `market_indices` | 不进入 close；midday 如保留则单独定义 |
| 涨停/连板 | `zt_pool` + 物化 `strong_stocks` + `limit_up.close` | zt_pool 是事件事实；强势股和 limit_up 是派生结果，不冒充独立上游 |
| 龙虎榜 | basic + statistic + 两个 yyb 累计榜 | basic/detail 是按日期事实；统计和排行目标为查询时派生 |
| 市值/PE 归因 | limit-up-analysis 读 `market_snapshot` | 删除、独立增强，或另找明确基本面/股本事实；不得保留隐藏快照依赖 |
| 资讯 | `news_items` 可选 | 保持可选，不作为行情数字来源 |

## 6. Skill 消费导航

| 使用方 | 当前入口 | 数据使用规则 |
| --- | --- | --- |
| **daily-review** | MCP query/fetch + indicator-tools 管道 | 读取逻辑事实，不直连 PG。历史模式禁用 fetch；目标消费按上表改造，报告必须标注实际 basis。 |
| **limit-up-analysis** | MCP query/fetch + promote/deviation 管道 | zt_pool 是核心事件事实；strong、梯队、晋级率和营业部统计是派生结果。市值/PE 快照依赖需单独收口。 |
| **SEPA** | query_daily、query_index_daily、基本面透传、可选资讯 | 个股价格和复权经统一 reader；指数快照只作降级，不与 index_daily 混称同口径。 |
| **indicator-tools** | stdin/stdout 管道 | 不访问数据库，不物化指标。输入契约由 `query_daily` 等工具输出和技能文档共同维护。 |

## 7. 常见误用

1. 用 `market_snapshot` 或 `market_indices` 请求历史日期。
2. 把 `index_daily` 与 `market_indices` 混称为同一指数行情。
3. 把业务键 upsert 当成整日集合替换、历史可重取或本地可重算。
4. 把 `market_boards.board_code` 当物理唯一键，或混合申万双级与退役行业/概念口径。
5. 把 `lhb_basic.total_amount` 当全日成交额；它是买前五 + 卖前五席位合计。
6. 把 `lhb_stock_statistic.appear_count_3m` 当自然三个月统计。
7. 把 `lhb_yyb_most.buy_amount` 当纯买入额。
8. 用 `fetch_date` 连接历史 basic.trade_date，或把资讯 `fetched_at` 当发布时间。
9. 把 `stock_adjust_factor` 当完整公司行为登记簿，或在没有逐日台账时证明“无事件”。
10. 把 qfq 当固定物化历史；新模型 qfq 会随最新锚点重锚。
11. 把 switch `fact_watermark` 当交易日水位，或只看数据库 mode 忽略进程 env。
12. 把历史 `collection_log` repair/error 行当当前 pending，或对 rows 求和当表内现存量。
13. 把 `conclusions` 同键 upsert 当版本历史，或认为 agent 结论能从行情上游重取。
14. 把 `strong_stocks` 的计算公式当旧表重跑收敛保证；当前还需检查陈旧行清理。
15. 用当前 `stock_list` 或当前行业文件解释历史 Universe / 历史成分发布版本。

## 8. 维护规则

- 新增表、修改业务键/日期轴、改变写方或读方、调整字段来源时，同步本字典和对应 DDL 头注。
- 新增派生结果必须写明输入表、窗口、完整性条件、代码/种子版本，以及从空输入重建和旧表重跑两种行为。
- 新增“可重取一致”声明必须附真实限定样本证据；否则只记录入口能力和未验证状态。
- 业务库实测值、行数和 mode 是时点证据，不作为永久定义；报告不得记录真实 DSN 或用户名。
- 2026-11-18 只读核验发现 `lhb_stock_detail.id` 实际 integer、DDL bigserial，以及 `sw_industry_class` 迁移遗留列顺序差异。正式修复另立任务，本字典只记录事实。

## 9. 证据与相关文档

- 静态表语义研究：`research/database-table-semantics-20261118` 分支的 `docs/research/2026-11-18-database-table-semantics.md`（issue #190）。
- 业务库只读核验：`research/database-live-schema-20261118` 分支的 `docs/research/2026-11-18-database-live-schema.md`（issue #191）。
- 分类决策：issue #192；词汇同步到根 `CONTEXT.md`。
- 决策原因：ADR 0003（结论）、0006/0008（单位）、0007（错误语义）、0012（涨停与强势股）、0014–0017（龙虎榜）、0018（复权）、0019（baostock 租约）。
- 操作手册：`docs/daily-source-cli.md`、`docs/index-source-cli.md`、`docs/sw-boards-cli.md`、`docs/lhb-source-cli.md`、`docs/adj-backfill-cli.md`、`docs/adj-switch-cli.md`。
