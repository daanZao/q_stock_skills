# 24 张表的业务语义、写方读方与可重复获取性（#190）

日期：2026-11-18。基线：`9868ddd89e8779742f7fbe9929491f5db0a40080`；分支：
`research/database-table-semantics-20261118`。父地图：#189；下游分类决策与字典成稿：
#192/#193。

## 0. 范围、方法与结论边界

本报告以当前源码、`src/qstock_mcp/sql/001-020`、适配器、MCP/CLI、四个交付 skill、
ADR 和操作手册为一手证据。静态脚本从 DDL 的 `CREATE TABLE` 识别到 **24 张表**；
另将 `stock_daily` cutover 后条件存在的 `stock_daily_legacy` 单列为 **+1**。DDL 清单见
`src/qstock_mcp/sql/001_stock_daily.sql:4-31`、
`src/qstock_mcp/sql/002_market_snapshot.sql:3-29` 至
`src/qstock_mcp/sql/020_adjustment_switch.sql:3-13`，逐表证据见 §2。

本轮只读源码和 GitHub issue，**证券数据上游请求为 0；未连接任何数据库，未执行 SQL、
迁移、采集或测试，未修改业务代码**。历史深度和修订行为若只由代码路径证明，均写成
“入口存在/待重取验证”，不外推长期可用性或全市场完备率。旧研究只用于理解既有决策，
不作为本轮实测证据。

最重要的实现事实：

1. **“表是事实表”与“所有列都是原子字段”是两回事。** 例如 `lhb_basic` 的上榜事件是
   采集事实，但买/卖/净额/总额由席位明细汇总；指数、板块、个股日线也存在本地计算的
   涨跌额、涨跌幅、振幅（`src/qstock_mcp/adapters/lhb_exchange_adapter.py:85-105`、
   `src/qstock_mcp/adapters/_daily_common.py:25-44`、
   `src/qstock_mcp/adapters/_sw_board.py:60-99`）。
2. **`market_snapshot` 与 `market_indices` 都是实时截面，不是可历史回补的日线。**
   前者有独立日期相符防线，后者属 `REALTIME_SECTIONS`，传历史日期会拒收；真正的指数
   历史在 `index_daily`（`src/qstock_mcp/tools_snapshot.py:105-132`、
   `src/qstock_mcp/tools_board.py:594-613`、
   `src/qstock_mcp/tools_index.py:58-71`）。
3. **复权四表不是四个同类事实表。** `stock_daily_raw` 与 `stock_adjust_factor` 是可修订
   输入事实，`stock_daily_hfq` 是本地物化派生，`stock_factor_state` 同时保存派生锚点和
   完备性控制状态；qfq 没有第五张表，而是读时按最新锚点除以 `factor_latest`
   （`src/qstock_mcp/adjustment.py:398-470`、`src/qstock_mcp/price_reader.py:197-262`）。
4. **`strong_stocks` 是本地派生，不是外部强势股池。** 其输入为价格事实、60 交易日窗口、
   近 10 交易日 `zt_pool` 和 `stock_list`；但当前落库只 upsert，不删除修订后应落选的旧行，
   所以“计算函数确定”不等于“在任意旧表上重跑即完全收敛”
   （`src/qstock_mcp/tools_strong.py:87-140`、`src/qstock_mcp/tools_board.py:229-264`）。
5. **LHB 五表分属三种来源。** basic/detail 是上榜与席位采集记录（basic 含汇总派生列），
   statistic 是 basic 的近 60 **日历日**本地物化统计，yyb 两表是执行日抓取的同花顺当前
   累计排行，没有历史日期参数（`src/qstock_mcp/repository.py:582-701`、
   `src/qstock_mcp/adapters/_lhb_yyb.py:15-49`、`docs/lhb-source-cli.md:180-182`）。
6. **审计、控制、业务产物不能用“再抓一次”恢复。** `collection_log` 是 append-only 运行
   证据，`conclusions` 是 agent 产物且同键覆盖，`stock_adjustment_switch_state` 是切换
   状态机；它们的写入可重复或动作可重入，并不代表历史内容可由上游重建
   （`src/qstock_mcp/ledger.py:28-32,60-100`、
   `src/qstock_mcp/repository.py:861-919`、
   `src/qstock_mcp/adjustment_switch.py:135-224`）。

## 1. 阅读约定：实现事实与候选建议分开

§2 只描述当前实现。字段来源缩写：

- **U**：上游直接提供的标识、时间、价格、数量或文本；单位换算、代码归一化不改变其来源。
- **UD**：上游已经计算/分类的字段，例如 PE、榜单、涨停题材、排行；它是“上游发布口径的
  事实”，但不是更底层的原子观测。
- **L**：本项目由其他字段或其他表计算/复制；包括适配器内计算，不限于 SQL 聚合。
- **M**：本地元数据，例如 `source`、创建/更新/抓取时间、pending、审计摘要。

“业务键”优先指唯一约束/主键；没有物理唯一键时另写“逻辑父键”。日期轴区分交易日、
事件生效日、发布时间、执行日和切换时间，不能都当作 `trade_date`。

§3 才提出分类与可重复获取候选。这里特别区分三个动词：

- **幂等写入**：同一输入重复写不产生重复业务行；可能是 upsert、整日替换或父组替换。
- **可重取**：还能从上游取得目标日/目标对象的数据；受历史深度、源口径、修订、封禁和
  当前快照限制。
- **可重算**：给定完整且固定的本地输入与代码版本，能确定地生成结果；若不清陈旧行，
  仍不保证旧表重跑后的集合与空库重建相同。

## 2. 当前实现事实：24+1 逐表盘点

### 2.1 `stock_daily`

- **含义、键、日期**：legacy 个股日线，同一股票同一交易日可按 `adj=qfq/hfq/none` 存多口径；
  键 `(stock_code, trade_date, adj)`，日期轴 `trade_date`。cutover 后整表改名归档，DDL 不再
  创建空的新表（`src/qstock_mcp/sql/001_stock_daily.sql:1-31`）。
- **字段来源**：OHLCV、amount、turnover、tradestatus 为 U/M（随源而异）；腾讯/新浪的
  `change_amount/change_percent/amplitude` 是 L，baostock 的 `pctChg` 是 U，但其振幅和
  涨跌额为 NULL；`adj/source/timestamps` 是口径与 M。legacy qfq/hfq 还可能是上游复权序列，
  并非 raw 原子价格（`src/qstock_mcp/adapters/_daily_common.py:25-44`、
  `src/qstock_mcp/adapters/baostock_adapter.py:59-81`）。
- **写方**：`query_daily` 在 LegacyPriceReader 下的自愈及 legacy 模式 `morning` 可写；
  当前 `fetch_daily` 和 NewPriceReader 新窗口自愈统一走 raw+B，不按 `--adj` 写三种 legacy 口径。
  回滚修复可从 raw/hfq/state 重建 none/hfq/qfq（`src/qstock_mcp/tools_daily.py:200-245,256-296,378-394`、
  `src/qstock_mcp/tools_morning.py:51-79`、`src/qstock_mcp/adjustment_switch.py:45-83`）。
- **读方**：`LegacyPriceReader` 服务 `query_daily`、覆盖核验、强势股与申万自算；SEPA、
  daily-review、limit-up-analysis 和 indicator-tools 均通过 MCP/管道间接消费，不直连表
  （`src/qstock_mcp/price_reader.py:9-140`、`skills/sepa/SKILL.md:16-27`、
  `skills/indicator-tools/SKILL.md:8-18`）。
- **写入与重取边界**：业务键 upsert 可覆盖同键，但不能恢复某一历史行的原始来源、旧版本或
  已消失行。2023 年以前 qfq 仍只读 legacy；新模型拒绝跨边界混排，也不从旧 qfq 反推 raw/hfq
  （`src/qstock_mcp/price_reader.py:168-195`）。

### 2.2 `market_snapshot`

- **含义、键、日期**：全市场个股实时/收盘截面，含行情、估值和市值；键
  `(trade_date, stock_code)`，日期是 API 生效日而非可任意指定的历史日
  （`src/qstock_mcp/sql/002_market_snapshot.sql:1-29`）。
- **字段来源**：代码、名称、OHLC、pre_close、量额、换手、估值、市值为 U/UD，经单位换算；
  不同源的字段集不同：腾讯 rank 缺 OHLC/pre_close，新浪缺振幅/量比。表内不持久化本地
  广度聚合；广度/总额在查询时计算（`src/qstock_mcp/adapters/tencent_adapter.py:7-21`、
  `src/qstock_mcp/repository.py:207-280`）。
- **写方**：MCP `fetch_market_snapshot`、init 轻量初始化、CLI `snapshot`；生产链为腾讯批量
  报价→腾讯 rank→新浪直连（`src/qstock_mcp/adapters/__init__.py:76-89`、
  `src/qstock_mcp/tools_snapshot.py:50-60`）。
- **读方**：MCP `query_snapshot`；daily-review 用作广度、成交额及 top 75，
  limit-up-analysis 用作大小票/业绩粗分输入；CLI `coverage/parity` 的相应检查只读或对拍
  （`src/qstock_mcp/tools_snapshot.py:169-255`、`skills/daily-review/SKILL.md:28-49`、
  `skills/limit-up-analysis/SKILL.md:24-26`）。
- **写入与重取边界**：按日股 upsert；请求日必须等于 API 生效日，否则零业务落库。接口没有
  历史日期输入，旧快照只能由当时持续落库保留；重抓较少行也不会清除本日多余旧代码
  （`src/qstock_mcp/tools_snapshot.py:105-143`、`src/qstock_mcp/repository.py:109-157`）。

### 2.3 `market_indices`

- **含义、键、日期**：主要指数实时/收盘截面；键 `(trade_date, index_code)`，日期轴为行情
  时间戳对应的交易日（`src/qstock_mcp/sql/003_market_indices.sql:1-24`）。
- **字段来源**：腾讯路径各行情字段（含量比）为 U/UD；新浪路径的涨跌额/涨跌幅/振幅为 L，
  量比恒 NULL。成交量/成交额按源换算到股/万元
  （`src/qstock_mcp/adapters/tencent_adapter.py:297-335`、
  `src/qstock_mcp/adapters/sina_adapter.py:78-135`）。
- **写方**：MCP `fetch_board_snapshot(indices)`、CLI `close`；腾讯→新浪，另有七指数白名单
  定点补漏（`src/qstock_mcp/tools_board.py:614-646,719-747`）。
- **读方**：MCP `query_board_data`；当前 daily-review 逐七代码读收盘截面，SEPA 在缺指数
  长历史时作降级近似；CLI `parity/coverage` 可读
  （`src/qstock_mcp/repository.py:768-781`、`skills/daily-review/SKILL.md:37-50`、
  `skills/sepa/SKILL.md:27`）。
- **写入与重取边界**：业务键 upsert，但 indices 属实时 section，历史日期拒收；行级日期与
  请求日不符也整日拒收。因此 `index_daily` 的历史能力不能归到本表
  （`src/qstock_mcp/tools_board.py:594-613,721-747`）。

### 2.4 `market_boards`

- **含义、键、日期**：行业/概念板块行情表，当前申万双级与退役东财存量并存；物理键
  `(trade_date, board_type, board_name)`，**不是 board_code**。`source` 区分实时 NULL、
  申万回补 `swsresearch` 和退役 `push2his` 留档（`src/qstock_mcp/sql/004_market_boards.sql:1-29`、
  `src/qstock_mcp/sql/015_market_boards_source.sql:1-5`、
  `src/qstock_mcp/tools_backfill.py:49-50,95-106`）。
- **字段来源**：申万 current 的点位、OHLC、量额为 U，涨跌额/涨跌幅/振幅为 L；trend 的
  涨跌额/涨跌幅为 U，pre_close 取上一行且振幅为 L。量比、stock_count、领涨字段为 NULL；
  退役概念行是历史留档（`src/qstock_mcp/adapters/_sw_board.py:36-43,60-142`）。
- **写方**：MCP/CLI `fetch_board_snapshot(boards)`、`close`、`sw-boards` 写当前截面；
  CLI `backfill --source sw` 经 trend 写 1999 起历史。东财写路径和概念成分积累已移除
  （`src/qstock_mcp/tools_backfill.py:1-20,87-107`）。
- **读方**：MCP `query_board_data`；daily-review 当前消费申万当日及 5/10 日历史，
  limit-up-analysis 用作行业背景；CLI `sw-boards --check` 与 `coverage` 只读。自算板块行情
  `query_sw_board_daily` 不写本表（`skills/daily-review/SKILL.md:50-52,136-150`、
  `src/qstock_mcp/price_reader.py:107-140`）。
- **写入与重取边界**：实时 boards 历史日期拒收；trend 有独立 CLI 历史入口。默认断点续跑
  会跳过同窗口已有 ok 台账，`--force` 才整窗重抓。按名称 upsert 不做整日删除，板块改名或
  上游集合缩减可能留下旧行；实时、回补、退役行不能混称同口径
  （`src/qstock_mcp/tools_backfill.py:150-161,193-209`、
  `src/qstock_mcp/repository.py:731-765`）。

### 2.5 `zt_pool`

- **含义、键、日期**：涨停 `zt`、跌停 `dt`、炸板 `zb` 三池事件快照；键
  `(trade_date, pool_type, stock_code)`（`src/qstock_mcp/sql/005_zt_pool.sql:1-27`）。
- **字段来源**：价格、首封时间、板型、连板、封单额、题材为 U/UD，经时间/金额/连板文本
  映射；`consecutive_boards` 是上游 `high_days` 的本地解析。THS 不提供行业、量额、跌停价
  等字段，相应列为 NULL；`sealed_amount/reason_type` 由 017 后加
  （`docs/adr/0012-zt-pool-ths-strong-stocks-selfcalc.md:20-55`、
  `src/qstock_mcp/sql/017_zt_pool_ths_columns.sql:1-11`）。
- **写方**：MCP `fetch_board_snapshot(zt_pool)`、CLI `close/limitpool`；THS 单源、每日每池
  一次、无同源自动重试（`src/qstock_mcp/tools_board.py:157-226`）。
- **读方**：MCP `query_board_data`；limit-up-analysis 的梯队、晋级率、封单和题材，
  daily-review 的涨跌停结构；同时是 `strong_stocks` 本地计算输入。CLI `parity` 读连续性
  （`skills/limit-up-analysis/SKILL.md:22-50`、`src/qstock_mcp/tools_strong.py:48-71`）。
- **写入与重取边界**：业务键 upsert，整日不删除旧行；每日一次守卫使“重跑命令”常常故意
  零上游请求。THS 有 date 参数，但仅实测 4 天历史可取，更深历史、修订与同口径稳定性待验证
  （`src/qstock_mcp/adapters/ths_adapter.py:21-32`、
  `docs/adr/0012-zt-pool-ths-strong-stocks-selfcalc.md:89-97`）。

### 2.6 `strong_stocks`

- **含义、键、日期**：本地规则选股池；键 `(trade_date, stock_code)`，交易日快照
  （`src/qstock_mcp/sql/006_strong_stocks.sql:1-21`）。
- **字段来源**：入选集合与 `reason` 为 L；当日价、涨跌、量额、换手复制统一价格接缝，
  连板数复制 `zt_pool`，名称补自 `stock_list`；`market_cap/industry` 无真源恒 NULL
  （`src/qstock_mcp/tools_strong.py:74-84,98-140`）。
- **写方**：生产 `fetch_board_snapshot(strong_stocks)`、CLI `close/limitpool`，调用
  `compute_strong_rows`；无上游强势股请求（`src/qstock_mcp/tools_board.py:229-264`）。
- **读方**：MCP `query_board_data`；daily-review 入选理由聚类，limit-up-analysis 强势/龙头
  分析（`skills/daily-review/SKILL.md:53,163`、`skills/limit-up-analysis/SKILL.md:24-25,44-50`）。
- **写入与重算边界**：规则为 60 交易日新高 ∪ 近 10 交易日涨停至少 2 次。缺当日价格事实时
  返回 None 并记 empty，而非空池重算。当前仅 upsert，无当日 DELETE；因此从空表重算确定，
  但对已有表重跑不能清除因价格/涨停输入修订而失效的成员
  （`src/qstock_mcp/tools_strong.py:87-116`、`src/qstock_mcp/tools_board.py:237-264`）。

### 2.7 `lhb_basic`

- **含义、键、日期**：股票按上榜原因拆分的龙虎榜父事件；键
  `(trade_date, stock_code, lhb_reason)`，同股同日在不同榜单可有多行
  （`src/qstock_mcp/sql/007_lhb.sql:4-32`）。
- **字段来源**：代码、名称、原因和可得行情为 U；官方路径 close/change 常缺，换手率仅从
  特定原因文本提取。`buy_amount/sell_amount` 是买卖前五席位的 L 合计，net=buy−sell、
  total=buy+sell；THS 历史页也提供/解析这些金额。故表级是采集事件，金额列不是独立原子事实
  （`src/qstock_mcp/adapters/lhb_exchange_adapter.py:8-17,85-110`、
  `src/qstock_mcp/adapters/lhb_ths_adapter.py:115-167`）。
- **写方**：MCP `fetch_board_snapshot(lhb)`、CLI `close/lhb`；单日官方源，深圳历史默认 THS
  主采、官方补缺，沪深北有显式后备规则（`src/qstock_mcp/adapters/__init__.py:159-180`、
  `docs/adr/0016-szse-history-lhb-ths-primary.md:11-18`）。
- **读方**：MCP `query_board_data`；daily-review 龙虎榜节；statistic 的唯一直接业务输入；
  CLI `lhb --check/coverage` 只读或对拍（`src/qstock_mcp/repository.py:584-617`、
  `skills/daily-review/SKILL.md:45,163`）。
- **写入与重取边界**：单日 basic upsert + 父组详情替换；历史正式回溯按日 DELETE+INSERT
  basic，失败保留旧日，`--force` 同时重抓详情。官方历史入口有深度记录，但换源、字段缺失、
  详情完整性及金额汇总使整表不承诺跨次逐值一致（`docs/lhb-source-cli.md:20-21,61-67,147-152`）。

### 2.8 `lhb_stock_detail`

- **含义、键、日期**：basic 父事件下的买卖席位记录；只有代理主键 `id` 和外键
  `lhb_basic_id`，逻辑父键 `(trade_date, stock_code, lhb_reason)`，无席位行业务唯一约束
  （`src/qstock_mcp/sql/007_lhb.sql:1-6,34-57`）。
- **字段来源**：席位名/代码、方向、可得买卖净额为 U；`amount` 是 L 兼容列，等于该方向
  金额。官方源未提供的反向金额、净额、百分比、三月计数保持 NULL；北交所、非 A 股和未知
  代码不写详情（`docs/adr/0017-lhb-seat-details-and-repair.md:12-19`）。
- **写方**：与 basic 同日的 MCP/CLI LHB 链；深圳 THS 同页或交易所官方明细、上海交易所文件。
  写法是按父组 DELETE+INSERT，不是席位行 upsert（`src/qstock_mcp/repository.py:473-579`）。
- **读方**：MCP `query_board_data`；LHB 修复逻辑读父组完整性，basic 金额计算在上游适配器
  内使用席位响应，不是每次查询本表再汇总（`src/qstock_mcp/repository.py:716-728`、
  `src/qstock_mcp/tools_board.py:788-807,839-879`）。
- **写入与重取边界**：完整父组须同时有买卖方向，但字段可 NULL；默认只替换本次有效返回的
  父组，其他旧父组保留。详情缺口不单独使批次失败；重取受 basic 父记录、源能力和保留策略
  约束（`docs/adr/0017-lhb-seat-details-and-repair.md:14-26`）。

### 2.9 `lhb_stock_statistic`

- **含义、键、日期**：按股票物化的近 60 日历日 LHB 聚合快照；键
  `(trade_date, stock_code)`，日期是统计快照日，不是每个输入事件日
  （`src/qstock_mcp/sql/007_lhb.sql:59-85`）。
- **字段来源**：全部为 L/M：`appear_count_3m` 实际是窗口 basic 行数（同股多原因分别计），
  买/卖/净额为窗口合计；`stock_name=MAX(stock_name)`，两个 seat_count 恒 NULL。名称中的
  `3m` 不代表 SQL 使用三个月窗口（`src/qstock_mcp/repository.py:582-617,684-701`）。
- **写方**：单日 basic 落库后 `recompute_lhb_statistic`；历史回溯用临时 staging 补窗前输入，
  与当日 basic 替换同事务重算（`src/qstock_mcp/tools_board.py:798-807,873-880`）。
- **读方**：MCP `query_board_data`；daily-review “近三月常客”；CLI `lhb --check` 读库内快照
  与新浪 ggtj 对拍，`coverage` 读行数（`docs/adr/0014-lhb-statistic-selfcalc.md:19-38`、
  `skills/daily-review/SKILL.md:45,163`）。
- **写入与重算边界**：当日 DELETE+INSERT，可随窗口滑出和 basic 修订自愈；但前提是目标日
  前 60 日历日 basic 完整。历史 staging 不落窗外业务表，缺输入时不能把短窗结果当完整统计
  （`docs/lhb-source-cli.md:138-150`）。

### 2.10 `lhb_yyb_capital`

- **含义、键、日期**：同花顺营业部当前累计资金榜的本地执行日快照；键
  `(fetch_date, seat_name)`，`fetch_date` 是抓取执行日，不是榜单业务截止日
  （`src/qstock_mcp/sql/007_lhb.sql:87-103`）。
- **字段来源**：rank、seat、累计参与/买入金额为 U/UD；卖、净、均额无来源恒 NULL。金额中文
  文本在本地换算万元（`src/qstock_mcp/adapters/_lhb_yyb.py:15-30`）。
- **写方**：单日/close 的 LHB 段，经 `ExchangeLhbAdapter` 调 akshare 同花顺接口；历史任务
  不写（`src/qstock_mcp/adapters/lhb_exchange_adapter.py:349-369`、
  `docs/lhb-source-cli.md:147-152`）。
- **读方**：MCP `query_board_data`，daily-review 席位动向
  （`src/qstock_mcp/repository.py:768-781`、`skills/daily-review/SKILL.md:45,163`）。
- **写入与重取边界**：接口无日期参数，只能再取执行时当前累计榜；按席位 upsert 不删除本次
  榜单已消失席位，也不能凭 `fetch_date` 推断上游统计窗口
  （`src/qstock_mcp/adapters/_lhb_yyb.py:15-17`、`src/qstock_mcp/repository.py:427-470`）。

### 2.11 `lhb_yyb_most`

- **含义、键、日期**：同花顺营业部当前上榜排行快照；键 `(fetch_date, seat_name)`，同样只有
  执行日轴（`src/qstock_mcp/sql/007_lhb.sql:105-122`）。
- **字段来源**：rank、seat、appear、buy_count 为 U/UD；`buy_amount` 实为上游“合计动用资金”
  （买+卖），不是纯买入；卖额/卖数/净额恒 NULL
  （`src/qstock_mcp/adapters/_lhb_yyb.py:33-49`）。
- **写方/读方**：同 capital；MCP `query_board_data` 与 daily-review 消费。
- **写入与重取边界**：无历史日期参数、按席位 upsert 且不整日清理。历史 LHB 回溯只写
  basic/detail/statistic，不能补回某历史执行日的两张 yyb 排行
  （`docs/lhb-source-cli.md:180-182`）。

### 2.12 `conclusions`

- **含义、键、日期**：skill/agent 生成的通用 JSON 结论；键
  `(subject_type, subject_code, trade_date, conclusion_type)`，trade_date 是写入方声明的业务
  日期，payload 结构由 skill 自报（`src/qstock_mcp/sql/008_conclusions.sql:1-18`、
  `docs/adr/0003-generic-conclusions-table.md:1-12`）。
- **字段来源**：主体、日期、类型和 payload 都是本地业务产物；created/updated 为 M，server
  只校验日期和 JSON 可序列化，不校验 payload 语义
  （`src/qstock_mcp/tools_conclusions.py:21-59`）。
- **写方**：MCP `save_conclusion`；当前由 daily-review、limit-up-analysis、SEPA 写
  `daily_review.*`、`limit_up.close`、`sepa.*`（`skills/daily-review/SKILL.md:75-76`、
  `skills/limit-up-analysis/SKILL.md:45-46`、`skills/sepa/SKILL.md:84-95`）。
- **读方**：MCP `query_conclusions`；daily-review 读 `limit_up.close`，各 skill 可核验自身
  落库行（`src/qstock_mcp/tools_conclusions.py:62-94`）。
- **写入与重取边界**：同键 upsert，旧 payload 被覆盖且无版本历史；可重复保存相同 JSON，
  但不能从证券上游重取，也不能仅凭输入表确定重建 agent 文本、判断和报告路径。

### 2.13 `stock_list`

- **含义、键、日期**：当前 A 股代码/名称参考清单；键 `stock_code`，只有 `updated_at`，没有
  上市/退市生效日期或历史清单版本（`src/qstock_mcp/sql/009_stock_list.sql:1-8`）。
- **字段来源**：代码/名称为 U，source/updated_at 为 M；akshare 交易所官网清单主源，
  baostock `query_stock_basic` 过滤上市 A 股作后备，不含北交所后备能力
  （`src/qstock_mcp/adapters/__init__.py:61-73`、
  `src/qstock_mcp/adapters/baostock_adapter.py:34-56,197-200`）。
- **写方**：init 轻量初始化、CLI `stock-list`（`src/qstock_mcp/tools_init.py:82-104`）。
- **读方**：全量回溯标的集合、快照代码兜底、强势股名称；CLI `coverage` 以它为股票 Universe
  （`src/qstock_mcp/repository.py:324-328`、`src/qstock_mcp/tools_strong.py:74-84`、
  `src/qstock_mcp/tools_coverage.py:58-77`）。
- **写入与重取边界**：按 code upsert，不删除本次清单缺席代码；只能重取当前清单，不能重建
  某历史日 Universe。用它扫描历史覆盖时，扫描域是当前清单而非 point-in-time 清单
  （`src/qstock_mcp/repository.py:293-328`）。

### 2.14 `index_daily`

- **含义、键、日期**：七主要指数历史日线；键 `(index_code, trade_date)`，无 adj，与个股代码
  冲突隔离（`src/qstock_mcp/sql/010_index_daily.sql:1-26`）。
- **字段来源**：OHLCV/amount/turnover 为 U，经单位换算；腾讯的涨跌与振幅为 L，
  baostock `pctChg/turn` 为 U、振幅/涨跌额 NULL；source/timestamps 为 M
  （`src/qstock_mcp/adapters/tencent_adapter.py:204-243`、
  `src/qstock_mcp/adapters/baostock_adapter.py:59-81,202-208`）。
- **写方**：init 轻量初始化、CLI `morning` 尾行补齐、`index` 区间回填/T+0；链腾讯→baostock，
  source 可随复核覆盖（`src/qstock_mcp/tools_index.py:58-153`、
  `src/qstock_mcp/tools_morning.py:303-340`）。
- **读方**：MCP `query_index_daily`；daily-review 60 日历史、相关性、量价和偏离值基准；
  CLI `index --verify/coverage` 只读（`src/qstock_mcp/tools_init.py:358-423`、
  `skills/daily-review/SKILL.md:37,46,55-71`）。
- **写入与重取边界**：按指数日 upsert、区间入口可重复；腾讯 T+0 与 baostock 次晨可互相修订，
  科创50 只有腾讯承载。upsert 不删除上游后来撤掉的历史行，也不保证换源逐值相同
  （`src/qstock_mcp/adapters/__init__.py:130-142`、
  `src/qstock_mcp/repository.py:331-399`）。

### 2.15 `news_items`

- **含义、键、日期**：妙想搜索结果按市场/股票主体沉淀的资讯；键
  `(news_code, subject_type, subject_code)`，同一资讯挂多主体会多行；主轴 `publish_time`
  （timestamptz），另有 `fetched_at` 采集时间（`src/qstock_mcp/sql/011_news_items.sql:1-25`）。
- **字段来源**：资讯 id、类型、标题、截断正文、发布时间、来源、链接、作者、机构、评级、
  raw JSON 为 U；subject 关联是调用方业务输入；缺 id 时 `fb:title|date` 是 L 键，东八区时间
  解析与 fetched_at 为 L/M（`src/qstock_mcp/repository.py:927-1014`）。
- **写方**：MCP `mx_search` 单源搜索并落库；daily-review/limit-up-analysis 可触发搜索
  （`src/qstock_mcp/tools_mx.py:142-180`、`skills/daily-review/SKILL.md:38`）。
- **读方**：MCP `query_news` 纯读，daily-review 和 limit-up-analysis 读取近期资讯；查询不
  返回 raw（`src/qstock_mcp/tools_mx.py:183-230`、
  `src/qstock_mcp/repository.py:1035-1084`）。
- **写入与重取边界**：业务键 upsert 且更新 fetched_at，但搜索是问句驱动的当前结果，不是按
  news_code 的历史重取接口；相同问句不保证返回同集合。已保存 raw 可支持本地字段再投影，
  不能恢复未曾搜到的条目或历史搜索排名（`src/qstock_mcp/tools_mx.py:149-165`）。

### 2.16 `sw_industry_meta`

- **含义、键、日期**：申万行业代码、名称、级别参考表；键 `industry_code`，无业务日期轴
  （`src/qstock_mcp/sql/012_sw_industry.sql:15-20`）。
- **字段来源**：全部来自仓库内置 CSV 种子；种子合并多版本、新版名称优先，不是某个
  point-in-time 官网快照（`src/qstock_mcp/_sw_classify.py:94-109`）。
- **写方**：MCP `import_sw_industry_meta`，按代码 upsert，只动 meta 不动成分历史
  （`src/qstock_mcp/tools_init.py:529-564`）。
- **读方**：`query_sw_board_daily` 左连接补行业名；CLI `coverage` 还从同一 CSV 种子生成申万
  板块扫描清单（`src/qstock_mcp/price_reader.py:135-139`、
  `src/qstock_mcp/tools_coverage.py:88-99`）。
- **写入与重取边界**：同一 commit 的 CSV 可本地确定重放；upsert 不删除种子已移除代码。
  无需请求上游，但跨代码版本不保证名称集合相同。

### 2.17 `sw_industry_class`

- **含义、键、日期**：股票到行业的生效日期变更史；键
  `(source_system, stock_code, board_code, effective_date)`，日期是计入/生效日，查询取不晚于
  目标日的最新行。`sw` 现役，`em` 存量只读（`src/qstock_mcp/sql/012_sw_industry.sql:1-13,22-60`）。
- **字段来源**：`sw` 四列均来自申万 xls（代码本地补零、日期解析），`em` 为退役留档；
  source_system 是体系标识（`src/qstock_mcp/_sw_classify.py:56-91`）。
- **写方**：MCP `import_board_classification` 以本地文件为准；CLI `sw-dict` 下载当前官网 xls
  后复用同一入口。事务内删除该体系全部行再插入，不是逐行 upsert
  （`src/qstock_mcp/tools_init.py:425-523`、`src/qstock_mcp/repository.py:1087-1131`）。
- **读方**：`query_sw_board_daily` point-in-time 聚合、daily-review 行业归属；CLI coverage/
  parity 间接读（`src/qstock_mcp/price_reader.py:107-140`、`skills/daily-review/SKILL.md:136-148`）。
- **写入与重取边界**：固定同一文件可确定重放并清陈旧行；在线 URL 只给当时最新文件，历史
  文件版本未被仓库持久化。官网文件含历史变更不等于能重取过去某次发布版本；`em` 无写路径
  （`src/qstock_mcp/tools_init.py:476-511`、`src/qstock_mcp/sql/012_sw_industry.sql:11-13`）。

### 2.18 `collection_log`

- **含义、键、日期**：采集、导入、自算、拒收、计划缺席、复权修复/撤销的 append-only 运行
  台账；无业务唯一键，仅代理 id。`trade_date` 是目标交易日，区间采集记尾日；另有 started/
  finished 时间（`src/qstock_mcp/sql/013_collection_log.sql:1-28`）。
- **字段来源**：section/status/source/rows/error_kind/detail 是本地对一次动作的 M 摘要，
  detail 最长 500 字符，并非原始响应。状态为 ok/empty/error/planned_absent；复权 repair 的
  error_kind 另有专用词表（`src/qstock_mcp/ledger.py:21-42,60-100`）。
- **写方**：各 tools 写路径 `record_collection`，复权事件撤销另同事务直插；台账写失败只记
  warning，不拖垮主业务提交（`src/qstock_mcp/ledger.py:60-100`、
  `src/qstock_mcp/adjustment_backfill.py:59-72`）。
- **读方**：MCP `query_collection_log`；daily-review 历史就绪归因，THS 每日一次守卫，复权
  事件完备性/断点续跑、申万回补断点、CLI coverage/parity
  （`src/qstock_mcp/ledger.py:45-57,116-139`、
  `src/qstock_mcp/price_reader.py:242-275`、`skills/daily-review/SKILL.md:24-26`）。
- **写入与重取边界**：**故意非幂等**，每次动作新增行；重跑会产生新台账而不是覆盖旧台账。
  无原始请求/响应全文且写失败可缺行，所以不能从上游或业务表确定重建原运行史。历史 repair
  行也不等于当前 pending 数（`docs/adj-backfill-cli.md:145-147`）。

### 2.19 `sw_board_valuation`

- **含义、键、日期**：申万双级板块每日分析报表的估值维度；键
  `(trade_date, board_code)`，日期为报表交易日，T+1 获取；与 `market_boards` 实时/T+0 截面
  分表（`src/qstock_mcp/sql/018_sw_board_valuation.sql:1-25`）。
- **字段来源**：close_index、换手、PE、PB、流通市值、股息率为 U/UD，流通市值亿元→万元在
  映射层换算（`src/qstock_mcp/adapters/_sw_board.py:161-185`）。
- **写方**：CLI `morning` 的 sw_board_valuation part，申万 analysis report 单源；适配器实际
  接受 start/end 日期区间，可查询历史报表（`src/qstock_mcp/tools_morning.py:343-380`、
  `src/qstock_mcp/adapters/sw_adapter.py:197-213`）。
- **读方**：MCP `query_board_data`；daily-review 可在次日修订版消费，当前收盘版不把它当
  当晚必需数据（`skills/daily-review/SKILL.md:51`）。
- **写入与重取边界**：按日板块 upsert，不整日删除；日期区间可重取，但 T+1 修订口径与
  current/trend 不可混作同一字段版本，长期历史和重取稳定性未在本轮验证。

### 2.20 `stock_daily_raw`

- **含义、键、日期**：新复权模型的个股不复权日线输入事实；键 `(stock_code, trade_date)`。
  窗口外 D-1 可保留作基线输入，但不进入 hfq（`src/qstock_mcp/sql/019_adjustment_shadow.sql:1-25`、
  `docs/adr/0018-adjustment-shadow-and-price-reader.md:17-26`）。
- **字段来源**：baostock OHLC/preclose/量额/pctChg/tradestatus/turn 为 U；preclose 是除权
  参考价，不恒等于上一 raw close。腾讯/新浪替代 raw 的涨跌/振幅为 L，字段覆盖与 baostock
  不同；source/updated_at 为 M（`src/qstock_mcp/adapters/baostock_adapter.py:59-81`、
  `src/qstock_mcp/adapters/_daily_common.py:25-44`）。
- **写方**：MCP `fetch_daily/query_daily` 自愈，CLI `daily/morning/adj-backfill`，init 全量
  回溯共用 `rebuild_stock`；全市场回填整日替换，单票常态同键 upsert
  （`src/qstock_mcp/adjustment.py:186-212`、`src/qstock_mcp/adjustment_backfill.py:1-17`）。
- **读方**：`NewPriceReader` 的 raw/hfq/qfq 查询、强势股、申万自算、覆盖与切换水位；
  adj-acceptance/audit 和回滚修复也读（`src/qstock_mcp/price_reader.py:197-275,305-417`、
  `src/qstock_mcp/adjustment_switch.py:16-25`）。
- **写入与重取边界**：baostock 历史按日接口可重取并整日替换，替代源只补单票 raw，后续
  baostock 可覆盖升级；非正 close 留 raw 审计但不派生。请求失败不能解释成空日，raw 缺口会
  使 coverage partial（`docs/daily-source-cli.md:42-91`、
  `src/qstock_mcp/adjustment.py:267-271`）。

### 2.21 `stock_adjust_factor`

- **含义、键、日期**：每股除权日的累计 B 事件证据；键 `(stock_code, ex_date)`，日期轴是
  除权日。只保存 B 发生价格阶跃所需事件，不是完整公司行为登记簿
  （`src/qstock_mcp/sql/019_adjustment_shadow.sql:27-40`、
  `docs/adr/0018-adjustment-shadow-and-price-reader.md:11-16`）。
- **字段来源**：`factor_cum` 只取 baostock `backAdjustFactor`；`adjustFacto(r)` 不参与派生。
  日接口映射只明确 code/ex_date/B，kind/detail/reg_date 可空；source/timestamps 为 M
  （`src/qstock_mcp/adapters/baostock_adapter.py:124-137,186-193`）。
- **写方**：与 raw 相同的 morning/daily/init/adj-backfill 管线；成功重查整日替换事件集，
  同 B 非阶跃行不写，撤销另写 collection_log 标记并置 state pending
  （`src/qstock_mcp/adjustment_backfill.py:1-13`）。
- **读方**：`rebuild_stock` 建 F/hfq/state，`NewPriceReader` 返回事件和完备性；switch/audit/
  acceptance 读（`src/qstock_mcp/adjustment.py:213-281,398-470`、
  `src/qstock_mcp/price_reader.py:220-251,276-297`）。
- **写入与重取边界**：事件表本身不足以证明“无事件”；必须有全窗口逐日成功/空日台账。
  失败日不得当空日；补录、改期、撤销会改变下游 hfq/qfq，故重取是修订机制而非旧值保证
  （`docs/adj-backfill-cli.md:81-101,111-147`）。

### 2.22 `stock_daily_hfq`

- **含义、键、日期**：本地物化后复权日线；键 `(stock_code, trade_date)`，只写采集窗口内有效
  raw 行（`src/qstock_mcp/sql/019_adjustment_shadow.sql:42-58`）。
- **字段来源**：`factor_cum=F=B/B0` 与 OHLC=raw×F 为 L；volume/amount/turnover/tradestatus
  复制 raw，不重算；rebuilt_at 为 M（`src/qstock_mcp/adjustment.py:398-449`）。
- **写方**：只有 `rebuild_stock` 及其 morning/daily/init/backfill 调用方；按全票或受影响窗口
  DELETE+INSERT，与 state 同事务（`src/qstock_mcp/adjustment.py:417-486`）。
- **读方**：NewPriceReader 的 hfq/qfq、强势股窗口、切换修复与验收；不直接由 skill 读表
  （`src/qstock_mcp/price_reader.py:197-262,341-379`）。
- **写入与重算边界**：固定 raw、完整事件、可信 B0 与代码版本时本地确定重算；缺锚、事件未决、
  raw 缺口、非正 close 时不得生成或判 complete。新增事件会改 F_latest，从而重锚全历史 qfq，
  即使部分 hfq 行无需重写（`docs/adr/0018-adjustment-shadow-and-price-reader.md:11-18`）。

### 2.23 `stock_factor_state`

- **含义、键、日期**：每股复权锚点和重建状态；键 `stock_code`。`latest_trade_date` 是当前
  状态对应 raw 尾日，另有 last_rebuilt_at；不是逐日事实表
  （`src/qstock_mcp/sql/019_adjustment_shadow.sql:60-75`）。
- **字段来源**：baseline_factor=B0、factor_latest=F_latest、latest_trade_date 是 L；
  rebuild_pending 是本地控制位，last_rebuilt_at 是 M。CHECK 允许 pending 时锚点为空，但禁止
  无锚且非 pending 的“完整”假象（`src/qstock_mcp/adjustment.py:307-397,450-470`）。
- **写方**：`rebuild_stock`，事件撤销/异常也会标 pending；switch 读其水位并在回滚时处理无效
  锚（`src/qstock_mcp/adjustment_backfill.py:63-72`、
  `src/qstock_mcp/adjustment_switch.py:45-57`）。
- **读方**：NewPriceReader 计算 qfq、coverage、强势股与横截面；switch/audit/acceptance
  （`src/qstock_mcp/price_reader.py:220-275,341-402`）。
- **写入与重算边界**：相同事实与权限上下文可重算，但 B0 只能由全窗口采集或 morning 新股
  首日初始化；窄查询窗不得假定 B0=1。pending 表示需要修复，不是可由上游直接重取的业务
  事实（`docs/daily-source-cli.md:73-89`）。

### 2.24 `stock_adjustment_switch_state`

- **含义、键、日期**：单行复权切读控制状态；键固定 `id=1`，mode=legacy/new。
  `fact_watermark` 是四表本地时间戳最大值，不是交易日；另有 switched_at/updated_at
  （`src/qstock_mcp/sql/020_adjustment_switch.sql:1-13`、
  `src/qstock_mcp/adjustment_switch.py:16-25`）。
- **字段来源**：全部为本地 M/控制值；DDL 只幂等插入初始 legacy 行
  （`src/qstock_mcp/sql/020_adjustment_switch.sql:11-13`）。
- **写方**：CLI `adj-switch cutover/rollback/roll-forward`；表改名、状态更新、必要修复在同一
  事务，零上游请求（`src/qstock_mcp/adjustment_switch.py:135-224`）。
- **读方**：CLI `adj-switch status` 与切换预检；实际价格读取模式还由进程环境
  `QSTOCK_PRICE_SOURCE` 选择，数据库 mode 不会自动修改外部环境
  （`src/qstock_mcp/price_reader.py:420-432`、`docs/adj-switch-cli.md:16-24`）。
- **写入与重取边界**：这是有前置条件的有限状态机，不是通用幂等 upsert；重复 cutover 会因
  mode/表存在性不符而报错。时间戳和历史状态迁移不可由证券上游重取，也不应靠重跑动作伪造
  （`src/qstock_mcp/adjustment_switch.py:169-215`）。

### 2.25 条件表 `stock_daily_legacy`

- **存在条件与含义**：只在 cutover 时由 `ALTER TABLE stock_daily RENAME` 产生，结构与数据是
  当时 legacy 表的冻结留档；`ensure_schema` 检测到它时不重建 `stock_daily`
  （`src/qstock_mcp/adjustment_switch.py:178-189`、`src/qstock_mcp/sql/001_stock_daily.sql:4-8`）。
- **字段来源**：继承 §2.1 的全部混合历史，不因改名变成 raw 事实，也不增加来源版本信息。
- **写方**：正常 new 模式无业务写方；rollback 时改名回 `stock_daily`，若四表水位已推进则
  恢复表名后在同一事务修复。roll-forward 再改名归档（`src/qstock_mcp/adjustment_switch.py:190-215`）。
- **读方**：`LegacyPriceReader._legacy_table` 优先选择它，从而 legacy 查询、强势股、申万自算
  和 2023 年前 qfq 仍可读（`src/qstock_mcp/price_reader.py:9-12,15-104`）。
- **写入与重取边界**：单个历史行可能仍有上游重取路径，但**归档表整体的原行集合、来源、
  覆盖历史和当时 qfq 锚点不可由当前上游确定重建**；精确恢复依赖备份/归档，不依赖重新采集。

## 3. 候选分类与“可重复”分级（供 #192 HITL，不是既定决策）

### 3.1 候选分级定义

| 级别 | 候选含义 | 必要边界 |
| --- | --- | --- |
| **R1 历史可重取且同口径** | 有日期/事件寻址的历史入口，源、字段映射和业务口径固定，可再次取得同一逻辑事实 | 不只看 CLI 有 `--start`；还需重取样本验证。当前静态证据不足以给任何整表无条件 R1 |
| **R2 可重取但会修订** | 历史入口存在，但上游补录/撤销/复核、换源、字段集或本地派生可能改变结果 | 必须保留 source/台账/水位；重跑前区分断点续跑与 force 整窗重抓 |
| **R3 仅当前可得** | 只能取执行时当前清单、快照、搜索或累计排行；历史行靠当时落库积累 | `trade_date/fetch_date` 不创造历史重取能力；upsert 也不保证旧集合完整 |
| **R4 本地确定重算** | 固定代码版本和完整本地输入，可确定重放或重建 | 必须写明输入完整性、窗口、锚点、是否清陈旧行；不是“有一个计算公式”就够 |
| **R5 不可由上游重取的本地记录/业务产物** | agent 结论、运行审计、控制状态、退役留档的精确历史 | 依赖备份、工件、导出或显式版本保存恢复；不能借“可 upsert”冒充可重取 |

R2 的“会修订”包括**已实现的修订机制**和**换源/口径使重跑不保证同值**，不是断言每个上游
每天都改数。R1–R5 描述获取边界，不替代数据质量或完备性评级。

### 3.2 候选逐表矩阵

| # | 表 | 候选表级分类 | 候选可重复级别与条件 |
| ---: | --- | --- | --- |
| 1 | `stock_daily` | 原始事实 + 上游/本地派生混合；cutover 后兼容留档 | 活跃行 R2；归档后的精确整体 R5 |
| 2 | `market_snapshot` | 原始事实（上游截面，含 UD 列） | R3 |
| 3 | `market_indices` | 原始事实（实时截面，部分 L 列） | R3 |
| 4 | `market_boards` | 原始事实 + 本地派生列 + 兼容留档的混合表 | 申万 trend R2；current R3；push2his/concept 精确留档 R5 |
| 5 | `zt_pool` | 原始事实（上游事件池，含 UD/L 映射） | R2，历史深度仅 4 天样本，更深待验证 |
| 6 | `strong_stocks` | 派生 | R4，需完整价格/zt_pool/清单；现有写路径不清落选旧行 |
| 7 | `lhb_basic` | 原始事实（上榜事件）+ L 汇总列 | R2；只有满足同源同详情子集才可局部 R4，不能整表标 R4 |
| 8 | `lhb_stock_detail` | 原始事实（席位记录）+ 兼容派生列 | R2，受父记录、市场覆盖和详情保留规则约束 |
| 9 | `lhb_stock_statistic` | 派生 | R4，需完整 60 日历日 basic/历史 staging |
| 10 | `lhb_yyb_capital` | 原始事实（上游当前累计排行，UD 为主） | R3 |
| 11 | `lhb_yyb_most` | 原始事实（上游当前排行，UD 为主） | R3 |
| 12 | `conclusions` | 业务产物 | R5；相同 payload 可 upsert，但不是上游重取 |
| 13 | `stock_list` | 原始事实（当前参考清单） | R3；历史 Universe 不可由本表恢复 |
| 14 | `index_daily` | 原始事实（历史行情，含 UD/L 列） | R2，T+0/次晨复核和换源会修订 |
| 15 | `news_items` | 原始事实（资讯内容快照）+ 本地主体关联 | R3；raw 可本地再投影，但条目集合不可按历史搜索确定重取 |
| 16 | `sw_industry_meta` | 原始参考数据（仓库种子） | R4，限同一 CSV/代码版本；无需上游 |
| 17 | `sw_industry_class` | 原始事实（文件变更史）+ `em` 兼容留档 | 在线当前文件 R2；固定文件 R4；em 精确留档 R5 |
| 18 | `collection_log` | 审计 | R5；append-only 不是幂等重放 |
| 19 | `sw_board_valuation` | 原始事实（上游 T+1 分析报表，含 UD） | R2，按日区间可重取但属修订口径 |
| 20 | `stock_daily_raw` | 原始事实（含部分 L 行情列） | R2；整日替换支持修订，替代源字段口径不同 |
| 21 | `stock_adjust_factor` | 原始事实（累计 B 事件证据） | R2；完备性还依赖 collection_log 全窗口证据 |
| 22 | `stock_daily_hfq` | 派生 | R4，需 raw+B+可信 B0、无未决异常 |
| 23 | `stock_factor_state` | 派生 + 控制 | R4，需同一建锚权限/窗口与完整事实；pending 不可忽略 |
| 24 | `stock_adjustment_switch_state` | 控制 | R5；状态机可执行不等于历史可重取 |
| +1 | `stock_daily_legacy` | 兼容留档（内容仍是事实/派生混合） | 精确归档整体 R5；个别行重取只能归 R2，不能恢复旧表整体 |

### 3.3 最需要 #192 拍板的分类冲突

1. **“原始事实”是否允许含 UD/L 列。** 建议允许表级与字段级双轴，否则 `lhb_basic`、
   `market_indices`、`market_boards`、`stock_daily_raw` 会被迫在“事实/派生”中二选一，丢掉
   最重要的信息。字段字典至少标 `U/UD/L/M`，表级标行的主要生成方式。
2. **`lhb_basic` 是事件事实还是席位派生。** 候选是“事件事实 + 汇总派生列”，不是整表派生：
   官方适配器先取席位再生成 basic，但 THS basic、缺详情市场和保留策略使本地 detail 并非
   所有 basic 金额的完整可重算输入。
3. **`stock_factor_state` 是派生还是控制。** 两者都是：B0/F_latest 可算，pending/时间戳决定
  能否读和是否 complete。建议混合标签，禁止把它当普通维度状态忽略完备性。
4. **`stock_daily`/`stock_daily_legacy` 是事实还是兼容留档。** 名称与生命周期不同：活跃表
   是混合价格存储，归档表的主要契约是保留旧读口径。归档身份不能反向把历史 qfq 变成原子事实。
5. **yyb 排行与 statistic 是否同属“派生”。** 当前不同：statistic 是本地可重算，yyb 是上游
   当前统计快照。若目标都改查询时派生，yyb 仍缺历史输入，不能照搬 statistic 的 R4。
6. **`sw_industry_meta` 是上游事实还是本地种子。** 当前可重复性的所有者是仓库 CSV，不是
   官网；分类应写“参考数据/种子”，R4 只绑定 commit。
7. **R1 是否需要一个“已验证”修饰。** 当前多个历史入口存在，但本轮零上游请求，不能仅凭
   代码把整表写成 R1。建议字典将“入口能力”和“最近重取验证日期/样本”分列。

## 4. 文档漂移与容易误用的现状

以下是源码与注释/消费文档的差异，供 #193 成稿时避免复制旧话：

- `sql/001` 头注仍把 init 全量回溯写成 legacy 写方，但当前 init/daily 已复用 raw+B 回填，
  正常期不双写 legacy；以 `tools_daily.py:256-296`、ADR 0018 的 #167 补记为准
  （`docs/adr/0018-adjustment-shadow-and-price-reader.md:19`）。
- `sql/004`、`sql/015` 和 repository 盘面注册表附近仍有 push2his/东财单源旧注释；当前
  回补写方是申万 `swsresearch`，东财路径已移除（`src/qstock_mcp/tools_backfill.py:1-20`、
  `src/qstock_mcp/repository.py:402-424`）。
- `sql/007` 两张 yyb 的行内注释仍写“东财”，实际 `_lhb_yyb.py` 明确是同花顺接口
  （`src/qstock_mcp/adapters/_lhb_yyb.py:1-9`）。
- daily-review 口径附注仍称 `market_indices.amplitude` 与 `index_daily.amplitude` 长期 NULL；
  当前腾讯指数快照直给振幅，腾讯/新浪日线映射会计算振幅，baostock 才置 NULL。字典应按
  source 写字段能力，不能沿用“一律 NULL”（`skills/daily-review/SKILL.md:175`、
  `src/qstock_mcp/adapters/tencent_adapter.py:323-332`、
  `src/qstock_mcp/adapters/_daily_common.py:25-44`）。
- 业务键幂等普遍**不等于整日集合替换**：snapshot、indices、boards、zt_pool、strong、yyb、
  stock_list、sw valuation 等均只 upsert。字典若写“可重跑”，应另列“是否清理消失行”。
- `collection_log` 是审计也是复权完备性输入，但允许落账失败；它不能被描述成无损事件溯源
  日志（`src/qstock_mcp/ledger.py:3-5,72-100`）。

## 5. 给 #192/#193 的建议（目标与现状分栏）

### 5.1 #192 需要确认的术语/目标边界

1. **采用双轴模型**：表级 `原始事实/派生/审计/控制/业务产物/兼容留档` + 字段级
   `U/UD/L/M`；混合表显式写混合，不追求每张表只有单一标签。
2. **指数目标消费**：可把 `index_daily` 定为 close 复盘的指数历史事实，`market_indices`
   只作实时截面/降级参考，`market_snapshot` 不再作为 close 的必需采集入口。这只是目标建议：
   当前 daily-review 仍把 market_snapshot 列核心、逐个读 market_indices，且抓取 error 会触发
   中止，不能写成已经改造（`skills/daily-review/SKILL.md:24-39,80`）。
3. **LHB 目标消费**：按日期 basic 事件和 detail 席位作事实导航很自然；statistic 已是本地
   物化派生，可讨论改查询时派生。yyb 两表是当前累计快照，没有历史事实底座，不能与
   statistic 一并假定可任意历史查询时派生。
4. **可重算判据**：至少记录输入表、窗口、完整性证据、代码/种子版本、锚点和陈旧行清理策略。
   `strong_stocks` 与 `lhb_stock_statistic` 可作为“只 upsert”和“DELETE+INSERT”两个对照例。
5. **R1 验证门槛**：#192 若决定保留 R1，建议要求“同源同日期重复请求 + 字段口径对拍 + 样本
   日期/范围/结果”证据；本轮不为任何整表提供这种一手实测。

### 5.2 #193 字典结构与导航建议

- **全表速查矩阵**至少含：表、业务含义、物理/逻辑键、日期轴、写方、MCP/CLI/skill 读方、
  表级分类、字段级混合标记、R 级别、完整性前提、陈旧行策略、证据链接。
- **日期与单位单独成章**：`fetch_date != trade_date`，`publish_time/fetched_at` 不同，
  `effective_date` 是生效日，switch watermark 是本地时间戳；金额万元、volume 股是库内统一
  契约（`CONTEXT.md:55-63`）。
- **“当前实现”与“目标消费”必须双栏**：尤其是 close 对 market_snapshot/market_indices 的
  依赖，以及 LHB statistic/yyb 是否查询时派生。目标不得回写到写方/读方事实栏。
- **常见误用清单**优先收：用 market_snapshot/market_indices 补历史；把 board_code 当
  market_boards 唯一键；把 lhb_basic.total 当全日成交；把 appear_count_3m 当自然三个月；
  把 yyb most.buy_amount 当纯买入；把 factor 行当完整公司行为；把 qfq 当固定物化历史；
  把 collection_log 行当当前 pending；把 upsert 当可重取/可重算。
- **为每张派生表给“从空输入重建”和“在旧表上重跑”两种说明**。后者才能暴露 strong_stocks
  的落选旧行、upsert 表的消失行和 state 的 pending 门控。
- **导航只指逻辑事实**：skill 继续经 MCP/管道，不直连 PG；字典可以建议未来消费边界，但
  实现改造须另立 issue，符合 #189/#193 非目标。

## 6. 覆盖核对与未决项

- 覆盖数量：**24/24 DDL 表 + 1/1 条件归档表**。其中 `sql/007` 五张、`sql/012` 两张、
  `sql/019` 四张均已逐表展开，没有按文件合并漏表。
- 每表均有业务含义、业务键/逻辑父键、日期轴、字段来源、写方、读方和可重复边界；候选分类
  集中在 §3，未冒充 #192 已决策。
- 未决而非遗漏：THS 深史与修订稳定性、各历史源长期可重取性、新闻同问句结果稳定性、申万
  历史文件版本保存、上游字段原子性外部证明，均需后续限定样本重取；本轮按票要求不执行。
- 本报告不决定保留/改视图/停消费/迁移，不修改 CONTEXT/ADR，不替 #192/#193 或生产实施票
  拍板。
