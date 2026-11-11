# 现有覆盖/就绪代码盘点：`data-status` 的可复用接缝与语义冲突（issue #182）

2026-11-11，基线 `16f011a`（`docs: clarify parity CLI test contract (#122)`），分支
`research/data-status-readiness-inventory`。范围：`src/qstock_mcp` 15 个模块
（`tools_coverage` / `parity_acceptance` / `tools_index` / `tools_lhb` / `tools_board` /
`price_reader` / `adjustment_audit` / `adjustment_acceptance` / `tools_morning` /
`repository` / `ledger` / `output` / `dates` / `gaps` / `scheduler`）与四份文档
（`docs/knowledge-graph.md`、`CONTEXT.md`、ADR 0018、`docs/research/2026-10-31-price-consumers-inventory.md`）。

**方法与边界**：纯本地静态阅读（`read`/`grep`），未触网、未连库、未跑测试、未改动任何
业务代码；本报告是唯一新增文件。所有行号按当前 HEAD 重新核对（漂移说明见 §14.2）。
库内实际行数与水位不在本文范围——那是 #181（本地库现状与全量扫描性能盘点）的产物。

**判定口径**（本文三分类只回答"新命令能不能直接拿这段代码当完备判据"）：

| 分类 | 含义 |
| --- | --- |
| **可复用** | 语义与"全局逐维完备 + 最新完整交易日"一致，可直接调用或直接搬运，无需改契约 |
| **需改造** | 规则/形状有价值，但窗口、粒度、词表或查询形态与目标不符，必须改写后才能用 |
| **不要复用** | 语义与目标冲突（会把误报写进新命令），只能作为反例或规则参考 |

## 0. 结论摘要

一句话：**现有代码几乎全是"窗口内、逐票循环、二值状态、面内自洽"的验收器，没有一处能
直接回答"全局每维完备到哪一天"**；可复用的是**原子能力**（按日行数、台账归因、日历、
状态词表、复权对齐 SQL），不是任何现成的"覆盖核验"入口。

共盘点 **59 个接缝**：可复用 22、需改造 21、不要复用 16。

| 分类 | 处数 | 典型 |
| --- | --- | --- |
| 可复用 | 22 | `_table_date_counts`、`aggregate_batch_status`、`LEDGER_STATUSES`、`trading_day_window`、`_validation`、`select_complete_lhb_detail_parent_keys` |
| 需改造 | 21 | `_FACE_TABLES`、`_ledger_statuses`、`verify_index_coverage`、`_is_complete_lhb_day`（位置）、`_stock_daily_part` 台账行数 |
| 不要复用 | 16 | `verify_history_coverage` 整体、`_check_table_continuity` 判定、`price_reader` coverage 判定、`_check_structure_coverage`、`head_tail_gaps`（相反语义） |

最高影响的六条（详见 §10）：

1. **`verify_history_coverage` 的 `adj="qfq"` 默认在切读后是失效承诺**：`cmd_coverage`
   从不传 `adj`（`scheduler.py:710`），而 `NewPriceReader.select_dates` **完全不看 `adj`**
   （`price_reader.py:292-299`）——`QSTOCK_PRICE_SOURCE=new` 时"qfq 覆盖"静默变成"raw 有行"。
   现有测试只断言 legacy 语义（`tests/test_price_reader.py:45-66`），无 new+coverage 用例。
2. **"有行即可"与"逐日缺一即 gap"是两套相反判据**：`parity` 只看 `n > 0`
   （`parity_acceptance.py:133-135`），3 行的日子也算覆盖；`tools_coverage` 缺任一自然交易日
   即 gap（`tools_coverage.py:28`）。真正需要的"期望 vs 实际"计数，现有代码只在
   `adjustment_audit._validation`（`adjustment_audit.py:11-106`）里做过一次。
3. **期望值不必猜阈值**：`collection_log.rows` 已记录每次采集实际写入的行数（通常等于上游当日返回行数）
   （`tools_morning.py:64-71`、`tools_index.py:131-142`、`tools_board.py:912-922`），
   可直接作为"当日应有多少行"。`morning` 早于映射表就把这条证据写进库了。
4. **lhb 有三个互不等价的"完整"**：日完整（`tools_lhb.py:81-92`）、席位详情完整
   （`repository.py:716-728` + `detail_status`）、营业部快照（`lhb_yyb_*` 按 `fetch_date`，
   `repository.py:779-780`，无交易日语义）。且被 `backfill` 复用的日子**不写新台账行**
   （`tools_lhb.py:319-346`），台账存在性既非必要也非充分。
5. **台账"当日最新一行"有四种排序**：`id DESC`（`tools_coverage.py:106`）、
   `finished_at DESC` 无 tie-break（`parity_acceptance.py:117`）、`finished_at DESC, id DESC`
   （`ledger.py:130`、`price_reader.py:231`）。同一天可能被两个命令判成不同状态。
6. **per-stock / per-day 循环是结构性性能问题**：`verify_history_coverage` 每股一次查询
   （`:58-59`），`_existing_lhb_inputs` 每日三次 `SELECT *`（`:123-137`），
   `_check_structure_coverage` 逐票调 `query_daily`（`:830`）。全局扫描必须集合化。

模块边界建议（详见 §11）：**新建 `tools_data_status.py` 承载注册表 + 判定策略 + 组装，
只向下依赖 `repository/ledger/dates/output/db`**；不扩展 `tools_coverage`（其 `coverage`
契约是"有缺口即 exit 1"的硬门，与"不完备不算失败"直接冲突），也不建通用 readiness 框架
（当前只有一个消费者）。唯一需要下沉的既有规则是 `_is_complete_lhb_day` 的纯判据，
移到 `ledger.py` 由两边共用。

## 1. 现状地图：这些模块各自在回答什么问题

| 模块 | 对外契约 | 时间轴 | 粒度 | 判定输出 |
| --- | --- | --- | --- | --- |
| `tools_coverage.verify_history_coverage` | CLI `coverage`（`scheduler.py:690-712`） | 显式 `start..end` | 逐票 / 逐指数 / 逐板块 / 逐日 | `ok` / `gap` → 顶层 `ok`/`error` |
| `parity_acceptance.check_history_continuity` | CLI `parity`（`scheduler.py:803+`） | `window_days` 自然日回溯 | 逐面 / 逐日 | `ok` / `gap` |
| `tools_index.verify_index_coverage` | CLI `index --verify` | 缺省 31 自然日 | 逐指数 | `ok` / `gap` → `ok`/`partial_error`/`error` |
| `tools_lhb._is_complete_lhb_day` + `_existing_lhb_inputs` | CLI `lhb`（内部复用判定） | 逐日循环 | 逐日 / 逐父记录 | 可复用 / 不可复用（布尔） |
| `price_reader.query_daily` coverage | MCP `query_daily`（`server.py:114`） | 请求窗口 | 单票 | `complete` / `partial` / `unknown` |
| `adjustment_audit._validation` | `adj-acceptance` 内部（`adjustment_acceptance.py:728`） | 窗口 | 全库计数 | 计数，无等级 |
| `adjustment_acceptance._check_structure_coverage` | `adj-acceptance` | 窗口 + 迁移产物 | 全库 + 逐票 | `ok` / `error` |
| `tools_morning.*_part` | CLI `morning` | 单目标日 | 全市场 / 逐指数 | `ok`/`empty`/`error` + `error_kind` |
| `repository` / `ledger` / `dates` / `output` | 无独立契约 | — | 原子能力 | — |

结论：**没有一处同时具备"全局 + 三维等级 + 水位"**。`coverage` 和 `parity` 是验收器
（缺口即失败），`price_reader` 是单票读取门控，`adj-acceptance` 是迁移证据生成器。

### 1.1 由代码证据支持的节律分型（供 #176 用，不是决定）

| 型 | 判据 | 表（现状） | 备注 |
| --- | --- | --- | --- |
| 每日 | 每个交易日应有行；缺行即缺口，台账 `empty` 不能豁免 | `stock_daily`（legacy）、`stock_daily_raw`、`index_daily`、`market_snapshot`、`market_boards`、`market_indices`、`sw_board_valuation` | 与 `_FACE_TABLES` 的 `cadence="daily"` 同义（`parity_acceptance.py:54`、`:59`） |
| 事件 | 无行可为正常，须台账 `ok/empty` 确认 | `lhb_basic`、`lhb_stock_detail`、`lhb_stock_statistic`、`zt_pool` | `parity_acceptance.py:64`、`:69`；lhb 另有详情/yyb 两层（§5） |
| 状态快照 | 无交易日轴，只有"最新状态" | `stock_factor_state`、`lhb_yyb_capital`、`lhb_yyb_most` | `fetch_date` 语义（`repository.py:779-780`，issue #124） |
| 派生 | 完整度由父事实 + 状态派生，不独立计行 | `stock_daily_hfq`、`strong_stocks`、qfq（不物化） | ADR 0018:13；`stock_daily_hfq` 另有每日轴，但 completeness 取决于 raw 有效 + 非 pending |
| 元数据 | 只报行数与最新生效日，不参与水位 | `stock_list`、`sw_industry_class`、`sw_industry_meta`、`conclusions`、`news_items`、`collection_log` | `#174` 已定 `conclusions`/`news_items` 只报行数 |

注意：这是**表级**分型，双身份表（如 `stock_daily_hfq` 既有每日轴又是派生）必须只按"派生"
判定，否则会把"raw 缺失导致没有 hfq"记成 hfq 维度自己的缺口（F4 同型误报）。

另：`verify_history_coverage`、`check_history_continuity` 均**未注册为 MCP 工具**
（`server.py` 19 个工具无此二者），`data-status` 走 CLI-only 与现状一致。

## 2. `tools_coverage.py`：个股窗口覆盖 + lhb 台账判定

| 位置 | 做什么 | 分类 | 说明 |
| --- | --- | --- | --- |
| `tools_coverage.py:40-149` | `verify_history_coverage(start, end, adj="qfq")` 整体 | **不要复用** | 窗口必填、逐票循环、`adj` 默认 qfq、状态二值、无水位；见 F1/F2/F3/F11 |
| `tools_coverage.py:12-24` | `_dates(conn, table, col, code, start, end, adj)` 读某实体在窗口内的日期集 | **不要复用** | 窗口语义写死（`BETWEEN`）+ f-string 拼表名；全局扫描应改为注册表驱动的单条 `GROUP BY` |
| `tools_coverage.py:27-37` | `_entry(...)` 生成 `{rows, first_date, last_date, missing_days, status}` | **需改造** | 形状可留；`missing_days` 把停牌/上市前/退市后一律算缺（F4），`status` 需升为三级 |
| `tools_coverage.py:58-60` | 逐票 `select_price_dates`，每股一次查询 | **不要复用** | 5000+ 股 = 5000+ 查询；集合化替代见 §12.2 |
| `tools_coverage.py:90-109` | lhb 逐日 basic/statistic 计数 + 台账最新状态（`ORDER BY trade_date, id DESC`） | **需改造** | 思路正确（行数 + 归因）；需换唯一排序（F6）、补 `lhb_stock_detail`、区分日/详情/yyb（F5） |
| `tools_coverage.py:115-118` | lhb 就绪 = `(basic==0 且 ledger∈{ok,empty})` 或 `(basic>0 且 statistic>0)` | **需改造** | 与 `tools_lhb` 的复用判据不一致：`ok+0 行` 这里算就绪、`_existing_lhb_inputs:126-128` 只认 `empty`（F5） |
| `tools_coverage.py:129-148` | `sections` 四段 + `gaps` 计数 + `status=ok/error` | **不要复用** | 段名与 `parity` 的 `faces` 不同（`stock_daily` vs `market_snapshot`），且"有 gap 即 error"与 `--strict` 语义冲突（F7） |

可留用的间接收益：`stock_daily` 段把**个股**放进覆盖核验，`parity` 的 `_FACE_TABLES`
没有个股面——这是两套面清单不一致的证据（F7），也说明"维度清单"必须统一到一处。

## 3. `parity_acceptance.py`：五核心面连续性 + 台账归因

| 位置 | 做什么 | 分类 | 说明 |
| --- | --- | --- | --- |
| `parity_acceptance.py:50-71` | `_FACE_TABLES`：4 个面（snapshot/boards/zt_pool/lhb）→ 表 + sections + `cadence` | **需改造** | 可作**注册表种子**，但只有 4 面、无个股/复权/指数/元数据，且缺 `code_col`/`required_fields`/`expected_source` |
| `parity_acceptance.py:74-94` | `_parse_faces` 五面白名单校验 | **不要复用** | 面集合是 `parity` 的对外参数契约；新 CLI 的维度集不同，复用会绑死两套清单 |
| `parity_acceptance.py:97-105` | `_table_date_counts`：窗口内逐日 `COUNT(*)`，一条 SQL | **可复用** | 最干净的"按日事实"原语；只需加 `COUNT(DISTINCT code)` 与 `FILTER` 空值列 |
| `parity_acceptance.py:108-121` | `_ledger_statuses`：`DISTINCT ON (trade_date)` + `finished_at DESC`（无 id tie-break） | **需改造** | 排序与 `tools_coverage:106` 不同（F6）；应抽成唯一的 `latest_ledger_rows()` |
| `parity_acceptance.py:124-163` | `_check_table_continuity`：逐日 `n>0` 即覆盖，无行再查台账；event 面 `ok/empty` 豁免 | **需改造** | **presence-only**（F3）：一行也算覆盖，无法回答"完整"；归因思路（行→覆盖 / 无行+台账确认→正常空 / 无确认→断档）应保留 |
| `parity_acceptance.py:132-139` | `daily` 面不允许台账 `empty` 豁免，`event` 面允许 | **可复用** | 这是正确的节律判据，应作为注册表字段而非循环内分支 |
| `parity_acceptance.py:146-150` | 全表 `MIN/MAX/COUNT(*)`（不带窗口） | **可复用** | 现成的"水位/总量"来源；注意是无界全表扫描（小表无碍，`lhb_stock_detail` 需评估） |
| `parity_acceptance.py:151-163` | 返回 `covered_days/empty_confirmed_days/gap_days/table_last_date/status` | **需改造** | `covered_days` 由窗口内所有有行日期计（`:156`），非交易日行会让它大于 `trading_days`（F3 附注） |
| `parity_acceptance.py:166-186` | `_check_indices_continuity`：`verify_index_coverage` + `market_indices` 逐日 + 台账 | **不要复用** | indices 被写成并列特殊结构（`:227-232` 也要专门分支）；且 `market_indices` 允许台账 `ok/empty` 豁免（`:175`），与同模块 daily 面规则（`:137`）相反（F8） |
| `parity_acceptance.py:189-242` | `check_history_continuity(faces, window_days=120, end=今天)` | **不要复用**（作入口） | 窗口必填 + 默认 `today_str()`（`:208`）、自然日回溯（`:211`）；无 as-of、无水位、无维度等级。其**归因语义**可搬到新策略层 |

## 4. `tools_index.py`：指数覆盖

| 位置 | 做什么 | 分类 | 说明 |
| --- | --- | --- | --- |
| `tools_index.py:174-247` | `verify_index_coverage(codes, start, end)`：逐指数 rows/first/last/sources/missing_days | **需改造** | 语义最贴近目标的一段，但三点不合：缺省窗口 31 自然日（`:37`、`:188-189`）；`missing_days` 全量返回，全历史窗口输出无界；无台账归因，分不清"未采集"与"上游无数据" |
| `tools_index.py:40-55` | `_codes_or_default` + `MAJOR_INDICES`（`tools_init`） | **可复用** | 指数维度的单一事实来源，`parity`/`tools_board` 也用它；新命令不得自定义第二份清单 |
| `tools_index.py:37` | `VERIFY_WINDOW_DAYS = 31` | **不要复用** | 31 自然日缺省会把"历史都很完整、最近一个月有空档"与"很久没采"混为一谈 |
| `tools_index.py:233-239` | `ok` / `partial_error` / `error` 聚合 | **需改造** | 第三套状态词表（F7）；应映射到统一词表，不直接透传 |

维度特定陷阱（指数）：期望行数不能写成 `len(MAJOR_INDICES)`。科创50 等指数上市/纳入时点
不同，早期交易日天然只有 6 行；用固定 7 会制造永久误报（`docs/knowledge-graph.md:124`
记录了科创50 自 2026-10-13 起缺口的处理方式）。替代：按"该指数首行 ≤ 当日"动态计入，
或直接用台账 `rows`（但注意 §5 的 `index_daily` 台账二义性）。

## 5. `tools_lhb.py` + `tools_board.py`：lhb 的三层完整性

| 位置 | 做什么 | 分类 | 说明 |
| --- | --- | --- | --- |
| `tools_lhb.py:81-92` | `_is_complete_lhb_day(entry)`：source 含 `exchange`/`ths`；`partial_error` 里只有 `lhb_yyb_capital:` / `lhb_yyb_most:` 前缀的失败不算失败 | **需改造**（规则可复用） | 这是 #137/#138 决策的唯一编码："yyb 富化失败不使当日失败"。但它是**台账 detail 字符串解析器**（`:86-88` 依赖 `partial_error=…; detail_rows=` 分隔），被 `tools_board.py:899-903` 的写格式绑死；应下沉为 `ledger.py` 的纯函数，两个调用方共用（§11.3） |
| `tools_lhb.py:66-70` | `_latest_lhb_success`：取该日 section 里第一条 `ok/empty` | **需改造** | 依赖 `select_collection_log` 的 `finished_at DESC, id DESC` + `LIMIT`（`ledger.py:130-131`）；和 `parity`/`coverage` 的"当日最新行"不是同一判据（F6） |
| `tools_lhb.py:112-149` | `_existing_lhb_inputs`：逐日 `select_board_rows`（`SELECT *`）× 2 表 + 详情父键比对 + `detail_status` 解析 | **不要复用**（形态）/ 规则需保留 | 每日 4 条查询；但其中三条规则必须继承：①`rows` 非空须 `status=ok`、无行须 `status=empty`（`:126-128`）；②详情完整 = 期望父键 ⊆ 实际父键 且 无 `detail_status=partial\|missing`（`:139-147`）；③北交所与未知代码不计详情缺失（`tools_board.py:352-364`） |
| `tools_board.py:840-848` | 写库时算 `detail_missing` / `detail_status=ok\|partial\|missing` | **可复用** | 与 `select_complete_lhb_detail_parent_keys` 同源，是详情完整性的权威产生点 |
| `tools_board.py:899-922` | 台账 detail 格式：`partial_error=…` / `detail_rows=…; detail_status=…; detail_missing=…; detail_source=…` | **需改造** | 格式本身是隐式契约，被两处字符串解析（`tools_lhb.py:86-88`、`:139-142`）；新命令若再解析一次就是第三个副本 |
| `repository.py:716-728` | `select_complete_lhb_detail_parent_keys`：按 `(stock_code, lhb_reason)` 要求 buy 与 sell 席位各 ≥1 | **可复用** | 详情完整性的精确定义，单条 SQL/日；全局扫描需窗口化集合版本 |
| `repository.py:770-781` | `BOARD_QUERY_TABLES`（10 张盘面表 → `(date_col, code_col)`） | **可复用** | 现成注册表种子；`lhb_yyb_capital/most` 明确是 `fetch_date`（`:779-780`），即"无历史日期语义"的机器可读标记（issue #124） |

`lhb_yyb_*` 的处理必须是**快照报告**（最新 `fetch_date` + 行数），不得进入"最新完整交易日"
计算——它们没有 trade_date 轴。

**表外写行为（不是复用对象，但必须继承）**：`tools_lhb.py:319-346` 中被 `backfill` 复用的
日子直接 `continue`、**不写新台账行**。因此台账存在性既不是"已采集"的充分条件，也不是
"缺失"的必要条件；详见 F5。

## 6. `price_reader.py`：复权 coverage 与价格读取接缝

| 位置 | 做什么 | 分类 | 说明 |
| --- | --- | --- | --- |
| `price_reader.py:153-287` | `NewPriceReader.query_daily`：raw+因子派生、行级 `price_basis`、顶层 `adjust{anchor,events,coverage}` | **不要复用**（作完备判据） | 窗口级、单票、每次 5 条 SQL；`coverage` 是"本次请求窗口"的等级，**不是维度水位**（CONTEXT.md:174-175 定义的是"生命周期内 raw 连续 + 事件证据完整 + 基线可信 + 无未决异常"） |
| `price_reader.py:247-251` | `expected = trading_days_between(...)`、`actual = 行内日期`，集合不等即 partial；`none` 口径另有无效 close 判定 | **需改造** | 精确集合相等 → 停牌/上市前/退市后一律 partial（F4）；对全市场扫描等于"几乎全 partial" |
| `price_reader.py:252-253` | `stale_state`（state 锚点 < 窗口最大日）、`unresolved_events`（窗口内任一台账 error） | **可复用**（谓词） | 这两条正是"未决异常"的可操作定义；但粒度要改：`unresolved` 现在是"一天 error → 整窗 partial" |
| `price_reader.py:254-259` | `state is None → unknown`；`rebuild_pending`/缺口/无效 close/陈旧/未决 → partial；否则 complete | **需改造** | 五条质量门都要保留，但需按日求值再取水位，而非整窗一个等级 |
| `price_reader.py:156-179` | 2023-01-01 新旧边界：之前 qfq 只读 legacy、跨界拒绝、窗口外 hfq/raw 不反推 | **可复用**（边界规则） | 全局 CLI 必须显式呈现这条边界，否则 2023 前的"缺失"会被误读 |
| `price_reader.py:166` | 2023 前 legacy qfq 分支硬编码 `coverage="partial"` | **需改造** | 旧窗口永久 partial 是设计（不与新序列拼接），但新命令若照搬会把"legacy 只读"显示成"数据不全" |
| `price_reader.py:26-33` | `LegacyPriceReader.select_dates`：按 `adj` 过滤 `stock_daily` | **可复用**（现状契约） | 这是 `verify_history_coverage` 依赖的语义；`tests/test_price_reader.py:45-66` 断言的正是它 |
| `price_reader.py:292-299` | `NewPriceReader.select_dates`：**只查 `stock_daily_raw`，`adj` 参数完全未使用** | **不要复用** | F1 的核心：契约写的是"返回该 adj 的日期集"，实现返回 raw 日期集 |
| `price_reader.py:35-38` | `select_latest_date`：`max(trade_date) FROM stock_daily`（无 adj 过滤） | **需改造** | 混合口径的最新日，不能当"最新完整交易日"；且经 `QSTOCK_PRICE_SOURCE` 换实现后语义又变（`:309-314`） |
| `price_reader.py:309-314` | `get_price_reader()`：`QSTOCK_PRICE_SOURCE` 缺省 `legacy`，未注册值显式失败 | **需改造** | 新命令必须**在输出里声明 basis**（哪个 reader / 哪张表），否则同一命令的答案随环境变量变化 |

## 7. `adjustment_audit.py` / `adjustment_acceptance.py`：复权结构与覆盖

| 位置 | 做什么 | 分类 | 说明 |
| --- | --- | --- | --- |
| `adjustment_audit.py:11-106` | `_validation(conn, start, end, event_set_complete)`：**单条 SQL** 出 raw 行/股数、无效 close、hfq 行、state 行、pending、`hfq_without_valid_raw`、`valid_raw_without_hfq`、`state_without_raw`、`no_event_bad` 分类不一致 | **可复用** | 全仓质量最高的复权维度探针；`event_set_complete` 门控（`:92-95`）是"上游不全就不报派生结论"的正确范式 |
| `adjustment_audit.py:36-52` | `no_event_bad`：无事件票必须是 B0=B=F=1，否则计数 | **可复用** | 无事件票的强不变量，可移植进新 SQL |
| `adjustment_audit.py:266-285` | `audit_adjustment_migration`：校验 + 每股四表哈希 + 分层抽样 | **不要复用** | 迁移证据生成器（哈希/抽样/固定种子），与就绪无关 |
| `adjustment_acceptance.py:35-40` | `_calendar_sql`：把 `trading_days_between` 展开成 `VALUES` CTE，含前/后交易日 | **可复用** | 让"期望交易日"能与库表做集合连接，且不依赖日历表；窗口大时 VALUES 会变长，需设上限 |
| `adjustment_acceptance.py:726-879` | `_check_structure_coverage`：主键重复、非法因子、state 形状、事件对齐、raw 日行数、因子日覆盖 | **不要复用** | 四条耦合使它不可直接搬：①读迁移产物 `migration_dir/raw_days.csv`（`:816-826`）；②断言 `coverage == "partial"` 是**测试期望**不是语义规则（`:827-836`）；③因子日覆盖靠 `detail LIKE 'adj-backfill event start~end%'`（`:785`、`:800`），`morning` 采的因子日不计入（F9）；④逐票调 `query_daily`（`:830`） |
| `adjustment_acceptance.py:861-865` | 用迁移元数据 `trading_days` 对比 `raw_days_covered`/`factor_days_covered` | **不要复用** | 期望值来自迁移工件而非库内事实；新命令的期望必须可自证（§12.3） |

可移植的不变量（写进新 SQL，不要调用旧函数）：主键重复 = 0、因子/价格 `factor_cum > 0`、
state 形状合法、事件日必有 raw 与 hfq、非 pending 的有效 raw 必有 hfq。

## 8. `tools_morning.py`：raw/因子/指数三部分的台账与水位思想

| 位置 | 做什么 | 分类 | 说明 |
| --- | --- | --- | --- |
| `tools_morning.py:64-71` | `stock_daily` part 落台账：`rows=n`、`detail="morning_batch"` | **可复用** | **期望值来源**：`collection_log(section='stock_daily', trade_date=D, rows=N)` 是该次采集实际写入的行数，通常等于上游当日返回行数 |
| `tools_morning.py:172-183` | `_raw_gap_codes`：只看当日有行的票，其上一行到当日之间是否跳日 | **需改造** | 语义正确（停牌票不在当日行里，天然不误报），但每票 1 条 SQL；需集合化为窗口自连接 |
| `tools_morning.py:263-279` | `error_kind` 阶梯：`factor_mismatch` / `pctchg_derivation_failed` / `raw_gap` / `rebuild_pending` / `invalid_close` | **可复用** | 现成的复权维度"未决原因"词表；新命令读台账 `error_kind` 即可，不必重算 |
| `tools_morning.py:292-329` | `_index_daily_part`：`latest >= target` 即跳过；否则从尾行次日起补 | **需改造** | "尾行 vs 目标日"就是水位思想，但它是写路径；且它落的 `index_daily` 台账 `rows` 是**多指数合计**（`:322-328`），而 `index_backfill` 每指数一行、`trade_date=end_n`（`tools_index.py:131-142`）——同 section 两种行形状（F5 附注） |
| `tools_morning.py:332-368` | `sw_board_valuation` part：`ok/empty` + `rows` | **可复用** | 该维度的标准台账形态，可直接作期望值来源 |

`stock_adjust_factor` 台账的 `detail` 至少有两种前缀（`morning_batch:...` 与
`adj-backfill event ...`），任何按 detail 前缀归因的判据都会漏掉另一种（F9）。

## 9. `repository.py` / `ledger.py` / `output.py` / `dates.py` / `gaps.py`：原子能力

| 位置 | 做什么 | 分类 | 说明 |
| --- | --- | --- | --- |
| `repository.py:324-328` | `select_stock_codes`：`stock_list` 全代码 | **可复用**（带警告） | 是"当前universe"不是 as-of universe；用它当历史日的期望集合会把退市/新股判成缺口 |
| `repository.py:826-830` | `select_latest_board_date(conn, table, col)` 泛型 `max(col)` | **可复用** | 新 CLI 的"水位"原子；但需与 `:284-290`、`:1157-1161` 三处近似实现合一（后者经 `price_reader` 变得环境相关）。表清单见 §5 的 `BOARD_QUERY_TABLES` |
| `repository.py:833-853` | `select_board_rows`：`SELECT *` + 按日/代码过滤 | **不要复用**（扫描用） | 单日定点可取；全维扫描会被 `SELECT *` 拖垮 |
| `ledger.py:23` | `LEDGER_STATUSES = ("ok","empty","error","planned_absent")` | **可复用** | 四态是权威词表；`planned_absent` 必须作为独立输出态，不能并入"缺失" |
| `ledger.py:116-139` | `select_collection_log`：按日期/section/状态过滤，`finished_at DESC, id DESC`，`LIMIT` | **需改造** | 用于下钻很好；不能用于"逐日最新状态"（LIMIT + 无 `DISTINCT ON`） |
| `output.py:9-19` | `aggregate_batch_status(total, failed)` → `ok`/`partial_error`/`error` | **需改造** | 注释明写"状态词汇不得各自为政"（`:13`），应作为**维度集合**的汇总词汇；但逐日三维等级还缺 `unknown`（F10） |
| `dates.py:116-134` | `trading_day_window(N, end, now=...)`：含"最近已收盘交易日"判定（当日盘中则回退前一交易日） | **可复用** | 现成的 as-of 解析；`data-status` 的默认 `--as-of` 应直接用它 |
| `dates.py:137-145` | `trading_days_between(start, end)` | **可复用** | 期望交易日的唯一来源 |
| `dates.py:18-20` | `_MARKET_CLOSURES` 只到 2026，"2027 起须按公告补齐" | **可复用**（带硬警告） | 任何跨 2027 的水位判定都不可靠；输出必须带 `calendar_reliable_until: 2026` |
| `gaps.py:10-21` | `head_tail_gaps`：中段缺口视为停牌，不补 | **不要复用**（语义相反） | 这是**采集**策略（`tools_daily.py:54` 调用）；与"完备核验"的中段缺口判据正好相反，两者绝不能混进同一字段 |

## 10. 新 CLI 会继承的误报与语义冲突

### F1 `adj="qfq"` 默认在切读后失效（最高危）

- 证据：`tools_coverage.py:40` 默认 `adj="qfq"`；`scheduler.py:710` 调
  `verify_history_coverage(start, end)` **从不传 adj**；`price_reader.py:292-299` 的
  `NewPriceReader.select_dates` 不使用 `adj`，且**没有** `query_daily` 那条 2023 边界守卫
  （`price_reader.py:156-179`）；`price_reader.py:309-314` 由环境变量选实现。
- 后果：`QSTOCK_PRICE_SOURCE=new` 时，"个股 qfq 覆盖"实际是"raw 有行"——**假阳性**
  （raw 存在 ≠ qfq 可派生：factor/state 可能缺失）；同时 2023 前 raw 缺席会变成**假阴性**
  全市场 gap。两种错误都静默。
- 处置：新命令不复用该函数；价格维度按「raw 行 / hfq 行 / state 行 / 事件行」四事实分别报，
  并在输出声明 `basis`。

### F2 所有既有入口都是"窗口"语义

- 证据：`coverage` 必须 `--start/--end` 或 `--trading-days`（`scheduler.py:697-706`）；
  `parity` 用自然日 `window_days=120`（`parity_acceptance.py:38`、`:211`）；
  `index --verify` 缺省 31 自然日（`tools_index.py:37`、`:188-189`）。
- 后果：直接复用任何一个，`data-status` 就必须接受窗口，无法回答"全局水位"。
- 处置：窗口变成**可选**（`--lookback N`，缺省有界回溯），as-of 由
  `dates.trading_day_window(1, end, now)` 给出（§12.1）。

### F3 "有行即可"与"缺一即 gap"两套相反判据

- 证据：`parity_acceptance.py:133-135`（`n > 0` 即覆盖，3 行也算）；`tools_coverage.py:28`
  （缺任一自然交易日即 gap）。
- 后果：前者高估（部分失败日显示完整），后者低估（停牌日显示缺口）；两者都不能直接产出
  "最新完整交易日"。
- 附注：`covered_days` 由窗口内有行日期计数（`parity_acceptance.py:156`），非交易日行会让它
  大于 `trading_days`。
- 处置：改成"期望 vs 实际"实数比较（§12.3）。

### F4 精确集合相等把停牌/上市/退市/北交所判成缺口

- 证据：`price_reader.py:247-251`；`tools_coverage.py:28`；`tools_lhb.py` 体系则正确排除
  北交所详情（`tools_board.py:352-364`）。
- 后果：全市场扫描下，任何非全生命周期股票都会被判 partial；这正是"多少只算完整"无法用
  固定阈值表达的原因。
- 处置：期望值来自台账/当日行数而非日历全集；无法取得期望时输出 `unknown`，不输出 `partial`。

### F5 lhb 三个"完整"互不等价，且台账存在性不可靠

- 证据：日完整 `tools_lhb.py:81-92`；详情完整 `repository.py:716-728` 与
  `tools_board.py:840-848`；yyb 快照按 `fetch_date`（`repository.py:779-780`）；
  复用日不写台账（`tools_lhb.py:319-346`）。
- 后果：把三者 AND 起来会低估（yyb 失败不该使当日失败；北交所无详情）；把台账当必要证据
  会误报（复用日无新行）。
- 附注：`index_daily` 台账有两种行形状（`tools_morning.py:322-328` 合计行 vs
  `tools_index.py:131-142` 每指数行），`rows` 不能当期望计数。
- 处置：三维分别报（日 / 详情 / yyb 快照），不合并成一个"lhb 完整"。

### F6 台账"当日最新一行"四套排序

- 证据：`id DESC`（`tools_coverage.py:106`）；`finished_at DESC` 无 tie-break
  （`parity_acceptance.py:117`）；`finished_at DESC, id DESC`（`ledger.py:130`、
  `price_reader.py:231`）。
- 后果：同一天有 `ok` 与后续 `error` 时，两个命令可能给出不同归因。
- 处置：唯一 `latest_ledger_rows()`，`ORDER BY trade_date, section, finished_at DESC, id DESC`；
  三处旧调用点不改也可以，但新命令只认这一处。

### F7 状态词表四套 + 面清单两套

- 证据：`ok/error`（`tools_coverage.py:142`）、`ok/gap`（`parity_acceptance.py:162`、
  `:236`）、`ok/partial_error/error`（`tools_index.py:233-239`）、台账四态
  （`ledger.py:23`）、`complete/partial/unknown`（`price_reader.py:254-259`）。
  面清单：`_FACE_TABLES` 4 面 vs `tools_coverage` 的 4 段（含 `stock_daily`，无
  `market_snapshot`）。
- 后果：新命令任何一种 `status` 字段都会与既有消费者的直觉冲突（`coverage` 的消费者把
  非 `ok` 当失败，`scheduler.py:712`）。
- 处置：输出用自洽的三级（+可选 `unknown`）+ 与 `aggregate_batch_status` 的显式映射；
  不复用旧字段名 `status` 承载两个含义（建议 `level` 与 `status` 分工）。

### F8 indices 面双标

- 证据：`parity_acceptance.py:175` 允许 `market_indices` 无行时由台账 `ok/empty` 豁免，
  而 `_FACE_TABLES` 的 daily 面在 `:137` 明确不允许。
- 后果：同为"每日必有"的维度，两套判据。
- 处置：节律策略进注册表；`indices` 不再特殊分支。

### F9 因子日归因靠 detail 前缀

- 证据：`adjustment_acceptance.py:785`、`:800`（`detail LIKE 'adj-backfill event S~E%'`）。
- 后果：`morning` 采的因子日不计入 → 因子覆盖被低估（假阴性）。
- 处置：归因只看 `section + trade_date + status`，`detail` 前缀只作下钻线索。

### F10 `unknown` 无归宿

- 证据：`price_reader.py:254-259` 有 `unknown`；`aggregate_batch_status` 只有三态；
  #174 规划写的是"三级判定"。
- 后果：若不做决定，`unknown` 会被静默压成 `partial` 或 `missing`，把"没证据"说成"有缺口"。
- 处置：这是 #177 的决策输入，本文只标注不可默选（§12）。

### F11 per-stock / per-day 循环

- 证据：`tools_coverage.py:58-59`；`tools_lhb.py:123-137`；`adjustment_acceptance.py:830-836`；
  `tools_morning.py:176-182`。
- 后果：全市场 5000+ 票 × 每票 ≥1 查询；全局扫描不可接受。
- 处置：每维一条 `GROUP BY trade_date` + 有界回溯（§12.2）。

### F12 `lhb_yyb_*` 没有交易日轴

- 证据：`repository.py:779-780` 的 `fetch_date`；#124 决策。
- 后果：任何把 yyb 纳入"最新完整交易日"的计算都无意义。
- 处置：快照式报告（最新 `fetch_date`、行数、来源），单独成组。

## 11. 模块边界建议

### 11.1 三个方案

| 方案 | 改动面 | 优点 | 风险 | 结论 |
| --- | --- | --- | --- | --- |
| A. 扩展 `tools_coverage.py` | 同模块加全局模式/三维等级/水位 | 复用现成 `coverage` 入口与测试 | `coverage` 的契约是"有缺口 exit 1"（`scheduler.py:712`），与"不完备不算失败"直接冲突；一个函数两种失败语义；`stock_daily` 段与 `adj` 默认会把 F1 带进来 | **否** |
| B. 新建 `tools_data_status.py`（自洽核心） | 新文件 + `scheduler.py` 薄命令 + 文档 | 语义独立、可整体单测、不污染既有契约 | 注册表/策略/装配混在一个文件，后续复用时易被整块 import | **是（推荐）** |
| C. 抽通用 readiness core + 薄命令 | 新 core 模块 + 两处改造 | 长期最干净 | 目前只有一个消费者；`parity` 的 gap 策略是**有意**与"完备"不同的（presence-only），合并会逼出"一套策略打两种语义"；抽象过早 | **暂不** |

采纳 **B**，并为 C 留一个**单函数**的下沉口（见 11.3）。理由一句话：现在缺的是**一个维度的
事实模型**，不是**一套通用框架**；把注册表做成数据，将来第二次复用时再抽 core 不需要改调用方。

### 11.2 依赖方向

```
scheduler.py  cmd_data_status / data-status parser（薄，只做 argv → 结果 → 退出码）
      │
      ▼
tools_data_status.py      ← 唯一知道「维度 / 节律 / 等级 / 水位」的模块
      │  只向下 import
      ├── repository.py   BOARD_QUERY_TABLES, select_stock_codes,
      │                   select_complete_lhb_detail_parent_keys, 泛型 latest_date
      ├── ledger.py       LEDGER_STATUSES, is_complete_collection（从 tools_lhb 下沉的纯判据）
      ├── dates.py        trading_day_window, trading_days_between
      ├── output.py       aggregate_batch_status, error
      └── db.py           connect
   ✗ 不得 import tools_coverage / parity_acceptance / tools_lhb / tools_morning / price_reader
```

禁止向上的理由有先例：`docs/knowledge-graph.md:112` 记录过
`tools_init → tools_daily.ensure_coverage` 的成环风险，并因此把索引查询留在 `tools_init`。
`tools_data_status → tools_lhb` 会拉进 `adapters` / `fetch_chain` / `tools_board` 的整条
写路径，正是同型问题。

代价：`_is_complete_lhb_day` 的规则不能直接调用。处理办法是**下沉**而不是复制：

### 11.3 唯一需要下沉的规则

`tools_lhb._is_complete_lhb_day`（`:81-92`）的纯判据移入 `ledger.py`，例如

```python
def is_complete_collection(entry: dict, *, ignorable_error_prefixes: tuple[str, ...] = ()) -> bool
```

- 输入只有台账行字段（`source` / `detail` / `status`），无 I/O、无 import 依赖；
- `tools_lhb` 保留同名薄包装（行为不变，现有测试见 `tests/test_lhb_tools.py:396`、
  `:410`、`:424` 一带）；
- `tools_data_status` 直接调它，不 import `tools_lhb`。

这一步是本盘点里**唯一建议动既有业务代码的地方**，且是纯搬迁 + 包装；其余全部在新模块内完成。

### 11.4 测试接缝

| 层 | 怎么测 | 复用现有设施 |
| --- | --- | --- |
| 判定策略（纯函数） | `classify(facts, ledger, policy) -> level` 输入普通 dict，无 PG、无 env | 新单测，无 PG |
| 注册表 + 维度 SQL | `pg_test`（真实 PG，不可达 skip），fixture 造 N 天 × M 票 + 台账行 | `tests/conftest.py:113-121` |
| CLI 契约 | `scheduler.main([...])` + `capsys` + `PG_DSN` monkeypatch | `tests/test_scheduler_parity.py:31-35` 的 `_run` |
| 零上游保证 | 结构性：新模块不 import `adapters`/`fetch_chain`；加一条 import 断言测试 | 新断言 |

不引入 fake 适配器（模块不触网），也不需要注册 `price_reader` 实现（不 import 它）。

## 12. 不要复制窗口逻辑：具体替代

### 12.1 as-of 解析（一行）

```python
from .dates import trading_day_window
as_of, _ = trading_day_window(1, end_arg)   # 已处理「当日盘中回退前一交易日」
```
`dates.py:116-134` 已实现"最近已收盘交易日"语义，不要在命令里再写一遍。

### 12.2 扫描形状：有界回溯，不扫全史

每维一条 SQL，取"最近 K 个交易日"的逐日事实，再向下探测到第一个完整日：

```sql
WITH cand AS (
    SELECT DISTINCT trade_date FROM <table>
    WHERE trade_date <= %(as_of)s ORDER BY trade_date DESC LIMIT %(lookback)s
)
SELECT t.trade_date, COUNT(*) AS rows,
       COUNT(DISTINCT t.<code_col>) AS codes,
       COUNT(*) FILTER (WHERE t.<required_col> IS NULL) AS null_required
FROM <table> t JOIN cand c USING (trade_date)
GROUP BY t.trade_date ORDER BY t.trade_date DESC
```

- 表名/列名只来自内部注册表（同 `parity_acceptance.py:47-49` 的既有约定），不接外部输入；
- `lookback` 缺省小（如 10 交易日），`--lookback` 可放大；**不提供"扫全史"开关**；
- 需要跨年水位时，用 `repository.select_latest_board_date` 类的 `max(col)` 单点查询，
  不用全表 `GROUP BY`。

### 12.3 期望值来源优先级（替代"多少只算完整"的阈值）

1. **台账行数**：`collection_log(section=<face>, trade_date=D, status∈{ok,empty})` 的 `rows`
   ——该次采集写入并落账的行数，通常等于上游当日返回行数（`tools_morning.py:64-71`、
   `tools_index.py:131-142`、`tools_board.py:912-922`）。比较 `actual_rows` 与 `rows`。
2. **同维上一完整日行数**：仅当台账缺失时，用于判断"量级塌陷"（如 < 上一完整日 90%），
   并标记 `basis="relative"`。
3. **都没有 → `unknown`**：不猜、不判 partial。

这样"全市场多少只算完整"从阈值问题变成**对账**问题；阈值只在第 2 级出现，且显式标注为
相对判据。注意 `index_daily` 台账 `rows` 有二义（F5 附注），指数维度只能走第 2/3 级或
"逐指数尾行 vs as-of"（`tools_morning.py:298-304` 的思路）。

### 12.4 台账归因唯一入口

```python
def latest_ledger_rows(conn, sections, start, end) -> dict[tuple[str, str], dict]:
    # SELECT DISTINCT ON (section, trade_date) ...
    # ORDER BY section, trade_date, finished_at DESC, id DESC
```
四处旧排序（F6）不动，新命令只调这一处。

### 12.5 等级与汇总

- 逐维逐日：`complete` / `partial` / `missing`（+ 待定的 `unknown`，见 F10）；
- 每个维度：`latest_complete_day = max(D where level == complete)`；
- 维度集合汇总：映射到 `output.aggregate_batch_status`
  （`complete→ok`、`partial→partial_error`、`missing→error`），避免第五套词表；
- 退出码：缺省 0（不完备不是失败），`--strict` 才在任一级 `missing|partial` 时 exit 1；
  `planned_absent` 一律不算失败（`CONTEXT.md:82`）；
- 输出必须带：`as_of`、`basis`（reader/表）、`calendar_reliable_until`、
  `dimensions[].latest_date` 与 `latest_complete_day` 分开。

## 13. 交给下游票的待决项

| # | 待决 | 归属 | 本文提供的证据 | 为什么不能在此决定 |
| --- | --- | --- | --- | --- |
| 1 | 三级还是四级（`unknown` 是否独立） | #177 完备判定模型 | F10：`price_reader` 已有 `unknown`，但 `aggregate_batch_status` 只有三态 | 属判定模型本身，盘点只证明"现状不一致" |
| 2 | 各维"完整"的期望值取台账还是相对阈值 | #178 / #177 | §12.3；`collection_log.rows` 可用；`index_daily.rows` 有二义 | 需要库内实际数据验证台账覆盖率（#181） |
| 3 | 复权维度覆盖率门槛（多少票 complete 算水位） | #178 / #179 | §6：`price_reader` 是窗口级；CONTEXT.md:174 是生命周期级 | 两处定义不同，需先定"维度完备"是逐票聚合还是逐日聚合 |
| 4 | 维度清单与分组（23 表 → 哪些算一个维度） | #176 | §1.1 的节律五型（每日/事件/状态快照/派生/元数据）可直接作输入 | 分组是产品判断，且 `lhb_yyb_*` 已确认单独成组 |
| 5 | 命令定位、默认 as-of、目标库 | #175 | F2、§12.1 | 需与 `coverage` 的既有消费者对齐 |
| 6 | 输出契约与退出码（`--strict`） | #180 | F7、§12.5 | 属输出原型 |
| 7 | 2027 交易日历 | #174 未决项 | `dates.py:18-20` | 需外部官方公告 |

## 14. 证据与复现

### 14.1 只读命令

```bash
# 基线
git log -1 --format='%H %s'
# 接缝定位（本文所有 path:line 均来自这类检索）
grep -n "def \|_FACE_TABLES\|_MARKET_CLOSURES" src/qstock_mcp/{tools_coverage,parity_acceptance,tools_index,tools_lhb,price_reader,adjustment_audit,tools_morning,repository,ledger,dates,output,gaps}.py
```

未连库、未跑测试、未触网；工作区只有本文件一个新增文件。

### 14.2 行号漂移声明

`docs/research/2026-10-31-price-consumers-inventory.md` 已被标注作废（该文件 `:3`），且其
`tools_coverage.py:39`/`:58` 与当前 HEAD 的 `:40`/`:59` 已漂移。本文**不引用该作废文档的结论**，
F1 的一手证据是当前代码（`price_reader.py:292-299`、`scheduler.py:710`）与
`tests/test_price_reader.py:45-66` 的既有断言。该文档"qfq 默认会在 raw 化后误报"这一
**方向性判断**与当前代码一致，故作为交叉印证保留一条引用，不作为依据。

### 14.3 未验证项（需 #181 或实现期补）

- 各维实际行数/水位/台账覆盖率（本文只有代码语义）。
- `market_boards.board_code` 可空（`sql/004_market_boards.sql:8`），而覆盖核验按 `board_code`
  匹配（`tools_coverage.py:81`）；若存量行 `board_code` 为空则会静默漏计——需库内抽查。
- `stock_daily_raw` / `stock_daily_hfq` / `lhb_stock_detail` 的全表 `MIN/MAX/COUNT`
  在真实体量下的耗时（`parity_acceptance.py:148` 的形态）。
