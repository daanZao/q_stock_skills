# 数据库字典

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

“代码存在历史入口”和“已经做过同口径重取验证”是两件事。速查表中的“未专项验证”表示本次字典工作没有执行该表的同日期重取或生产重放；既有单元测试和上游调研仍按其各自样本有效，不能外推长期一致性。

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

| 表 | 分类 | 日期轴 | 获取方式 | 一致性验证 |
| --- | --- | --- | --- | --- |
| `stock_daily` | 原始事实（混合） | `trade_date` | 按日期重取，受口径/修订影响 | 未专项验证 |
| `stock_daily_legacy` | 原始事实（混合）；归档 | `trade_date` | 精确整体只能备份恢复 | 未专项验证 |
| `index_daily` | 原始事实（混合） | `trade_date` | 按日期重取，T+0/补抓可修订 | 未专项验证 |
| `market_indices` | 原始事实（混合） | `trade_date` | 仅取当前 | 未专项验证 |
| `market_snapshot` | 原始事实（混合） | `trade_date` | 仅取当前 | 未专项验证 |
| `stock_daily_raw` | 原始事实（混合） | `trade_date` | 按日期重取，允许修订 | 未专项验证 |
| `stock_adjust_factor` | 原始事实 | `ex_date` | 按日期重取，允许补录/撤销 | 未专项验证 |
| `stock_daily_hfq` | 派生结果 | `trade_date` | 本地重算 | 未做生产重放 |
| `stock_factor_state` | 系统记录（含派生锚点） | `latest_trade_date` | 本地重算 + 控制恢复 | 未做生产重放 |
| `stock_adjustment_switch_state` | 系统记录 | 无业务日期轴 | 只能备份恢复/受控迁移 | 未做生产重放 |
| `market_boards` | 原始事实（混合，含留档） | `trade_date` | trend 按日期重取；current 仅取当前 | 未专项验证 |
| `zt_pool` | 原始事实（混合） | `trade_date` | 可按日期重取；深史待验证 | 限定样本，见明细 |
| `strong_stocks` | 派生结果 | `trade_date` | 本地重算 | 未做生产重放 |
| `sw_board_valuation` | 原始事实（UD 为主） | `trade_date` | 按日期重取，T+1 修订口径 | 未专项验证 |
| `sw_industry_meta` | 原始事实（参考种子） | 无业务日期轴 | 固定种子本地重放 | 未做生产重放 |
| `sw_industry_class` | 原始事实（含 `em` 留档） | `effective_date` | 固定文件重放；在线文件仅当前版本 | 未做生产重放 |
| `lhb_basic` | 原始事实 + L 汇总列 | `trade_date` | 按日期重取，源/详情覆盖有边界 | 未专项验证 |
| `lhb_stock_detail` | 原始事实 + L 兼容列 | `trade_date` | 按日期重取，受市场覆盖影响 | 未专项验证 |
| `lhb_stock_statistic` | 派生结果 | `trade_date` | 本地重算 | 未做生产重放 |
| `lhb_yyb_capital` | 原始事实（上游累计排行） | `fetch_date` | 仅取当前 | 未专项验证 |
| `lhb_yyb_most` | 原始事实（上游当前排行） | `fetch_date` | 仅取当前 | 未专项验证 |
| `stock_list` | 原始事实（当前参考清单） | 无历史业务日期轴 | 仅取当前 | 未专项验证 |
| `news_items` | 原始事实 + 本地主体关联 | `publish_time` | 当前搜索；已存 raw 可再投影 | 未专项验证 |
| `conclusions` | 业务产物 | `trade_date` | 只能备份/原产物恢复 | 未专项验证 |
| `collection_log` | 系统记录 | `trade_date` + 起止时间 | 只能备份恢复 | 未专项验证 |

## 4. 逐表明细

### 4.1 个股、指数与复权

#### `stock_daily`

- **含义/键**：legacy 个股日线，键 `(stock_code, trade_date, adj)`；同一股票日期可有 qfq/hfq/none 多口径。
- **生命周期**：目标消费模型已决定废弃本表，不再作为新分析的事实源；但当前仍处迁移期。2026-11-18 只读核验 `stock_adjustment_switch_state.mode=legacy`，因此“已决定废弃”不等于已经停止消费、切读或删除物理表。
- **当前写入/来源**：legacy `query_daily` 自愈按腾讯 → 新浪 → baostock 链写入，其中 baostock 使用单票区间接口 `query_history_k_data_plus`；legacy morning 次晨通过 baostock 全市场单日接口 `query_daily_history_k_AStock` 写入；rollback 可从本地新模型修复三种口径。new 模式 morning 和 raw+B 持续采集不写本表。存量行还可能来自已退役来源，不能按当前链反推。
- **当前读方**：LegacyPriceReader 服务 query_daily、强势股和申万自算；daily-review、limit-up-analysis、SEPA 经 MCP query_daily 间接消费，indicator-tools 再消费 query_daily 的管道输出，并非 skills 直连 PG。
- **字段来源**：行情为 U，部分源的涨跌额、涨跌幅和振幅为 L；`adj/source/timestamps` 为 M。上游 qfq/hfq 不是 raw 原子价格；腾讯缺 `qfqday` 时可回落 `day`，legacy 仍按请求 adj 落库，因此行级 `adj='qfq'` 不能单独证明统一复权口径或锚点。
- **获取边界**：可按日期重取部分来源，但换源、复核和复权锚点会改变结果。2023 年以前 qfq 仍依赖 legacy，不能反推 raw；物理删除须待切读、历史边界和回滚依赖解除。

#### `stock_daily_legacy`（条件存在）

- **含义/键**：cutover 时由 `stock_daily` 改名形成的冻结留档，结构与业务键不变。
- **写/读**：cutover、rollback、roll-forward 控制表名；LegacyPriceReader 优先读取归档表。
- **字段来源**：继承 `stock_daily`，归档不会把旧 qfq 变成原子事实。
- **获取边界**：精确归档整体只能由备份或归档恢复；重新采集不能恢复原行集合、来源和当时的 qfq 锚点。

#### `index_daily`

- **含义/键**：7 个主要指数的发布点位日线，键 `(index_code, trade_date)`；不应用个股 qfq/hfq 复权模型，因此无 `adj`。指数自身的成分调整和除数修正属于指数编制规则，不能与个股复权混称。
- **角色**：长期历史、当日日终行情和分析窗口的事实源；目标 close 复盘的指数收盘、20/60 日窗口、相关性和量价均以此表为准。
- **何时写/来源**：init 建历史底座；`index` 按区间或当日补抓；morning 仅在尾行落后目标日时补抓。当前链为腾讯 → baostock，已覆盖目标日的尾行会直接跳过，不保证 baostock 次晨复核。
- **读方**：query_index_daily、daily-review 和指数覆盖核验。当前 SEPA 不读本表。
- **字段来源**：OHLCV、amount、turnover 为 U；腾讯涨跌和振幅为 L，baostock 部分字段为 U 或 NULL；source 为 M。
- **获取边界**：按指数和日期重取，T+0、尾行补抓和换源可修订；upsert 不清理上游撤掉的历史行。

#### `market_indices`

- **含义/键**：指数实时/收盘截面，键 `(trade_date, index_code)`；不是指数历史日线，也不要求连续交易日覆盖。
- **角色**：盘中报价、原始量比和当前 SEPA 降级快照。当前生产适配器只取 7 大指数，旧东财路径曾留下更广泛指数代码，不能把存量代码数当作当前采集 Universe。
- **何时写/来源**：close 或 `fetch_board_snapshot(indices)` 触发，当前链为腾讯批量报价 → 新浪，并含主要指数白名单补漏。
- **读方**：当前 daily-review、SEPA 指数降级和 query_board_data。
- **字段来源**：腾讯行情和量比为 U/UD，新浪部分涨跌字段为 L；本表没有行级 source。
- **获取边界**：仅取当前，历史日期被实时 section 防线拒绝。目标 close 不消费本表，其当日 0 行不得被用来判定 `index_daily` 或 close 指数数据缺失。
- **覆盖差异示例（2026-11-18 只读核验）**：000905 在 `index_daily` 有 5,311 行（20050104–20261117），在 `market_indices` 只有 5 个采集日（20261009–20261019）；5 个重叠日收盘点位全部一致，成交额仅小数舍入差。这是历史日线与有限快照采集范围不同，不是同一数据集的行数冲突。

#### `market_snapshot`

- **含义/键**：持久化的全市场个股时点截面，键 `(trade_date, stock_code)`，含行情、PE/PB 和市值。“快照”指数据时效短，不表示数据库临时表；已落库历史行会持续保存，但只代表当时取得的截面。
- **角色**：目标消费限定为盘中/午市时点分析及明确的快照增强，不作为 close 复盘的事实源或就绪前置。close 所需广度、成交额和前 75 应从全市场个股日线派生。
- **何时写/来源**：snapshot、init 或 fetch_market_snapshot 触发；当前链为腾讯批量报价 → 腾讯 rank 榜单 → 新浪直连。主源需股票清单，后备源字段集不同。
- **当前读方**：query_snapshot、当前 daily-review 与 limit-up-analysis；close 当前仍用其做广度、成交额和前 75，涨停子技能用市值/PE 做启发式归因，这些是当前实现而非目标消费。
- **字段来源**：行情、估值、市值为 U/UD；广度和成交额聚合在查询时计算，不是本表物化列。
- **获取边界**：仅取当前；API 日期与请求日期不符即拒收。目标 close 的就绪检查不查询本表，其当日 0 行不得判为 close 数据缺失；市值/PE 等无法从日线派生的增强项必须删除、独立降级或另找事实源。

#### `stock_daily_raw`

- **含义/键**：新模型个股不复权日线输入事实，键 `(stock_code, trade_date)`；D-1 可仅作基线计算输入。
- **何时写/来源**：daily、morning、adj-backfill、init 和 new 查询自愈写入。全市场只用 baostock；腾讯/新浪只补单票 raw，后续 baostock 可覆盖升级。
- **读方**：NewPriceReader、审计、切换与回滚。
- **字段来源**：baostock OHLC、preclose、量额、pctChg、tradestatus、turn 为 U；替代源部分涨跌字段为 L；source 为 M。
- **获取边界**：按日期重取并支持修订，替代源字段集不同。preclose 是除权参考价，不恒等于上一 raw close；请求失败不能当空日。

#### `stock_adjust_factor`

- **含义/键**：每股除权日累计 B 事件证据，键 `(stock_code, ex_date)`。
- **何时写/来源**：daily、morning、adj-backfill、init 和 new 查询自愈从 baostock 逐日因子接口写入；成功重查可整日替换事件集。
- **读方**：rebuild_stock、NewPriceReader、审计和切换。
- **字段来源**：`factor_cum` 只取 baostock `backAdjustFactor`，`adjustFacto(r)` 不参与派生；kind/detail/reg_date 可空。
- **获取边界**：可按日期重取，但补录、改期和撤销会修订下游。事件表本身不能证明“无事件”，还需全窗口逐日成功/空日台账。

#### `stock_daily_hfq`

- **含义/键**：本地物化后复权日线，键 `(stock_code, trade_date)`。
- **写/读**：rebuild_stock 写；NewPriceReader、审计、切换和回滚读。
- **字段来源**：OHLC 和 `factor_cum=F` 为 L，量额、换手、停牌状态复制 raw；qfq 不落表，而由 hfq 按最新锚点读时派生。
- **重算条件**：raw、完整事件、可信 B0、代码版本和无未决异常必须固定。无窗口时删除该票全部 hfq；只有起点时删除起点及之后；有窗口时删除 `[derive_start, window_end]`，再插入本次有效行。新事件可重锚全历史 qfq。
- **一致性边界（2026-11-18 只读核验）**：已联接行满足 `hfq.close = raw.close × factor_cum`，公式偏差为 0，只能证明逐行计算一致。raw 为 4,797,979 行、hfq 为 4,573,186 行，32 个 state 仍 pending，不能宣称全量集合一致或完整。600519 在 20261116/20261117 的 raw close 为 1453.9/1457.7，hfq close 为 1453.9/11179.4759289；按同一 `factor_latest=7.669257` 派生的 qfq 约为 189.58/1457.7，仍存在跨日基准断层。因此当前证据不支持“raw 与 hfq 全量一致且派生结果正确”的结论。

#### `stock_factor_state`

- **含义/键**：每股复权锚点及重建控制状态，键 `stock_code`；`latest_trade_date` 是状态尾日。
- **写/读**：rebuild_stock 和事件撤销写；NewPriceReader、审计和切换读。
- **字段来源**：B0、F_latest 和尾日为 L；pending、重建时间为 M/控制值。
- **重算条件**：需要同一建锚权限、窗口和完整事实。state 按股票 upsert，不是逐日历史；窄窗重建不得清除无关 pending。无锚、pending 与 complete 不能互换；本表分类为系统记录，同时保存派生锚点。

#### `stock_adjustment_switch_state`

- **含义/键**：单行复权切读状态机，键固定 `id=1`，mode 为 legacy/new。
- **写/读**：adj-switch 的 cutover、rollback、roll-forward 写；状态报告和预检读。
- **字段来源**：全部为 M/控制值；数据库 mode 不会自动修改进程 `QSTOCK_PRICE_SOURCE`。
- **获取边界**：每次状态转换都有 mode 和物理表前置条件，不满足即报错，不是通用可重入动作。精确状态与时间戳只能靠本地记录和备份恢复。

### 4.2 板块、涨停与强势股

#### `market_boards`

- **含义/键**：持久化的**上游发布板块指数行情**，物理键 `(trade_date, board_type, board_name)`，**不是 board_code**。现役申万一级/二级发布口径与退役行业/概念留档可并存；本表不是成分股本地聚合结果表。
- **current 当日截面入库链**：`sw-boards` / close → `fetch_board_snapshot(boards)` → `SwSResearchAdapter.fetch_boards` → 申万 current → `map_sw_current_rows` → 通用 `upsert_board_rows`。`latest_price=官网 l8`、`pre_close=官网 l3`，本地计算 `change_percent=(l8-l3)/l3×100`、`change_amount=l8-l3`，振幅也由官网指数高/低/昨收计算；这些 L 列的输入是**官网指数点位**，不是成分股。代码：`adapters/sw_adapter.py:138-178`、`adapters/_sw_board.py:60-99`、`tools_board.py:284-292`。
- **trend 历史回补入库链**：`backfill --source sw` → `fetch_sw_board_history` → 申万 trend → `map_sw_trend_rows` → `upsert_board_history`。`change_percent=官网 markup`、`change_amount=官网 hike`，为 U；preclose 取上一行、振幅本地计算，为 L。代码：`adapters/sw_adapter.py:180-195`、`adapters/_sw_board.py:102-142`、`repository.py:749-765`。
- **source 的三个层级**：`SwSResearchAdapter.name='swsresearch'` 可出现在工具响应和采集台账，无论 current 还是 trend。表内 `market_boards.source` 则不同：current 经 `BOARD_FIELDS` 通用 upsert，字段集不含 source，故为 `NULL`；trend 由 `upsert_board_history` 显式写 `swsresearch`。手册冒烟中的“source=swsresearch”若指工具响应，不能据此推断表内 current 行也有该值。
- **与自算板块的区别**：`query_sw_board_daily` 是“申万成分字典 × 库内个股日线”的查询时成交额加权聚合，`basis=sw_amount_weighted`，不写 `market_boards`。它由 issue #75 建立、#92 修正横截面 adj；#105/#111/#118 定稿的是“申万官网 current/trend 为 boards 主源，自算保留作降级与交叉验证”。两条链不能混称。
- **读方**：query_board_data、daily-review 和 limit-up-analysis；当前 daily-review 以申万官网发布行为板块主口径，缺失时才按既有规则降级到 `query_sw_board_daily` 自算口径。
- **获取边界**：trend 可按日期重取，current 仅取当前。实时、回补和退役留档不能混成同口径；`NULL` 还可能包含旧东财实时留档，必须结合 `board_type`、业务日期和台账判断。upsert 不清理改名或消失板块。
- **时点证据（2026-11-19 只读核验）**：表内 `swsresearch` 的 industry_l1/l2 合计 16,120 行；`NULL` 的 concept/industry 合计 1,002 行且均为 20261013 留档。该分布只描述当前库，不改变上述 current/trend 代码契约。

#### `zt_pool`

- **含义/键**：涨停 zt、跌停 dt、炸板 zb 事件池，键 `(trade_date, pool_type, stock_code)`。这些事件是 close 复盘、涨停分析和强势股规则的必要输入；历史覆盖短或上游深史未验证，不构成删除该数据面的理由。
- **何时写/来源**：close、limitpool 和 fetch_board_snapshot 从 THS 涨停揭秘 dataapi 写入；每日每池一次、池间/分页间默认 20 秒退避。
- **读方**：limit-up-analysis、daily-review 和强势股自算。
- **字段来源**：价格、首封、板型、封单额、题材为 U/UD；连板数含本地解析。THS 不提供行业等字段，保持 NULL。
- **获取边界**：2026-10-20 THS dataapi 限定调研记录 4 天历史可取，这是接口样本，不是库内总覆盖或已证明的最大历史深度。2026-10-23 `qstock_test_120` 三次低频冒烟验证当日三池字段，并非深史验证；详见 ADR 0012。每日一次守卫使重跑可能故意零请求。
- **时点证据（2026-11-19 只读核验）**：业务库现有 1,713 行、20 个有事件记录的交易日期，范围 20260914–20261019；有行日期不等于区间内每日三池均已成功采集，完整性仍须结合交易日历与采集台账核验。
- **后续工作——THS 实测**：[调研：THS 涨停池历史覆盖与低频请求性能 #200](https://github.com/daanZao/q_stock_skills/issues/200) 验证三池历史边界、同日期重取一致性，以及限定样本串行低频下的时延、分页和成功率；不主动做并发/压力测试，未验证的上游 TPS 与并发上限保持“待验证”。
- **后续工作——日线自算**：[Spec（待核实）：由个股日线派生收盘涨跌停与疑似炸板候选 #201](https://github.com/daanZao/q_stock_skills/issues/201) 研究以 `stock_daily_raw` 和当日有效交易制度判定收盘涨停/跌停，并以日内高/低价触及涨跌停价但收盘未封识别疑似炸板。该路线是 L 派生结果，不能提供首封时间、封单额、炸板次数或题材；存储口径由 Spec 决定，未决前不得直接混入或覆盖本表的 THS 原始事实。

#### `strong_stocks`

- **含义/键**：本地规则选股池，键 `(trade_date, stock_code)`；规则为 60 交易日新高 ∪ 近 10 交易日涨停至少 2 次。
- **写/读**：close、limitpool 自算并写；daily-review 和 limit-up-analysis 读，无外部强势股上游。
- **字段来源**：入选集合和 reason 为 L；行情、连板和名称复制其他事实；市值和行业无真源保持 NULL。
- **时点证据（2026-11-19 只读核验）**：业务库现有 3,193 行、18 个有结果日期，范围 20260915–20261019；该分布不证明区间内逐日完整。
- **重算条件**：需要目标日的完整价格事实、60 交易日价格窗口、10 交易日 `zt_pool` 窗口和 `stock_list`。`zt_pool` 无行必须结合采集台账区分空池与未采集，否则“近 10 日多次涨停”分支不能判为完整。当前写路径只 upsert、不清修订后落选旧行，因此从空表重算与旧表重跑必须分开验证。
- **后续工作——历史回补**：[Spec（待核实）：strong_stocks 历史回补与整窗重放 #203](https://github.com/daanZao/q_stock_skills/issues/203) 将其纳入首次使用/历史窗口编排。当前 `backfill --source sw` 只承载申万板块，尚无本表历史重放入口；后续应复用自算规则、保持零上游请求，在输入完整后按日原子替换并清理落选旧行。缺事实时报告缺口，不自动转为采集，也不只计算单一分支冒充完整结果。

#### `sw_board_valuation`

- **含义/键**：申万双级板块每日估值报表，键 `(trade_date, board_code)`，T+1 获取。
- **何时写/来源**：morning 从申万 analysis report 写入；query_board_data 和次日修订分析读取，当前收盘版不作为当晚必需输入。
- **字段来源**：收盘指数、换手、PE/PB、流通市值和股息率为 U/UD，金额换算为万元。
- **获取边界**：可按日期区间重取，但属于 T+1 修订口径，不能与 current/trend 混作同字段版本。

#### `sw_industry_meta`

- **含义/键**：申万行业代码、名称和级别参考表，键 `industry_code`。
- **何时写/来源**：import_sw_industry_meta 从仓库内置 CSV 种子写入；自算板块和 coverage 读取。
- **字段来源**：来自固定种子，不是某日官网快照；种子合并多版本，新版名称优先。
- **获取边界**：同一 commit 的种子可本地重放；跨版本集合可变化，upsert 不删除种子已移除代码。

#### `sw_industry_class`

- **含义/键**：股票到行业的生效日期变更史，键 `(source_system, stock_code, board_code, effective_date)`。
- **何时写/来源**：固定文件导入，或 sw-dict 下载申万当前 xls 后按 source_system 整体替换；自算板块和行业归属读取 point-in-time 版本。
- **字段来源**：sw 来自申万 xls，代码和日期经本地映射；em 为退役留档。
- **获取边界**：固定同一文件可确定重放并清陈旧行；在线 URL 只提供当前发布版本，不能恢复过去某次文件版本。

### 4.3 龙虎榜

#### `lhb_basic`

- **含义/键**：按上榜原因拆分的龙虎榜父事件，键 `(trade_date, stock_code, lhb_reason)`；同股同日可多原因。
- **何时写/来源**：close、单日 lhb 和 fetch_board_snapshot 从交易所官方披露写入；历史回溯中深圳默认 THS 主采、官方补缺，其他市场按既有后备规则处理。
- **读方**：daily-review、statistic 和 coverage。
- **字段来源**：代码、原因和可得行情为 U；buy/sell 是买卖前五席位的 L 汇总，net=buy−sell、total=buy+sell，均为 L。
- **获取边界**：在上游覆盖范围内按日期重取；换源、详情子集和保留策略会影响汇总列。整表不能标为本地重算。

#### `lhb_stock_detail`

- **含义/键**：basic 父事件下的席位记录，代理键 id + 外键 basic_id；逻辑父键同 basic，席位行无物理业务唯一键。
- **何时写/来源**：与 basic 同一 LHB 链写，深圳可取 THS 同页或交易所明细，上海取交易所文件，按父组替换。
- **读方**：席位分析和详情修复。
- **字段来源**：席位、方向和可得金额为 U；`amount` 为方向金额的 L 兼容列。官方未提供的字段保持 NULL。
- **获取边界**：可按日期重取，但受父事件、市场覆盖和详情保留规则约束；北交所、非 A 股和未知代码不强行补席位。

#### `lhb_stock_statistic`

- **含义/键**：按股票物化的近 **60 日历日** LHB 聚合快照，键 `(trade_date, stock_code)`。
- **写/读**：basic 写入后从本地 basic 重算；daily-review、lhb --check 和 coverage 读，无独立统计上游。
- **字段来源**：全部为 L/M；`appear_count_3m` 实际是窗口 basic 行数，同股多原因分别计，不是自然三个月。
- **重算条件**：目标日前 60 日历日 basic 必须完整；当日 DELETE+INSERT 可随窗口和修订自愈，缺输入时不能把短窗结果当完整统计。

#### `lhb_yyb_capital`

- **含义/键**：同花顺营业部当前累计资金榜快照，键 `(fetch_date, seat_name)`。
- **何时写/来源**：close 和单日 lhb 经 akshare `stock_lh_yyb_capital` 写入；当前 daily-review 读取，历史 LHB 回溯不写。
- **字段来源**：排名、席位和累计金额为 U/UD；卖出、净额和均额无来源保持 NULL。
- **获取边界**：接口无日期参数，仅取当前；fetch_date 不揭示上游统计窗口，upsert 不清已消失席位。

#### `lhb_yyb_most`

- **含义/键**：同花顺营业部当前上榜排行，键 `(fetch_date, seat_name)`。
- **何时写/来源**：close 和单日 lhb 经 akshare `stock_lh_yyb_most` 写入；当前 daily-review 读取，历史 LHB 回溯不写。
- **字段来源**：排名、上榜次数和年内买入股票只数为 U/UD；`buy_amount` 实为“合计动用资金”（买+卖），不是纯买入。
- **获取边界**：仅取当前；upsert 不清已消失席位，也不能由 fetch_date 推断统计窗口。

### 4.4 清单、资讯、结论与台账

#### `stock_list`

- **含义/键**：当前 A 股代码和名称清单，键 `stock_code`，没有历史 Universe 或上市/退市生效轴。
- **何时写/来源**：init 和 stock-list 触发，当前链为 akshare 交易所官网清单 → baostock；两者市场覆盖能力不同。
- **读方**：全量回溯、快照清单、coverage 和名称补充。
- **字段来源**：代码名称为 U，source/updated_at 为 M。
- **获取边界**：仅取当前；按 code upsert 不删除缺席代码，不能重建历史 Universe。

#### `news_items`

- **含义/键**：资讯按市场/股票主体沉淀，键 `(news_code, subject_type, subject_code)`；同一资讯挂多主体会产生多行。
- **何时写/来源**：daily-review、limit-up-analysis 或调用方执行 mx_search 时，从妙想当前搜索结果写入；query_news 和 daily-review、limit-up-analysis 读取。当前 SEPA 不读本表。
- **字段来源**：内容和发布时间为 U，主体关联为调用方输入，fallback 键和时间解析含 L/M。
- **获取边界**：搜索是问句驱动的当前结果，同问句不保证同集合。已存 raw 可再投影，不能恢复未曾搜到的资讯或历史排名。

#### `conclusions`

- **含义/键**：agent 生成的 JSON 结论，键 `(subject_type, subject_code, trade_date, conclusion_type)`。
- **何时写/读**：skill 调 save_conclusion 写入；query_conclusions 和 skill 间引用读取。payload 结构由 skill 自报，server 不校验语义。
- **字段来源**：主体、业务日、类型和 payload 都是本地业务产物；created/updated 为 M。
- **获取边界**：同键 upsert 会覆盖旧 payload 且无版本历史；可重复保存相同 JSON，但不能从证券上游重建 agent 文本和判断。

#### `collection_log`

- **含义/键**：采集、导入、自算、拒收、计划缺席和复权修复的 append-only 台账，仅代理 id，无业务唯一键。
- **何时写/读**：各写路径执行动作时记录；就绪归因、断点续跑、复权完备性和 THS 低频守卫读取。
- **字段来源**：section/status/source/rows/error_kind/detail 都是一次动作的 M 摘要，detail 不是原始响应。
- **获取边界**：故意非幂等，每次动作新增行；台账可能缺行且历史 repair 不等于当前 pending，精确运行史只能由本地备份恢复。

## 5. 每日复盘：当前实现与目标消费

> 本节是已确认的目标使用边界，不表示当前 skill 已完成改造。后续实施从[数据库字典地图 #189](https://github.com/daanZao/q_stock_skills/issues/189)另起 issue；本字典不代替实施验收。

| 逻辑事实 | 当前实现 | 目标消费 |
| --- | --- | --- |
| legacy 个股日线 | skills 经 LegacyPriceReader 消费 `stock_daily`；legacy morning 与 rollback 仍写入，switch 当前为 legacy | `stock_daily` 已决定废弃；完成切读后只保留归档，不再作为分析事实源，新分析使用 raw+B 及受控派生 |
| 指数收盘与历史 | 当日读 `market_indices`，历史读 `index_daily`，因此快照缺失会被误判为指数未就绪 | close 统一以 `index_daily` 为指数事实；就绪检查查当日行及所需历史窗口，`market_indices` 0 行不参与判定；量比另定派生或删除 |
| 市场广度与成交额 | `market_snapshot` 聚合，快照缺失会阻断 close | 从全市场个股日线事实本地聚合；`market_snapshot` 0 行不参与判定 |
| 成交额前 75 | `market_snapshot` 排序 | 从个股日线事实排序，`stock_list` 补名称，不依赖快照表 |
| 个股时点截面 | close 将 `market_snapshot` 当核心输入 | `market_snapshot` 仅用于盘中/午市时点分析和明确增强，不进入 close 数据源或就绪检查 |
| 指数实时截面 | close 必需 `market_indices` | 不进入 close；midday 如保留则单独定义 |
| 涨停/连板 | `zt_pool` + 物化 `strong_stocks` + `limit_up.close` | zt_pool 是事件事实；强势股和 limit_up 是派生结果，不冒充独立上游 |
| 龙虎榜 | basic + statistic + 两个 yyb 累计榜 | basic/detail 是按日期事实；统计和排行目标为查询时派生 |
| 市值/PE 归因 | limit-up-analysis 读 `market_snapshot` | 删除、独立增强，或另找明确基本面/股本事实；不得保留隐藏快照依赖 |
| 资讯 | `news_items` 可选 | 保持可选，不作为行情数字来源 |

## 6. Skill 消费导航

| 使用方 | 当前入口 | 数据使用规则 |
| --- | --- | --- |
| **daily-review** | MCP query/fetch + indicator-tools 管道 | 读取逻辑事实，不直连 PG。历史模式禁用 fetch；目标 close 的就绪检查以 `index_daily` 当日行和窗口、全市场个股日线及必要板块/事件事实为准，不以 `market_indices` 或 `market_snapshot` 是否采集为门槛，报告必须标注实际数据口径。 |
| **limit-up-analysis** | MCP query/fetch + promote/deviation 管道 | zt_pool 是核心事件事实；strong、梯队、晋级率和营业部统计是派生结果。市值/PE 快照依赖需单独收口。 |
| **SEPA** | query_daily、基本面透传、market_indices 降级、会话资讯 | 个股价格和复权经统一 reader；当前没有 query_index_daily/query_news 入口，指数快照近似必须标注降级。 |
| **indicator-tools** | stdin/stdout 管道 | 不访问数据库，不物化指标。输入契约由 `query_daily` 等工具输出和技能文档共同维护。 |

## 7. 常见误用

1. 用 `market_snapshot` 或 `market_indices` 请求历史日期；把 `market_snapshot` 当物理临时表，或把它当 close 必需输入并因当日 0 行误判复盘数据缺失。
2. 把 `index_daily` 与 `market_indices` 混称为同一指数行情，比较两表总行数判断冲突，或因 `market_indices` 当日 0 行而忽略 `index_daily` 已就绪、误判 close 指数数据缺失。
3. 把业务键 upsert 当成整日集合替换、历史可重取或本地可重算。
4. 把 `market_boards.board_code` 当物理唯一键；混合申万双级与退役行业/概念口径；把申万官网发布行当本地自算，或把 `query_sw_board_daily` 自算结果当 `market_boards` 落库行；只用 `source='swsresearch'` 判断全部申万官网数据。
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

- [静态表语义研究](https://github.com/daanZao/q_stock_skills/blob/research/database-table-semantics-20261118/docs/research/2026-11-18-database-table-semantics.md)（issue #190）。
- [业务库只读核验](https://github.com/daanZao/q_stock_skills/blob/research/database-live-schema-20261118/docs/research/2026-11-18-database-live-schema.md)（issue #191）。
- 分类决策：issue #192；词汇同步到根 `CONTEXT.md`。
- 决策原因：ADR 0003（结论）、0006/0008（单位）、0007（错误语义）、0012（涨停与强势股）、0014–0017（龙虎榜）、0018（复权）、0019（baostock 租约）、[0020](adr/0020-sw-boards-published-and-selfcalc.md)（申万官网发布、自算与 source 层级）。
- 操作手册：`docs/daily-source-cli.md`、`docs/index-source-cli.md`、`docs/sw-boards-cli.md`、`docs/lhb-source-cli.md`、`docs/adj-backfill-cli.md`、`docs/adj-switch-cli.md`。
