# 业务库实际表形态、日期轴与来源样本只读核验

日期：2026-11-18。关联：#191（本研究）、#189（parent map）；下游输入：#192、#193。
代码基线：`9868ddd89e8779742f7fbe9929491f5db0a40080`；DDL 对照范围为 `src/qstock_mcp/sql/001`—`020`。

## 0. 结论速览

| 问题 | 本次实测结论 | 证据 |
| --- | --- | --- |
| 实际表数 | `public` 下有 **24 张普通表**，没有额外视图、物化视图、外部表或分区表；24 张均能对应当前 DDL 的条件分支结果 | 【DB-OBJ】 |
| `stock_daily_legacy` | **不存在**；存在的是 `stock_daily`。这是 sql/001 允许的条件形态，不是缺表漂移 | 【DB-OBJ】【SQL001】 |
| 物理 schema 漂移 | 一处类型漂移：`lhb_stock_detail.id` 实际为 `integer`，DDL 为 `bigserial`；另有一处无语义差异的列顺序差异：`sw_industry_class` 保留迁移后的列顺序 | 【DB-COL】【SQL007】【SQL012】 |
| 约束与索引 | 业务主键/唯一键、外键、CHECK、默认值及二级索引均与相应 DDL 条件分支一致；所有实际索引均为 valid/ready | 【DB-CON】【DB-IDX】 |
| 复权切读 | `stock_adjustment_switch_state` 形态符合 DDL，唯一行 `mode=legacy`，`fact_watermark/switched_at` 均为 NULL | 【DB-ADJ】 |
| `market_snapshot` | 只有 20261013 一个业务日期、5,923 行，行级 `source` 全为 `efinance` | 【DB-MARKET】 |
| `market_indices` | 20261009—20261019 共 3,423 行、5 个观测日期；表内**没有 `source` 列**，不能给全部存量行指定来源 | 【DB-COL】【DB-MARKET】 |
| `stock_daily` 与影子表 | legacy 表同时有 `adj=qfq` 与 `adj=none`；raw 全为 baostock，hfq 是另一物化形态，state 有 32 行 `rebuild_pending=true`，不能用行数差直接判定完备性 | 【DB-ADJ】 |
| `market_boards` | 实际只观察到 `source=NULL` 和 `swsresearch`；未观察到 sql/015 注释中的 `push2his`。两类来源还对应不同 `board_type` 词汇 | 【DB-BOARDS】【SQL015】 |
| LHB 五表 | basic/detail/statistic 用 `trade_date`；两张 yyb 表只有 `fetch_date`。`fetch_date` 不能当作 basic 的交易日期，也不等于物理写入时间 | 【DB-LHB】 |
| `collection_log` | 11,321 行、15 个 section；约束允许四态，当前只观察到 ok/empty/error，**0 条 planned_absent** | 【DB-LOG】 |
| conclusions/news | 结论的 `trade_date` 可与写入日不同；新闻必须区分 `publish_time` 与 `fetched_at`，210 个 subject 关联行只对应 150 个不同 `news_code` | 【DB-PRODUCT】 |

以上只描述 2026-11-18 本轮只读查询看到的业务库状态。**有行不证明长期可重取，空日期区间不证明无事件，当前空表也不能证明表无用途；本轮没有做任何上游可用性实验。**

## 1. 方法、范围与限制

### 1.1 执行约束

- 使用用户提供的业务库连接；每个连接均先执行 `SET TRANSACTION READ ONLY`，并确认 `current_setting('transaction_read_only')='on'`。
- 系统目录查询设置 `statement_timeout=15s`；逐表计数与聚合查询设置 `statement_timeout=45s`。
- 只执行 SELECT、系统目录函数及以上事务/超时设置。**零 INSERT/UPDATE/DELETE、零 DDL、零业务库写入、零补数、零行情/资讯上游请求。**
- 2026-11-18 11:46:37+08:00 的最后控制查询仍确认只读和 45s 超时。数据库 TimeZone 为 `Asia/Shanghai`，下文 timestamptz 日期均按该时区展示。
- 不记录实际 DSN 或用户名；不导出行情明细、席位明细、新闻标题/正文、结论 payload 或 URL。除单行复权控制状态外，报告仅保留计数、分组和首末值等聚合结果。

### 1.2 实际执行的查询类别

1. `pg_class/pg_namespace`：枚举 `public` 的 r/p/v/m/f 对象；`to_regclass` 检查 `stock_daily`、`stock_daily_legacy`、切读控制表。
2. `pg_attribute/pg_attrdef`：逐表核对列顺序、`format_type`、NOT NULL、默认值及列注释。
3. `pg_constraint/pg_get_constraintdef`：核对主键、唯一、外键、CHECK 和 NOT NULL；`pg_index/pg_get_indexdef` 核对索引定义及 valid/ready。
4. 对 24 表逐表执行精确 `count(*)`，按实际适用日期列做 `min/max`；没有用估算行数代替精确计数。
5. 有选择地聚合 `source/status/adj/section/board_type/pool_type/information_type` 等字段；对结论和新闻仅做时间、类型及主体数量聚合。
6. 对 LHB 比较日期轴和 `created_at` 日期；对切读控制表读取唯一控制行。

所有 24 表计数和上述目录/聚合 SQL 均成功，**没有 SQL 失败、超时或被跳过的已声明检查**。不同查询使用不同的只读事务，并非一个跨全部语句的全局一致性快照；并发业务写入可能造成不同查询时点略有差异。本报告不声称检查了其他 schema、触发器、权限/RLS、序列归属、表空间或所有业务字段的值域。

## 2. 实际对象与 DDL 差异

### 2.1 实际 24 表

实际对象按名称排序为：

```text
collection_log                 conclusions
index_daily                    lhb_basic
lhb_stock_detail               lhb_stock_statistic
lhb_yyb_capital                lhb_yyb_most
market_boards                  market_indices
market_snapshot                news_items
stock_adjust_factor            stock_adjustment_switch_state
stock_daily                    stock_daily_hfq
stock_daily_raw                stock_factor_state
stock_list                     strong_stocks
sw_board_valuation             sw_industry_class
sw_industry_meta               zt_pool
```

没有 DDL 未声明的额外表，也没有当前条件下应存在却缺失的表。`stock_daily_legacy` 是切读后可能出现的条件归档名，本轮不存在；不能在字典中把它写成当前可查询表，也不能从当前不存在推断以后不会出现。【DB-OBJ】【SQL001】

### 2.2 物理差异

| 对象 | 当前 DDL | 业务库实际 | 判定 |
| --- | --- | --- | --- |
| `lhb_stock_detail.id` | `bigserial PRIMARY KEY` | `integer NOT NULL DEFAULT nextval(...)`，仍有主键 | **类型漂移**；本次只记录，不修改 |
| `sw_industry_class` 列顺序 | `source_system, stock_code, board_code, effective_date, updated_at` | `stock_code, effective_date, board_code, updated_at, source_system` | 迁移遗留的**顺序差异**；列集合、类型、默认值和四元组主键顺序符合目标语义 |
| 其他 22 表 | sql/001—020 的列、默认、空值约束 | 与相应条件分支一致 | 未发现物理形态漂移 |

`lhb_stock_detail.lhb_basic_id -> lhb_basic(id) ON DELETE CASCADE` 外键实际存在；detail 表没有额外业务唯一键，与 DDL 注释一致。全部索引 `indisvalid=true` 且 `indisready=true`。【DB-COL】【DB-CON】【DB-IDX】

### 2.3 注释/观测词汇差异，不误报为物理漂移

| 对象 | DDL/注释给人的印象 | 当前实测 | 字典处理建议 |
| --- | --- | --- | --- |
| `stock_daily` | sql/001 标题写“前复权 OHLCV” | `adj=qfq` 18,016,400 行，`adj=none` 31,378 行；`adj` 无 CHECK | 字典按 `adj` 分逻辑事实，不把整表只写成 qfq |
| `market_boards.source` | sql/015 只解释 NULL 与 `push2his` | NULL 1,002 行、`swsresearch` 16,120 行、`push2his` 0 行 | 注释词汇不覆盖现库；NULL 与未知来源须保留区别 |
| `market_indices` | 需要讨论日期与来源 | 根本没有 `source` 列 | 不允许从 `index_daily.source` 或单条台账反填行级来源 |
| LHB 文件头 | sql/007 写“四表” | 文件和现库均有 basic/detail/statistic/yyb_capital/yyb_most **五表** | 字典明确五表及两种日期轴 |
| `news_items.information_type` | sql/011 注释列举 NEWS/NOTICE/REPORT/INV_NEWS/WECHAT/BOND | 还观察到 CFH、HOT_NEWS；未观察到 BOND | 该列无 CHECK，应按开放上游词汇处理 |

这些观测不证明某来源永久停用或某枚举永不出现，只说明当前注释不宜直接当完整值域。【DB-ADJ】【DB-BOARDS】【DB-LHB】【DB-PRODUCT】

## 3. 24 表精确行数、业务键、索引与日期轴

下表行数均为本轮精确 `count(*)`。首末值是适用业务轴的 `min/max`，**不是连续覆盖或完备性声明**。`text/char(8)` 日期按库内声明的 yyyymmdd 语义展示；本轮没有做全列日期格式校验或交易日历连续性检查。【DB-COUNT】【DB-CON】【DB-IDX】

| 表 | 精确行数 | 适用业务日期轴首..末 | 主键/业务唯一键 | 主要二级索引（不含主键/唯一键自动索引） |
| --- | ---: | --- | --- | --- |
| `collection_log` | 11,321 | `trade_date` 20221230..20261117 | PK `id` | `trade_date`；`(section,trade_date)`；`status` |
| `conclusions` | 11 | `trade_date` 20261009..20261023 | PK `id`；UQ `(subject_type,subject_code,trade_date,conclusion_type)` | subject；date；type |
| `index_daily` | 39,331 | `trade_date` 19901219..20261117 | PK `id`；UQ `(index_code,trade_date)` | `index_code`；`trade_date` |
| `lhb_basic` | 6,759 | `trade_date` 20260616..20261116 | PK `id`；UQ `(trade_date,stock_code,lhb_reason)` | code；date |
| `lhb_stock_detail` | 46,711 | `trade_date` 20260706..20261116 | PK `id`；FK `lhb_basic_id`，无业务 UQ | basic_id；date；seat_name |
| `lhb_stock_statistic` | 105,690 | `trade_date` 20260616..20261116 | PK `id`；UQ `(trade_date,stock_code)` | code；date |
| `lhb_yyb_capital` | 645 | `fetch_date` 20261013..20261019 | PK `id`；UQ `(fetch_date,seat_name)` | fetch_date；seat_name |
| `lhb_yyb_most` | 380 | `fetch_date` 20261013..20261019 | PK `id`；UQ `(fetch_date,seat_name)` | fetch_date；seat_name |
| `market_boards` | 17,122 | `trade_date` 20260612..20261116 | PK `id`；UQ `(trade_date,board_type,board_name)` | name；date；type |
| `market_indices` | 3,423 | `trade_date` 20261009..20261019 | PK `id`；UQ `(trade_date,index_code)` | code；date |
| `market_snapshot` | 5,923 | `trade_date` 20261013..20261013 | PK `id`；UQ `(trade_date,stock_code)` | code；date |
| `news_items` | 210 | `publish_time` 20260109 16:53..20261015 14:42 +08 | PK `id`；UQ `(news_code,subject_type,subject_code)` | subject；publish_time |
| `stock_adjust_factor` | 17,615 | `ex_date` 20230106..20261117 | PK `(stock_code,ex_date)` | ex_date |
| `stock_adjustment_switch_state` | 1 | 无历史业务日期轴 | PK `id`；CHECK `id=1`、mode 二态 | 无 |
| `stock_daily` | 18,047,778 | `trade_date` 19901219..20261117 | PK `id`；UQ `(stock_code,trade_date,adj)` | code；date |
| `stock_daily_hfq` | 4,573,186 | `trade_date` 20230103..20261117 | PK `(stock_code,trade_date)` | date |
| `stock_daily_raw` | 4,797,979 | `trade_date` 20221230..20261117 | PK `(stock_code,trade_date)` | date |
| `stock_factor_state` | 5,375 | `latest_trade_date` 20230215..20261117 | PK `stock_code` | 无 |
| `stock_list` | 5,927 | 无业务日期轴；只有 `updated_at` | PK `stock_code` | 无 |
| `strong_stocks` | 3,193 | `trade_date` 20260915..20261019 | PK `id`；UQ `(trade_date,stock_code)` | code；date |
| `sw_board_valuation` | 162 | `trade_date` 20261117..20261117 | UQ `(trade_date,board_code)` | date；code |
| `sw_industry_class` | 12,929 | `effective_date` 19900101..20261014 | PK `(source_system,stock_code,board_code,effective_date)` | 无 |
| `sw_industry_meta` | 796 | 无日期轴 | PK `industry_code` | 无 |
| `zt_pool` | 1,713 | `trade_date` 20260914..20261019 | PK `id`；UQ `(trade_date,pool_type,stock_code)` | code；type；date |

补充审计时间轴：`collection_log.finished_at` 为 20261019 09:55:57..20261118 09:23:17 +08；`news_items.fetched_at` 为 20261010 16:09:46..20261015 16:39:48 +08。它们不能替代表内业务日期轴。`stock_list.updated_at` 只是清单更新时间，不是证券上市/生效日期。【DB-COUNT】

## 4. 重点证据

### 4.1 `market_snapshot` / `market_indices` / `index_daily`

**快照。** `market_snapshot` 的 5,923 行全部位于 20261013，5,923 个不同股票代码，`created_at` 完全相同（20261013 16:15:54），`source=efinance` 也是 5,923/5,923。该证据只支持“一次全市场截面已落库”，不支持其他日期有覆盖或 efinance 今后仍可重取。【DB-MARKET】

**指数快照。** `market_indices` 有 16 列，最后一列是 `created_at`，无 `source/updated_at`。按日聚合为：【DB-COL】【DB-MARKET】

| trade_date | 行数 / 不同指数代码 | created_at |
| --- | ---: | --- |
| 20261009 | 7 / 7 | 20261010 16:07:49 |
| 20261012 | 43 / 43 | 20261012 17:03:12 |
| 20261013 | 1,124 / 1,124 | 20261013 17:12:15 |
| 20261014 | 1,124 / 1,124 | 20261014 17:54:20 |
| 20261019 | 1,125 / 1,125 | 20261019 18:09:17 |

全表有 1,133 个不同指数代码。`collection_log` 中 `section=indices` 只有一条 ok 行：20261019、`source=efinance`、`rows=1125`，与当日行数吻合；其余四个日期没有同 section 台账可归属。因此只能说“20261019 有一次 efinance 采集台账”，**不能说全表都是 efinance**。首末值间只有 5 个观测日期，也不能写成连续历史。【DB-LOG】

**指数日线是另一张表。** `index_daily` 有行级 source，当前分布如下；唯一键不含 source，所以它是按 `(index_code,trade_date)` 保留一个选定结果，不是多源并存明细库。【DB-MARKET】【SQL010】

| source | 行数 | 指数代码数 | 日期数 | trade_date 首..末 |
| --- | ---: | ---: | ---: | --- |
| akshare | 12,258 | 4 | 5,493 | 19910406..20260611 |
| baostock | 26,331 | 4 | 8,659 | 19901219..20260611 |
| tencent | 742 | 7 | 106 | 20260612..20261117 |

这张表与 `market_indices` 不可混名：前者是日线，后者是截面快照；后者无来源列，也不会进入 close 的目标边界属于 #192 的决策，而非本轮代码改造事实。

### 4.2 `stock_daily` / raw / hfq / factor / state / switch

`stock_daily` 实际 18 列，含 `adj`、行级 `source`、后追加的 `tradestatus integer`；业务键含 `adj`。按 `adj/source` 聚合如下。不同来源的代码集合可重叠，不能加总“不同代码数”。【DB-ADJ】

| adj | source | 行数 | 不同代码 | 不同日期 | trade_date 首..末 |
| --- | --- | ---: | ---: | ---: | --- |
| none | baostock | 31,373 | 5,229 | 29 | 20260901..20261117 |
| none | tencent | 5 | 1 | 5 | 20261019..20261023 |
| qfq | baostock | 15 | 2 | 10 | 20260922..20261013 |
| qfq | efinance | 17,518,360 | 5,855 | 9,456 | 19901219..20261013 |
| qfq | tencent | 498,025 | 5,592 | 94 | 20260612..20261030 |

按 adj 合计：`none=31,378`（5,229 代码、34 日期），`qfq=18,016,400`（5,920 代码、9,469 日期）。legacy `tradestatus` 为 NULL/0/1 分别 18,016,405/64/31,309 行；raw 的 smallint tradestatus 则没有 NULL，0/1 分别 11,272/4,786,707 行。两表的空值语义不可互相套用。【DB-ADJ】

影子模型的实测轮廓：

| 表/状态 | 当前证据 |
| --- | --- |
| `stock_daily_raw` | 4,797,979 行、5,375 代码、938 日期，20221230..20261117；`source=baostock` 占全部行 |
| `stock_adjust_factor` | 17,615 行、4,445 代码，20230106..20261117；`source=baostock-adjust-factor` 占全部行 |
| `stock_daily_hfq` | 4,573,186 行、5,345 代码、937 日期，20230103..20261117；含 `factor_cum` 和 `rebuilt_at`，无行级 source |
| `stock_factor_state` | 5,375 行：5,343 行 pending=false 且因子成对非空；32 行 pending=true，其中 31 行两个因子均 NULL |
| `stock_adjustment_switch_state` | `id=1, mode=legacy, fact_watermark=NULL, switched_at=NULL, updated_at=20261117 11:44:56+08` |

raw 比 hfq 多 224,793 行、不同代码数多 30，但本轮没有逐行反连接归因，不能直接把差值全部命名为“缺失”“待重建”或“异常”。尤其 state 的 32 个 pending 与上述两个差值不是同一个指标。`stock_daily` 仍存在、switch 为 legacy、`stock_daily_legacy` 不存在共同表明当前不是“已切 new 且 legacy 已改名”的形态；不进一步推断调度是否完整部署。【DB-ADJ】

### 4.3 `market_boards` 的 source 与板块体系

| source | board_type | 行数 | 日期数 | trade_date 首..末 | 不同 board_code / name |
| --- | --- | ---: | ---: | --- | ---: |
| NULL | concept | 506 | 1 | 20261013..20261013 | 506 / 506 |
| NULL | industry | 496 | 1 | 20261013..20261013 | 496 / 496 |
| swsresearch | industry_l1 | 3,224 | 104 | 20260612..20261116 | 31 / 31 |
| swsresearch | industry_l2 | 12,896 | 104 | 20260612..20261116 | 124 / 124 |

NULL 行的 `created_at` 全为 20261013 17:12:28；swsresearch 行的 `created_at` 跨 20261026..20261117，说明业务日期早于物理回补/写入日期。20261102—20261116 的每个观测日期均为 31 个一级 + 124 个二级申万行，但这里没有检查 104 个日期是否均为交易日或是否连续。【DB-BOARDS】

注意唯一键是 `(trade_date,board_type,board_name)`，**不含 source**。因此同键不同来源不能作为两套并存事实保存；字典必须提示按板块体系和来源过滤，不应把 concept/industry 与申万两级混成同一个无量纲榜单。当前未观察到 `push2his`，不等于该值被约束禁止或历史上从未出现。【DB-CON】【DB-BOARDS】

### 4.4 LHB 五表：`trade_date` 与 `fetch_date`

| 表 | 行数 | 日期轴 | 不同日期 | 首..末 | created_at 首..末 |
| --- | ---: | --- | ---: | --- | --- |
| basic | 6,759 | trade_date | 103 | 20260616..20261116 | 20261029..20261117 |
| detail | 46,711 | trade_date | 90 | 20260706..20261116 | 20261117..20261117 |
| statistic | 105,690 | trade_date | 103 | 20260616..20261116 | 20261029..20261117 |
| yyb_capital | 645 | fetch_date | 4 | 20261013..20261019 | 20261013..20261019 |
| yyb_most | 380 | fetch_date | 3 | 20261013..20261019 | 20261013..20261019 |

basic 的 6,759 行全部 `lhb_reason<>''`；有 569 个 `(trade_date,stock_code)` 组包含多行，单组最多 6 行，实证唯一键不能退化成仅日期+股票。detail 的 trade_date 跨度早于集中写入时间，说明它是业务交易日而非 created 日期。【DB-LHB】

yyb 两表没有 trade_date。capital 有 176 行 `fetch_date=20261016` 而 `created_at::date=20261019`，所以 fetch_date 至少不能定义为“物理 INSERT 当天”。sql/007 注释把它称为“抓取日”；是否还承载上游榜单统计日/目标日语义，本轮库内聚合不能进一步证明，字典应保留这一边界，不与 basic.trade_date 做等值联接假设。【DB-LHB】【SQL007】

### 4.5 `collection_log` section/status

总体状态：ok 9,325、empty 519、error 1,477、planned_absent 0。ok 行 source 为 NULL 的有 9 行，error 行 source 为 NULL 的有 22 行（其余 error 行并非都无来源）；因此不能简化成“失败必无来源”或“有来源必成功”。`rows` 是单次动作写库行数，区间任务会重复记录，不能拿 sum(rows) 当表现存数。【DB-LOG】

| section | status | 行数 | trade_date 首..末 | sum(rows) |
| --- | --- | ---: | --- | ---: |
| boards | error | 2 | 20261019..20261019 | NULL |
| index_daily | ok | 23 | 20261012..20261117 | 1,280 |
| indices | ok | 1 | 20261019..20261019 | 1,125 |
| lhb | error | 6 | 20260612..20260812 | 0 |
| lhb | ok | 192 | 20260616..20261116 | 207,332 |
| market_boards | error | 10 | 20261016..20261018 | NULL |
| market_boards | ok | 310 | 20261026..20261116 | 27,435 |
| market_snapshot | error | 2 | 20261019..20261019 | NULL |
| stock_adjust_factor | empty | 92 | 20230103..20260414 | 0 |
| stock_adjust_factor | error | 3 | 20250213..20261117 | 10 |
| stock_adjust_factor | ok | 926 | 20230106..20261116 | 19,164 |
| stock_daily | empty | 427 | 20261014..20261030 | 0 |
| stock_daily | error | 1 | 20261016..20261016 | NULL |
| stock_daily | ok | 6,017 | 20261012..20261117 | 548,293 |
| stock_daily_raw | error | 3 | 20250214..20250214 | 3 |
| stock_daily_raw | ok | 1,842 | 20221230..20261116 | 5,964,122 |
| stock_factor_repair | error | 1,450 | 20230106..20261110 | NULL |
| stock_list | ok | 4 | 20261026..20261117 | 22,311 |
| strong_stocks | ok | 2 | 20261016..20261019 | 211 |
| sw_board_valuation | ok | 1 | 20261117..20261117 | 162 |
| sw_industry_meta | ok | 1 | 20261019..20261019 | 796 |
| zt_pool | ok | 6 | 20261015..20261019 | 357 |

当前 15 个 section 中没有 `conclusions`、`news`、`stock_factor_retracted` 台账行。因此“所有写库动作都有台账”的 DDL 注释不能由现库数据反向证明；也不能因为没有这些 section 就断言对应写入路径从未执行。status CHECK 已包含 planned_absent，但当前观测为 0，不可声称该第四态已有业务库样本。【DB-LOG】【SQL013】【SQL016】

### 4.6 conclusions/news 的日期语义

**conclusions。** 11 行全部是 market 级，按类型为：

| conclusion_type | 行数 | trade_date 首..末 | created_at 首..末 |
| --- | ---: | --- | --- |
| daily_review.close | 6 | 20261009..20261023 | 20261010..20261029 |
| db_connectivity.check | 1 | 20261012..20261012 | 20261012..20261012 |
| limit_up.close | 4 | 20261012..20261016 | 20261012..20261019 |

5 行的 trade_date 等于 created_at 的 yyyymmdd，6 行不等；不等组写入日比业务日晚 1—6 天。故 `trade_date` 是结论所针对的业务日，`created_at/updated_at` 是业务产物写入/修订时间，三者不可互换。样本全部为 market 不证明 stock 类型无用；未读取 payload，不对结论内容、质量或可重算性下结论。【DB-PRODUCT】

**news_items。** `publish_time` 是资讯发布时间，`fetched_at` 是入库抓取时间。210 行分布在 4 个 fetched_at 日期（10/10、10/12、10/13、10/15），发布时间却跨 9 个本地日期，最早可追溯到 20260109；`fetched_at-publish_time` 从约 5 分钟到 278 天以上，且没有 fetched_at 早于 publish_time 的行。抓取日不能当发布日期，首末 publish_time 也不表示检索窗口完整。【DB-PRODUCT】

| information_type | 关联行数 | 不同 news_code | source=NULL 行数 |
| --- | ---: | ---: | ---: |
| CFH | 4 | 4 | 0 |
| HOT_NEWS | 1 | 1 | 0 |
| INV_NEWS | 139 | 91 | 0 |
| NEWS | 40 | 28 | 0 |
| NOTICE | 13 | 13 | 13 |
| REPORT | 11 | 11 | 11 |
| WECHAT | 2 | 2 | 0 |

全表 210 行、150 个不同 news_code；market 75 行/75 个 news_code，stock 135 行/75 个 news_code、6 个不同 subject_code。同一 news_code 可挂多个 subject，所以关联行数大于资讯数符合 `(news_code,subject_type,subject_code)` 业务键，不应误当重复脏数据。NOTICE/REPORT 的 source 当前全 NULL，也只代表这一样本。【DB-PRODUCT】【SQL011】

### 4.7 其余选择性聚合

- `stock_list`：akshare 5,585 行、efinance 342 行；这是当前清单行来源，不提供上市/生效日期轴。
- `sw_industry_class`：12,929 行全部 `source_system=sw`，5,934 股票、553 板块；当前没有 em 行，不证明预留体系无用途。
- `sw_industry_meta`：level 1/2/3 分别 38/197/561 行；`sw_board_valuation` 仅 20261117，level 1/2 分别 31/131 行。
- `zt_pool`：zt/dt/zb 分别 1,061/271/381 行，各覆盖 19 个观测日期；首末值不证明逐日完整。
- `strong_stocks`：3,193 行、1,171 股票、18 个观测日期。该表没有 source 列，不应从 section 名反填行级来源。

这些统计均来自聚合查询，没有导出对应业务明细。【DB-COUNT】【DB-MISC】

## 5. 供 #192 / #193 使用的建议

### 5.1 #192：分类和判据应先确认的边界

1. **把“业务日期”“物理写入/修订时间”“控制水位”分栏。** trade_date、ex_date、effective_date、publish_time、fetch_date、latest_trade_date、fact_watermark 不可统一改名成 date，也不可拿 created_at/fetched_at/finished_at 替代。
2. **把行级来源、动作级来源和无来源分开。** `market_snapshot/stock_daily/stock_daily_raw` 有行级 source；collection_log 是动作审计；`market_indices` 无行级 source。建议字典使用 `unknown/not_recorded`，不要从邻表推断。
3. **stock_daily 按 adj 分逻辑事实。** 当前 qfq 与 none 同表并存；raw、factor、hfq、state、switch 又分别是原始事实、事件/因子、派生物化、每股控制状态和全局切读状态。是否标“原子/派生/控制”由 #192 拍板，本轮只提供形态证据。
4. **快照与历史可重取不得绑定。** market_snapshot 只有一个截面，market_indices 只有五个观测日，均不足以证明长期可重取或不可重取；需要独立上游研究。
5. **market_boards 是混合体系物理表。** source 与 board_type 联合暴露旧行业/概念和申万两级差异；唯一键不含 source，分类时不能声称它保存同键多源历史。
6. **LHB 需要表级+字段级分类。** basic 业务键必须保留 reason；detail 是 FK 关联记录；statistic 的 DDL 已声明库内窗口派生；yyb 两表是另一日期语义。是否将统计/排行改成查询时派生属于 #192 的目标决策，当前表和存量行仍存在。
7. **审计表不是完备性全集。** collection_log 可支持“有动作记录”的审计判断，但当前 section 覆盖和 sum(rows) 均不能替代逐表覆盖检查；status 四态是允许词汇，planned_absent 当前无实测样本。
8. **业务产物与资讯原文分开。** conclusions 是带业务日和 upsert 业务键的产物；news_items 是发布时间+抓取时间+多主体关联的资讯记录。news_code 数与关联行数是两个指标。

### 5.2 #193：数据库字典成稿建议

1. 全表矩阵覆盖上述实际 24 表，另设“条件对象”说明 `stock_daily_legacy` 当前不存在、切读后可能出现；不要漏掉它，也不要给当前行数。
2. 每表至少列：物理主键/业务唯一键、二级索引、业务日期轴、写入时间轴、行级 source 是否存在、当前观测行数及观测日。行数标“2026-11-18 实测快照”，不要写成稳定 schema 属性。
3. 单列“已确认物理漂移”：`lhb_stock_detail.id integer` 与 `sw_industry_class` 列顺序；不要在本票或字典成稿中顺手改库/改 DDL。
4. 为 market_indices、market_boards、stock_daily、LHB 五表、conclusions/news 设置“常见误用”条目，直接采用 §4 的反例。
5. 字典的“当前实现事实”和 #192 的“目标消费边界”必须分栏。例如 close 使用 index_daily、market_indices 不进入 close、market_snapshot 非必需入口均不能回写成已完成代码改造。
6. 对“历史可重取、上游修订、本地确定重算、快照不可恢复、审计、控制、业务产物 upsert”分别给判据和证据链接；本报告只提供现库证据，不给上游长期能力盖章。
7. 保留 NULL 与未知值语义，不把聚合展示用的 `<NULL>` 写成库内字符串；不在字典、issue 或示例中记录真实 DSN/用户名。

## 6. 未验证项与禁止外推

- 未做上游请求，未验证任何来源当前可达性、历史重取深度、修订行为、TPS、并发上限或长期持续服务能力。
- 未做交易日历逐日缺口、逐股票/指数连续性、跨表行级对账、raw→hfq 反连接归因或 32 个 pending 的逐票原因核验。
- 未验证文本日期的全列格式、非法日期、时区边界之外的上游时间语义，也未检查所有数值字段的单位、有限性和跨源一致性。
- 未读取结论 payload、新闻正文/raw、席位或股票明细；未对数据质量、内容正确性或业务结论可重算性作判断。
- 未检查非 public schema、触发器、权限/RLS、序列、函数、扩展和物理存储参数；本报告的“无额外对象”仅限 §1 所列 public relkind 查询范围。
- 多事务只读观测不是同一全局快照；首末日期和行数均可能随业务库后续写入变化。
- 当前未观察到某 source/status/section/表，不等于永久无用途或历史从未出现；当前有行同样不等于长期可重取。

## 证据索引

- 【I191】GitHub Issue #191，本票 Question/产出约束：<https://github.com/daanZao/q_stock_skills/issues/191>。
- 【I189】GitHub Issue #189，数据库字典 parent map：<https://github.com/daanZao/q_stock_skills/issues/189>。
- 【SQL001】`src/qstock_mcp/sql/001_stock_daily.sql`，条件 stock_daily/stock_daily_legacy 形态。
- 【SQL007】`src/qstock_mcp/sql/007_lhb.sql`，LHB 五表、键和 fetch_date 注释。
- 【SQL010】`src/qstock_mcp/sql/010_index_daily.sql`，指数日线与行级 source。
- 【SQL011】`src/qstock_mcp/sql/011_news_items.sql`，新闻业务键及 publish/fetch 字段。
- 【SQL012】`src/qstock_mcp/sql/012_sw_industry.sql`，成分字典建表与迁移。
- 【SQL013】【SQL016】`src/qstock_mcp/sql/013_collection_log.sql`、`016_collection_log_planned_absent.sql`，台账及四态。
- 【SQL015】`src/qstock_mcp/sql/015_market_boards_source.sql`，source 注释。
- 【SQL019】【SQL020】`src/qstock_mcp/sql/019_adjustment_shadow.sql`、`020_adjustment_switch.sql`，复权影子与控制表。
- 【DB-OBJ】2026-11-18 只读 `pg_class/pg_namespace/to_regclass` 查询，范围见 §1。
- 【DB-COL】同日只读 `pg_attribute/pg_attrdef/format_type` 全 24 表列形态查询。
- 【DB-CON】【DB-IDX】同日只读 `pg_constraint`、`pg_index/pg_get_indexdef` 全 24 表查询。
- 【DB-COUNT】同日 24 次逐表精确 count 与适用日期轴 min/max，全部成功。
- 【DB-MARKET】market_snapshot、market_indices、index_daily 的日期/source 聚合。
- 【DB-ADJ】stock_daily、raw、hfq、factor、state、switch 的 adj/source/tradestatus/state 聚合与控制行。
- 【DB-BOARDS】market_boards 的 source/board_type/日期/created_at 聚合。
- 【DB-LHB】LHB 五表日期轴、created_at、reason 分组及 yyb fetch/created 聚合。
- 【DB-LOG】collection_log 的 status、section/status、重点 source 聚合。
- 【DB-PRODUCT】conclusions/news 的类型、主体数、业务时间和写入时间聚合；未读取正文/payload。
- 【DB-MISC】stock_list、SW、zt_pool、strong_stocks 的选择性聚合。
