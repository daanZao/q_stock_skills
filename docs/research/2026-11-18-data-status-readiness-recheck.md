# `data-status` 就绪判据与接缝增量复核：#166/#167 与近期 LHB 修复（issue #188）

2026-11-18。Parent map：#174；旧盘点：#182。复核范围为 `16f011a..9868ddd`，
当前 HEAD `9868ddd`，throwaway 分支 `research/data-status-readiness-recheck-20261118`。
实际代码增量为 #166（`5fcb092`）、#167（`50a29ca`、`db3986a`）、一个不在本题语义范围内的
reconcile 工作流提交（`b443c42`），以及 #185/#186（`f956cce`）和 #187（`9868ddd`）。

## 0. 方法、边界与结论摘要

一手证据是当前源码、DDL、测试、项目手册和 ADR；旧报告只作为待复核命题，不作为现状依据。
本轮没有请求任何证券数据上游，没有连接或写入业务库，没有执行补数、切读或回滚，也没有实现 CLI。
验证使用 fake 适配器与隔离 PG 测试库，共 **343 passed**（命令与结果见 §9）。真实库的表水位、
台账分布和扫描耗时仍属于 #181，本报告不以测试库结果推断生产状态。

分类含义：

- **仍成立**：旧命题及其处置在当前 HEAD 仍直接适用；行号可能漂移。
- **需修正**：旧命题抓到了真实风险，但新增门禁、粒度或写模型改变了准确表述/复用方式。
- **已失效**：旧命题描述的用户可见错误已不再发生；若底层helper仍危险，会另列限制。

最高影响结论：

1. **#182 F1 的“默认 qfq 静默退化成 raw 有行即 ok”已失效。** `coverage` 现在追加
   `select_coverage`，pending、无锚、事件错误等会把 `status` 改成 `gap`
   （`src/qstock_mcp/tools_coverage.py:59-70`、`src/qstock_mcp/price_reader.py:381-402`），
   并有 raw 日期全齐但 pending 仍为 gap 的回归
   （`tests/test_price_reader_global.py:99-128`）。但 `select_dates` 仍只返回 raw 日期，
   `rows/first_date/last_date/missing_days` 仍是 raw 口径，且仍逐票查询；所以“F1 已修复”
   **不等于** `tools_coverage` 可成为 `data-status` 核心。
2. **切读后的状态不是单个 flag。** 必须同时观察 `QSTOCK_PRICE_SOURCE`、持久化
   `stock_adjustment_switch_state.mode`、`stock_daily/stock_daily_legacy` 物理表形态；三者由不同
   代码读写，可能不一致。`adj-switch status` 只返回持久化 mode，而且内部仍执行全部 DDL 和咨询锁，
   不能作为严格只读入口（`src/qstock_mcp/adjustment_switch.py:159-177`、
   `src/qstock_mcp/db.py:44-54`）。
3. **“legacy qfq 已全部停写”是不准确的。** daily/init 的写入口已改为 raw+B→hfq/state，new 模式
   morning 不再双写 legacy；但 legacy 查询自愈仍写 legacy，回滚修复也会重建 legacy qfq
   （`src/qstock_mcp/tools_daily.py:32-80`、`:378-423`，
   `src/qstock_mcp/adjustment_switch.py:45-83`）。`data-status` 应把 legacy 与 new 分成两个
   存储/质量视角，再以当前读取模式解释，不应把 legacy 尾日停滞直接判成新模型缺口。
4. **无锚不是“raw 缺失”，也不是可派生的 complete。** 无 B0 时可以保留 raw 与因子证据，
   state 为 NULL 锚 + pending，hfq/qfq 不生成；state 不存在才是 reader 的 `unknown`
   （`src/qstock_mcp/adjustment.py:315-397`、`src/qstock_mcp/price_reader.py:253-275`）。
   有有效锚但 pending 时仍可返回 hfq/qfq 行，只是 coverage=partial
   （`tests/test_query_daily_adjustment.py:149-169`）。
5. **复权台账新增了“全市场逐日 / 单票区间”混合格式，统一排序已不足以消歧。**
   `stock_adjust_factor` 同日可有 backfill、morning 和不同股票的 daily 行；`stock_daily_raw`
   同日可有全市场日行或区间尾日行。`NewPriceReader` 却只按 section+date 取最新一行，未限定写方
   （`src/qstock_mcp/price_reader.py:243-251`）。因此旧的“抽一个 latest_ledger_rows 统一排序”
   建议必须升级为“先限定写方/范围，再排序；不能判定时 unknown”。
6. **#185/#186/#187 没有改变 LHB 的三层完整性或 partial/error 契约。** 它们分别提高瞬时故障
   恢复率、接受并换算“元”单位净额、避免通用席位带同代码但不同金额被误拒。持续失败仍错误、
   完全重复席位仍拒收、净额一致性容差未放宽。日完整、席位结构完整和 yyb 快照仍须分开。

## 1. 旧 F1–F12 增量判定总表

| 旧命题 | 当前判定 | 当前一手证据与测试 | 对 `data-status` 的准确处置 | 同步票 |
| --- | --- | --- | --- | --- |
| F1 默认 qfq 在 new reader 下静默退化为 raw 有行 | **已失效（原用户可见错误）；底层 helper 风险仍需修正** | raw-only helper 仍在 `price_reader.py:308-315`；新增 quality gate 在 `tools_coverage.py:59-70`、`price_reader.py:381-402`；`test_price_reader_global.py:99-128` 断言 raw 全齐+pending→partial/gap | 不复用 `coverage` 整体；把 raw 日期与复权质量分列。不要再把旧 F1 原文当现状 | #175、#179、#180 |
| F2 既有入口都是窗口语义 | **仍成立** | `coverage` 要求窗口并逐日建日历 `tools_coverage.py:41-53`，CLI 仍强制窗口 `scheduler.py:747-769`；parity 自然日窗口 `parity_acceptance.py:189-212` | 新命令独立提供 as-of/有界回溯，不把窗口验收结果当全局水位 | #175、#178 |
| F3 presence-only 与缺一即 gap 相反 | **仍成立，且期望行数来源需按写方收窄** | parity `n>0` 即继续 `parity_acceptance.py:124-139`；coverage 集合差 `tools_coverage.py:28-38`；对应节律测试 `test_parity_acceptance.py:98-124` | 保留“期望 vs 实际”，但只有匹配写方的全市场台账 rows 才能做广度期望；单票区间 rows 不可当市场期望 | #177、#178、#181 |
| F4 精确日历集合会误伤停牌/上市/退市 | **仍成立** | `expected != actual` 直接 partial `price_reader.py:263-275`；raw gap 回归 `test_query_daily_adjustment.py:172-189` | 单票 reader 可作质量谓词参考；全市场必须结合生命周期、权威停牌行和已确认缺失日，不能直接按 stock_list×日历 | #177、#178、#179 |
| F5 LHB 日/详情/yyb 三种完整互不等价 | **仍成立** | 日复用 `tools_lhb.py:81-92`、`:123-147`；详情仅要求父键买卖两侧 `repository.py:716-728`；yyb 仍 fetch_date `repository.py:768-781`；详情/yyb 测试 `test_lhb_tools.py:396-442` | 三维分开；近期修复只改变有效数据产出，不合并判据 | #176、#177、#178 |
| F6 最新台账有四种排序 | **需修正：问题已扩大为粒度/写方消歧** | 旧排序仍在 `tools_coverage.py:115-120`、`parity_acceptance.py:108-121`、`ledger.py:116-139`；新增多写方见 `adjustment_pipeline.py:97-109`、`:201-222`，reader 未限定 detail `price_reader.py:243-251` | 唯一排序函数仍必要但不充分；注册表须含 writer/scope/detail 前缀或结构化等价物 | #177、#178、#179、#181 |
| F7 状态词表与面清单不统一 | **仍成立** | coverage 顶层 ok/error `tools_coverage.py:140-159`；parity ok/gap `parity_acceptance.py:227-242`；批次三态 `output.py:9-19`；reader 三级 `price_reader.py:270-275`；switch 另有 committed+partial_error `adjustment_switch.py:229-236` | 命令运行状态、数据完备等级、采集台账状态、切读动作状态分列，不能共用一个 `status` 猜语义 | #180 |
| F8 indices 面台账豁免双标 | **仍成立** | 普通 daily 面不允许 empty 豁免 `parity_acceptance.py:129-145`，market_indices 允许 ok/empty `:166-176`；`test_parity_acceptance.py:127-139` 只证明复用现状，不消除双标 | 日频/事件/派生节律进注册表，不复制 indices 特例 | #176、#177 |
| F9 因子覆盖按 backfill detail 前缀归因会漏 morning | **仍成立；新增 reader 混写方风险** | acceptance 仍限定 backfill 前缀 `adjustment_acceptance.py:806-812`；daily 采集识别 backfill/morning/daily 三类 `adjustment_pipeline.py:97-109`；reader 反而完全不限定 `price_reader.py:243-251` | 不把 acceptance 的窗口验收计数当全局覆盖；按明确写方集合归因，不能以“最新一条”掩盖其他写方 error | #179、#181 |
| F10 unknown 无统一归宿 | **仍成立，且 unknown/partial 边界已有实证** | state None→unknown、有 state 且异常→partial `price_reader.py:253-275`、`:393-402`；无锚 raw partial/qfq unavailable `test_query_daily_adjustment.py:125-146`；批次聚合仍只有三态 `output.py:9-19` | unknown 必须独立保留；无 state 与有 state 无锚不能合并 | #177、#180 |
| F11 per-stock/per-day 循环不适合全局扫描 | **仍成立；新增可移植集合 SQL** | coverage 仍逐票 `tools_coverage.py:58-71`；reader 单票多查询 `price_reader.py:202-251`；morning raw gap 仍逐票 `tools_morning.py:177-188`。另一方面 `_validation` 提供集合质量探针 `adjustment_audit.py:11-109` | 全局默认集合 SQL+有界窗口；reader 仅抽样互证。复用 `_validation` 时按 §4.4 修正 pending 解释 | #178、#179、#181 |
| F12 yyb 无交易日轴 | **仍成立** | `BOARD_QUERY_TABLES` 仍明确 fetch_date `repository.py:779-780`，DDL 业务键也按 fetch_date `sql/007_lhb.sql:87-122` | 只报快照 freshness/行数，不进入最新完整交易日 | #176、#178、#180 |

## 2. #166：切读、归档与回滚带来的新门禁

### 2.1 F1 的准确 closing condition

`verify_history_coverage` 先以 reader 的 `select_dates` 生成日期覆盖，再调用 new reader 的
`select_coverage`；后者复用 `query_daily`，将 qfq 无法派生归一为 state None→unknown、
有 state→partial（`tools_coverage.py:59-70`、`price_reader.py:381-402`）。所以：

- raw 全齐但 pending：`missing_days=[]`、`coverage=partial`、`status=gap`，已有定值回归
  （`test_price_reader_global.py:99-128`）。
- raw 全齐但无 B0：qfq `query_daily` 抛 `data_unavailable`，raw 可返回且 coverage=partial；
  `select_coverage` 不会把它变成 complete（`price_reader.py:253-254`、`:393-402`，
  `test_query_daily_adjustment.py:125-146`）。
- 仍不能据此把 `rows` 或 `missing_days` 标成 qfq：它们来自 raw-only `select_dates`
  （`price_reader.py:308-315`）。legacy 默认 reader 的原契约也未改变
  （`tests/test_price_reader.py:30-66`）。

结论应写成“**#166 已加防假阳性门禁，旧 F1 的静默 ok 不再成立；raw 日期字段和 N+1 形态仍不可复用**”。

### 2.2 三路状态必须一致，不能只读 mode 或 env

- DDL 新增单行控制表，只持久化 `mode`、事实时间戳水位和切换时间
  （`sql/020_adjustment_switch.sql:3-13`）。
- cutover/roll-forward 改名旧表并写 mode=new；rollback 反向改名，必要时修复 legacy
  （`adjustment_switch.py:178-215`）。DDL 在归档表存在时不会重建一个空 `stock_daily`
  （`sql/001_stock_daily.sql:4-31`），有测试 `test_adjustment_switch.py:37-73`。
- legacy reader 自己按归档表是否存在选择物理表（`price_reader.py:9-12`）；全局 reader 则由
  `QSTOCK_PRICE_SOURCE` 选择，缺省 legacy，非法值显式失败（`:427-432`）。
- switch 成功结果只回显 `required_env`，不会修改持久环境（`adjustment_switch.py:217-223`；
  `docs/adj-switch-cli.md:18-24`）。morning 是否双写也直接读 env，而非控制表 mode
  （`tools_morning.py:66-74`）。

因此 `data-status` 至少要输出/检查：`price_source_env`、`switch_mode`、`legacy_table_present`、
`active_legacy_table`、`adjustment_window_start`。若 env=new 但物理表/持久 mode 仍是 legacy，
或 env=legacy 但 mode=new，应给出明确 consistency warning/degraded 状态；不能静默选一边，
也不能把这种情况当成某个数据维度自身的缺口。

### 2.3 `adjustment_switch` 可复用与不可复用接缝

**可复用为规则参考，不宜直接调用：**

- 归档冲突、目标 mode、表存在性、生产显式开关和事务咨询锁是切换动作的安全门禁
  （`adjustment_switch.py:135-215`）；对应 cutover/rollback/roll-forward 测试在
  `test_adjustment_switch.py:37-131`，生产门禁测试在 `:219-234`。
- 修复回滚会处理事件导致的历史 qfq 重锚，无锚时删除新窗口陈旧 qfq，而不是伪造价格
  （`adjustment_switch.py:45-83`；`test_adjustment_switch.py:237-276`）。

**不可复用为就绪/水位判据：**

- `_fact_watermark` 是四表 `updated_at/rebuilt_at/last_rebuilt_at` 的最大时间戳，不是交易日，
  更不证明 raw 连续、事件完整或无 pending（`adjustment_switch.py:16-25`）。
- `_verify_legacy` 只验证 none/hfq/qfq 对应行是否存在及 stale qfq，不验证 raw 自身的完备性、
  事件证据、金额质量或最新完整日（`:86-121`）。
- `status` 仅返回 mode（`:175-177`），但进入该分支前已执行 `ensure_schema` 和咨询锁
  （`:159-160`）；`ensure_schema` 会执行所有 DDL 并 commit（`db.py:44-54`）。手册“不写业务表、
  不生成工件”最多只能说明不写业务数据/工件，不能据此认定它是只读事务
  （`docs/adj-switch-cli.md:28-31`）。严格纯读的 `data-status` 应自行 SELECT 控制表和
  `to_regclass`，不得调用 `run_adjustment_switch("status")`。

## 3. #167：daily/init/morning 的真实写模型

### 3.1 三个入口的共同点和差异

| 入口 | 当前事实写入 | legacy 行为 | 基线权限与质量门 | 测试证据 |
| --- | --- | --- | --- | --- |
| 单票 daily / new T+0 查询自愈 | baostock 优先 raw；逐日累计 B 证据；满足条件才重建 hfq/state。腾讯/新浪可补单票 raw，但替代源保持 pending/partial（`tools_daily.py:182-217`、`adjustment_pipeline.py:52-76`、`:177-267`） | 写入口的 `adj` 只回显，不直接写 qfq（`tools_daily.py:256-296`）；legacy 查询仍是另一路径 | 只消费已有 B0；raw 中段缺口不会被 head/tail 自愈自动补齐；factor error、alternative raw、invalid close 阻止 complete | `test_fetch_daily.py:364-448`、`:489-602`；`test_query_daily_adjustment.py:325-460` |
| `daily --all` / init 全量回溯 / adj-backfill | 复用逐日全市场 raw+因子，额外读取窗口外 D-1，随后本地重建（`tools_init.py:195-255`、`adjustment_backfill.py:1018-1056`） | 不写 qfq；init 测试明确 stock_daily=0 | 只有起点覆盖配置窗口且事件集完整才可建锚（`adjustment_backfill.py:1011-1014`）；end 落后于库内事实会拒绝（`:945-961`） | `test_init_full.py:113-143`；`test_adjustment_backfill.py:1184-1224` |
| morning | 一把抓前一交易日全市场 raw+逐日 B，并重建受影响派生（`tools_morning.py:51-82`、`:191-300`） | env=new 时 `legacy_rows=0`，raw/因子继续；否则 legacy 同键双写 | 通常消费已有锚，只有无 state、无历史 raw、当日无事件的新股首日允许 B0=1（`:234-262`）；权威 raw 可修订 T+0 并清 pending | `test_morning_batch.py:152-184`、`:910-942` |

**legacy qfq 停写的精确表述**：#167 的 raw+B 写入口不再直接写 qfq，new morning 停止双写；
并非所有进程/模式都物理停止 legacy 写入。legacy `ensure_coverage` 仍 upsert legacy
（`tools_daily.py:32-80`），修复回滚也会写三种 legacy adj
（`adjustment_switch.py:45-83`）。`data-status` 不应将“legacy 尾日不再推进”作为 new 模式故障。

另有一处文档漂移：`sql/001_stock_daily.sql:1-3` 头注仍把“init 全量回溯”列为 legacy 写方，
与当前 `tools_init.py:195-255` 和 `test_init_full.py:113-143` 相反。维度登记表应以代码/测试为准，
后续实施时修正该头注，不能复制到 spec。

### 3.2 baseline / pending / partial / unknown 的机械边界

- state 形状由 DDL 约束：B0 与 F_latest 必须同时 NULL 或同时非 NULL；无 B0 时只能 pending
  （`sql/019_adjustment_shadow.sql:60-75`）。
- 无 B0 且不允许建锚：raw 与因子保留，state 写 `baseline_factor=NULL`、
  `factor_latest=NULL`、`rebuild_pending=true`，hfq/qfq 为空
  （`adjustment.py:315-397`；`test_adjustment.py:158-192`）。
- 有锚但事件对账 mismatch/unverified：可以保留 B0/F_latest 和 hfq 行，同时 pending=true
  （`adjustment.py:340-357`、`:450-470`；`test_adjustment.py:463-511`）。reader 此时返回行但
  coverage=partial（`test_query_daily_adjustment.py:149-169`）。
- state 不存在：reader coverage=unknown；state 存在但无锚：raw partial，qfq data_unavailable，
  coverage 入口归一为 partial（`price_reader.py:253-275`、`:393-402`）。
- `rebuild_stock` 返回快照自己的 `coverage` 在成功且无 pending 时仍写 `unknown`
  （`adjustment.py:500-503`）。它是内部重建结果，不应成为 `data-status` 的全局词表来源；
  对外单票语义以 `NewPriceReader`/显式 SQL 事实为准。

### 3.3 窗口边界不是固定文案，而是配置输入

`QSTOCK_ADJ_WINDOW_START` 缺省 20230101，但可配置（`adjustment.py:11-16`）。新 qfq 跨界拒绝，
窗口前 qfq 只读 legacy 且 coverage=partial；窗口外 hfq/raw 不反推
（`price_reader.py:172-195`；`test_query_daily_adjustment.py:192-229`）。回填的 D-1 raw 可留作
审计/计算输入但不进入 hfq（`adjustment.py:258-281`、`:417-435`；
`test_adjustment.py:431-460`）。window_end 也不得回退窗口外派生
（`adjustment.py:166-185`；`test_adjustment.py:534-579`）。

所以 spec/输出不能硬编码“2023 以前都缺失”；应报告实际 `adjustment_window_start`，把
legacy-only 区间标为不同 basis。D-1 的存在也不能算作窗口内覆盖一天。

## 4. 可复用与不可复用接缝（按指定模块）

### 4.1 `price_reader`

**可复用为单票策略/抽样互证：**

- 新旧边界和错误分类（`price_reader.py:172-195`）。
- pending、请求窗 raw 缺口、invalid close、state 陈旧、事件台账 error 的质量谓词
  （`:263-275`），以及 data_unavailable→unknown/partial 的归一（`:381-402`）。
- legacy 归档物理表选择规则（`:9-12`）。

**不可直接复用为库级水位：**

- `select_dates`/`select_latest_date`/`select_window_anchor` 都是 raw 日期，不检查 hfq/state；
  `select_dominant_adj` 只要当日任一 raw 行就返回 qfq（`:308-339`）。
- `query_daily` 是单票、多 SQL、请求窗精确日历判据；逐票调用既会把停牌/生命周期边界变成
  partial，也形成 N+1。它只适合做抽样，不是全局扫描计划。
- 事件日志未区分写方粒度（见 §5），所以即使逐票调用，也不能把结果当成无歧义的全局真源。

### 4.2 `tools_coverage`

可保留的是“显式窗口、交易日历、输出逐实体缺口”的验收器角色，以及 new 模式追加 quality gate
的防假阳性思路（`tools_coverage.py:41-71`）。不可复用的是整体入口：仍逐票、二值 gap/error、
raw 日期字段与 qfq 参数并存（`:28-38`、`:58-71`、`:140-159`）。LHB 段仍只看
basic/statistic/台账，不看详情，而且 basic=0 时 ok 或 empty 都算 ready
（`:101-136`），与 `tools_lhb` 只复用“有行+ok / 无行+empty”的规则不同
（`tools_lhb.py:123-129`）。#182 的“新建独立核心，不扩展 coverage”建议仍成立。

### 4.3 `tools_strong` 与 `tools_sw_board`

这两处是**消费者正确性接缝**，不是上游完备性证明：

- new qfq 截面通过 hfq+state 内连接取价，缺 hfq 不回退 raw
  （`price_reader.py:341-379`）；强势股测试 `test_price_reader_global.py:131-153` 已钉住。
- 但 `compute_strong_rows` 只在“完全无 raw/无窗口锚”时返回 None；raw 存在而 qfq 全部不可派生时
  可得到空列表（`tools_strong.py:87-100`）。工具层对 None 记 empty，对空列表仍记 ok+0 行
  （`tools_board.py:229-264`）。因此 strong_stocks 的 empty/0 行不能证明输入完整，也不能区分
  “确无强势股”与“派生输入不可用”。它是派生维度，必须另查 raw+hfq/state+zt_pool 输入门。
- 申万自算 new 分支直接 join raw，只用与复权无关的 change_percent/amount
  （`price_reader.py:404-417`）；聚合正确性测试在 `test_price_reader_global.py:63-96`。
  所以申万有行不是 qfq/hfq 就绪证据。
- `query_sw_board_daily` 虽被描述为纯查询，却调用 `ensure_schema`
  （`tools_sw_board.py:77-95`）。严格只读的 `data-status` 不应调用这个高层入口；若需要其
  point-in-time 聚合规则，只提取 SQL/纯函数，不能带入 DDL 副作用。

### 4.4 `adjustment_audit` / `adjustment_acceptance`

`_validation` 仍是最好的集合化复权质量探针之一：raw/event/hfq/state、pending、invalid close、
alignment 和 no-event 分类在一条 SQL 中汇总（`adjustment_audit.py:11-109`）。但 #167 后要做两项
解释修正：

1. `valid_raw_without_hfq` 当前对所有有效 raw 计数（`:76-80`），其中包括“无 B0、按设计不生成
   hfq”的票。它可以作为 partial 的信号，不能未经 state 分类就全部称为派生丢失。
2. `no_event_bad` 的强结论仍以 `event_set_complete` 为前置（`:39-54`、`:94-109`）；事件日失败时
   不能把无事件票分类 mismatch 当成已证明。

`adjustment_acceptance` 仍依赖迁移工件、backfill detail 前缀和逐票 reader
（`adjustment_acceptance.py:806-812`、`:837-875`），保持“迁移硬验收器”边界，不变成日常水位入口。

## 5. 新增最关键接缝：复权台账的写方与范围

### 5.1 同一个 section 已不是一种行形状

- 单票 daily 的 raw 台账以**请求区间尾日**为 trade_date，rows 是各自愈段写入合计
  （`tools_daily.py:83-108`、`:212-217`）。
- 全市场 backfill 的 raw 台账是**一个真实交易日一行**，rows 是该日全市场行数；断点只有
  匹配 backfill 前缀且库内行数一致才复用（`adjustment_backfill.py:575-654`、`:1022-1040`）。
- morning 的 raw 广度期望记在 section=`stock_daily`，即使 new 模式 legacy_write=disabled，
  rows 仍是上游全市场 raw 行数（`tools_morning.py:66-75`）。不能看到 section=stock_daily 就把
  rows 当 legacy qfq 写入数。
- 因子台账同时有 backfill、morning 和 `daily:<code>`；daily 的单票 ok/empty/error 也按日追加
  （`adjustment_pipeline.py:97-109`、`:201-222`）。backfill 的 rows 还是实际存储事件数，同 B
  非阶跃行会跳过，不能当上游原始返回行数（`adjustment_backfill.py:330-342`、`:460-471`）。

因此旧报告 §12.3“优先拿 collection_log.rows 对账”的方向仍对，但必须限定为：

1. raw 市场广度：只取同一全市场写方、同一窗口/日、成功状态的对应行；morning 与 backfill 分列。
2. 单票 daily 区间行：只用于该 code+range 的采集审计，不进入全市场日行数比较。
3. 因子：ok/empty/error 主要用于“证据是否确认”；事件数不是市场广度，empty 可正常。
4. 无法从 detail 唯一确定写方/范围时输出 unknown，不猜、不拿最近一行顶替。

### 5.2 `NewPriceReader` 的跨写方干扰是新风险

reader 对窗口内 `stock_adjust_factor` 直接 `DISTINCT ON (trade_date)`，按
`finished_at DESC,id DESC` 取一条，没有 detail/source 写方过滤（`price_reader.py:243-251`），
再把任何最新 error 作为 unresolved（`:268-275`）。在全市场采集后又有单票 daily 的现实序列中：

- 后发生的其他股票 daily error 可能令本股票 partial；
- 后发生的单票 ok/empty 也可能遮住较早的全市场 error。

本轮现有测试没有覆盖“同日不同写方/不同 code 的交叉干扰”，所以这是一项**由当前 SQL 直接证明、
尚无回归测试的接缝风险**，不是声称已观察到生产误报。`data-status` 不应复制该查询；
#179 应决定是否同时修正 reader 的证据范围，或在新核心里显式规避。

同理，append-only 的 `stock_factor_repair`/`stock_factor_retracted` 是历史审计事实，不是“存在即
永久未解决”的当前状态。当前 readiness 应以 state、后续成功重建和对应窗口证据联合判断；
retracted 标记可要求后续 hfq/state 已重锚，但不能单独永远拉低水位
（标记产生见 `adjustment_backfill.py:307-381`）。

## 6. #185/#186/#187：LHB 判据是否改变

### 6.1 日完整性与 partial/error：规则不变

`_is_complete_lhb_day` 仍要求来源含 exchange/ths，并只豁免 yyb 两个子项错误
（`tools_lhb.py:81-92`）；复用时仍要求有 basic+ok 或无 basic+empty，详情另行判定
（`:123-147`）。#185 只在深交所个股详情和上交所文件边界加入最多 3 次、0.5/1 秒退避
（`lhb_exchange_adapter.py:252-263`、`:289-291`、`:306-309`）：

- 瞬时失败第二次成功，不再留下 market error，因而可以提高后续“完整日”产出率
  （`test_lhb_exchange_adapter.py:346-400`）。
- 持续失败仍恰好 3 次并保留 errors，不会改判成功（`:366-416`）。
- yyb 子项失败仍可在工具响应中带 partial_error 而保留 basic/statistic
  （`test_lhb_tools.py:396-407`）；席位详情 partial/missing 仍是独立降级信号
  （`:410-442`）。

所以不需要改 LHB 日完整公式，也不能因为“加了重试”就把持续 error 当 empty。

### 6.2 席位详情：结构判据不变，拒收边界更精确

`select_complete_lhb_detail_parent_keys` 仍只证明某 basic 父键至少有 buy、sell 各一行
（`repository.py:716-728`）。#187 修改的是进入库前的拒收规则：机构专用/沪深股通专用等通用席位，
即使带 seat_code，也只按方向+代码+规范化名称+买/卖/净额的完整行身份判重；具体营业部仍代码优先、
无代码按名称（`_lhb_detail.py:1-33`）。同代码不同金额的通用席位有效，完全重复仍拒绝
（`test_lhb_tools.py:445-475`）。

这会减少错误的 `detail_missing`，但不会自动修复既有缺口，也不意味着详情 SQL 已验证金额质量。
`data-status` 可复用父键买卖两侧 SQL 作为**结构完整门**，并另列：

- 详情 partial/missing、detail_error 和来源；
- 可用金额的 `(buy-sell)-net` 一致性；
- 通用席位完全重复/具体营业部代码或名称重复作为质量信号；
- 官方源缺席位代码、反向金额或净额可为结构性 NULL，不能与 THS 完整字段同一硬门。

### 6.3 金额质量：增加“元”合法输入，不放宽一致性

#186 让 `_span_amount` 接受“元”，并统一经 `_money_cn` 转成万元
（`lhb_ths_adapter.py:41-52`、`_num.py:38-51`）；`buy-sell` 与 net 的一致性检查仍在原处，
容差未放宽（`lhb_ths_adapter.py:137-142`）。回归确认元单位净额与万元买卖额可同时解析且结果仍
为万元（`test_lhb_ths_adapter.py:139-152`）。

因此字段级质量门应保留：金额单位为万元、`total_amount=buy+sell`、可用时净额一致；但不能把
`close_price/change_percent` 等官方结构性 NULL 当错误（DDL/字段现状见 `sql/007_lhb.sql:7-21`，
来源说明见 `lhb_exchange_adapter.py:8-12`）。本次 LHB 增量没有新增表、日期轴或状态词，
不需要重分组；只应把上述解析/判重规则加入 #177 的质量判据和 #180 的验收种子。

## 7. 建议同步到 #175–#181 的准确要点

### #175 命令定位与边界

- 保持独立 `data-status`，不扩展 coverage：F1 的静默 ok 已消失，但 coverage 仍是逐票、窗口、
  raw 日期+gap/error 验收器，并未变成全局水位核心。
- “纯读”必须是连接/事务级保证。**不要调用** `adj-switch status` 或 `query_sw_board_daily`：
  两者都会 `ensure_schema`；直接 SELECT 控制表、物理表与业务事实。
- 默认 as-of 仍可用最近已收盘交易日，但复权另输出实际 `QSTOCK_ADJ_WINDOW_START`；读取模式须
  同时展示 env、持久 mode、归档表形态，并定义不一致时的降级/错误契约。

### #176 维度清单与分组

- 候选范围从 `sql/001-019` 扩到 `001-020`；新增 `stock_adjustment_switch_state` 是控制/元数据，
  不参与业务水位。`stock_daily_legacy` 是 legacy 维度的物理归档名，不是另一业务维度。
- new 复权可作为一个维度，内部强制分 raw、逐日 B 证据、hfq、state/锚四个子门；legacy 单列，
  不用其停写/尾日判断 new 是否完整。
- strong_stocks 是 raw+hfq/state+zt_pool 的派生；申万自算是查询期 raw 派生，不是 qfq 质量证据。
- LHB 日、席位详情、statistic、yyb 快照的归属不因 #185–#187 改变。

### #177 完备判定模型

- 采用明确矩阵：无 state=unknown；有 state 无 B0=pending/partial 且 hfq/qfq 不可派生；有锚但
  pending=可有用旧/部分派生行但仍 partial；alternative raw、raw gap、invalid close、因子 error
  均不得 complete。
- raw 有行、strong 空/有行、申万自算有行都不能单独证明复权完整。派生维度先查输入门。
- 台账规则从“最新一行”升级为“写方+范围+状态”；不能消歧则 unknown。事件 empty 正常，raw 单票
  区间 rows 不作全市场期望。
- LHB 保留三层判据；金额万元、净额一致、通用席位完全重复与具体营业部判重列入质量门，
  官方结构性 NULL 白名单化。

### #178 最新完整日与水位

- `max(raw.trade_date)` 只能作为 `latest_data_date`；switch `fact_watermark` 是写入时间戳，二者
  都不是 latest_complete_date。new 复权水位必须在 raw+B+hfq/state+pending/事件门后推进。
- 无锚 pending 不得推进 hfq/qfq 水位；有锚 pending 也不能因“有行”推进 complete 水位。
- 台账行数对账仅使用匹配全市场写方的成功日行；单票区间尾日不能冒充市场水位。
- LHB 重试只改变重跑后可获得的完整日，不改变水位算法；详情水位与 basic 日水位继续分开，
  yyb 不参与交易日水位。

### #179 复权维度判据

- 将旧 F1 更新为：select_coverage 已阻止 raw-only 假 complete；不要继续传播“当前仍静默 ok”。
  同时保留 raw-only select_dates/latest/dominant 和 N+1 的不可复用结论。
- 复用 reader 的边界与质量谓词作单票策略参考，库级判定用集合 SQL；输出实际 window_start，
  区分 legacy-only、无锚、pending、state absent。
- 明确处理三路切读状态一致性，不能把 env 或 switch mode 单独当真源。
- 把“factor log 未限定写方，可能跨 code/写方遮蔽 error”列为新接缝风险；决定 reader 是否修正，
  新核心不得复制。repair/retracted 是需与后续 state 联合解释的历史证据，不是永久未决。
- `_validation` 可移植，但 valid_raw_without_hfq 须按 B0/pending 分类，不能一律称异常丢行。

### #180 输出契约与验收原型

- 增加 `price_source_env`、`switch_mode`、`physical_price_tables`、
  `adjustment_window_start`、`ledger_scope/basis`；命令运行 error、数据 complete/partial/
  missing/unknown、采集 ok/empty/error、switch committed/partial_error 不共用一个字段。
- unknown 必须可输出；latest_data_date 与 latest_complete_date 并列，时间戳 fact_watermark 若展示
  必须另名且注明非交易日水位。
- 种子至少覆盖：raw 全齐+pending、无 state、无 B0、有效锚+pending、alternative raw、归档后
  legacy/new、env/mode/table 不一致、同日全市场+单票混合台账、LHB 元单位净额、通用席位同码异额、
  完全重复、持续重试失败。全程不输出实际连接串或用户名。

### #181 本地库现状与性能盘点

- 盘点对象加入 switch 控制表和 stock_daily/stock_daily_legacy 物理形态；不要调用带 DDL 的
  高层“status/query”入口。本研究未检查业务库，实际是否切读仍须该票以只读 SQL 证实。
- collection_log 不只按 section/status 聚合，还要按写方前缀、单票/全市场范围、同日多行分布统计；
  专门寻找 factor 同日跨 code/写方样本，以评估 §5.2 风险。
- 复权统计加入 raw source 分布、state absent/无锚/pending 分布、raw/hfq/state 尾日差、repair/
  retracted 与后续重建关系；不要只统计 state 表已有代码而漏掉 raw 有行但无 state 的票。
- 性能实验优先集合 SQL/EXPLAIN；不要逐票 query_daily，也不要把 switch 时间戳当日期水位。
- LHB 可抽查万元/净额一致性、详情买卖两侧和重复类型，但本票只诊断，不回填历史缺口。

## 8. 对旧模块边界建议的最终判断

#182 的“新建 `tools_data_status.py`，不扩展 `tools_coverage`，只向下依赖只读原子层”仍是最稳
方案，但依赖图需增加两条限制：

1. 切读状态直接读控制表/系统目录，不 import 或执行 `adjustment_switch` 动作入口。
2. 不为“纯读复用”调用 `tools_sw_board.query_sw_board_daily`，也不把 price_reader 的高级查询
   放进全市场循环。可共享纯判据/SQL，但不共享带 DDL、写库或采集副作用的入口。

LHB `_is_complete_lhb_day` 下沉为纯台账判据的旧建议仍有效；除此之外，新核心还需要一个明确的
“台账写方/范围注册表”，这是 #167 后比统一排序更优先的接缝。

## 9. 验证记录与未验证项

### 9.1 本轮实际执行

以下两条命令均使用仓库现有 fake 适配器与隔离测试库，无证券数据上游请求：

```bash
.venv/bin/pytest -q \
  tests/test_price_reader_global.py tests/test_adjustment_switch.py \
  tests/test_fetch_daily.py tests/test_query_daily_adjustment.py \
  tests/test_init_full.py tests/test_morning_batch.py \
  tests/test_adjustment_backfill.py tests/test_adjustment.py \
  tests/test_lhb_exchange_adapter.py tests/test_lhb_ths_adapter.py \
  tests/test_lhb_tools.py
# 261 passed in 144.81s

.venv/bin/pytest -q \
  tests/test_parity_acceptance.py tests/test_scheduler_parity.py \
  tests/test_scheduler.py tests/test_price_reader.py \
  tests/test_collection_log.py tests/test_query_daily.py tests/test_sw_boards.py
# 82 passed in 117.97s
```

合计 **343 passed，0 failed**。这些结果验证当前代码的契约与构造样本，不证明生产数据已修复、
生产切读已执行或全市场性能已达标。

### 9.2 明确未验证/留给下游

- 未连接业务库，未记录实际 DSN/用户名，未核实生产 mode、物理表、水位、pending 分布或 LHB 存量
  缺口；全部归 #181 的只读盘点。
- 未发任何上游请求，因此 #185/#186/#187 的真实页面/长期成功率不在本轮新增证据内；本轮只验证
  当前 fixture/fake 回归与代码契约。
- 没有为 §5.2 新风险写业务代码或新增测试；报告只指出当前 SQL 的确定行为和现有测试空白。
- 没有修改 #174、#175–#181，也没有关闭 #188；issue 同步由父会话执行。
