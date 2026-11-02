---
name: limit-up-analysis
description: 涨停/连板分析编排（收盘后），可单独调用、也是每日复盘的涨停结构子模块。当用户请求涨停分析/连板梯队/晋级率/涨停归因/连板龙头点评时使用；经 MCP 查询 zt_pool/strong_stocks/market_snapshot/market_boards 做就绪检查与归因，晋级率与连板梯队经 indicator-tools 的 promote 指标管道计算（不手算），龙头点评由 agent 结合资讯生成，结论经 save_conclusion 落库（limit_up.close）。
---

# 涨停/连板分析

收盘后基于当日涨停池（zt_pool）与强势股池（strong_stocks）做的市场级涨停结构分析：连板梯队与晋级率、涨停归因三维度、连板龙头点评（术语见 `CONTEXT.md`）。本 skill 为编排 skill，**轻脚本、重编排**：编排逻辑在本文件，agent 自身即 LLM，龙头点评与归因汇总结论由 agent 手写生成，不调外部 LLM API。数据一律来自 MCP 工具（盘面/快照经 `query_board_data`、`query_snapshot`、`fetch_board_snapshot`、`fetch_market_snapshot`，资讯经 `mx_search`、`query_news`），不直接访问数据库；晋级率与连板梯队**必须经 indicator-tools 的 promote 指标管道计算**，agent 不得手算这些数字。结论经 `save_conclusion` 落库（通用结论表，见 ADR 0003 `docs/adr/0003-generic-conclusions-table.md`）。

本技能以复盘为主、可单独调用；每日复盘（daily-review skill）调用本技能并把其摘要编进复盘报告（引用键见"输出形态"节）。

## 触发与模式

- 由用户手动触发或由 daily-review 编排调用，无常驻调度。
- 模式仅收盘一种：conclusion_type 固定 `limit_up.close`；subject 固定 `subject_type="market"`、`subject_code="_market"`。
- 同日重复跑为 upsert（业务键含 conclusion_type，返回 `outcome: "updated"`），不产生重复结论行。

## 编排流程（按顺序执行）

1. **确定交易日**：`trade_date` 格式 `yyyymmdd`，缺省取当日，用户可指定。建议先用 `query_board_data("zt_pool")`（不带日期）确认库内最新交易日，避免拿非交易日空查。
2. **数据就绪检查**：对当日逐表查行数——
   - 核心：`zt_pool` 经 `query_board_data("zt_pool", trade_date)`（`count=0` 即核心缺失）。zt_pool 由同花顺涨停揭秘承载（issue #120）：行内 `sealed_amount`（封单额，万元）/`reason_type`（涨停原因题材）/`limit_up_type`（板型）可直接用于龙头点评与归因；**THS 无所属行业字段，`industry` 列恒为 null**。
   - 关键：`strong_stocks` 经 `query_board_data("strong_stocks", trade_date)`，`market_snapshot` 经 `query_snapshot(trade_date)`，`market_boards` 经 `query_board_data("market_boards", trade_date)`（分别支撑入选理由聚类、大小票/业绩粗分、行业背景参考——现仅申万行业口径，概念维度已退役 issue #119）。strong_stocks 现为**库内自算**（60 日新高 ∪ 近 10 日多次涨停，`reason` 为规则文本）：当日为 empty 且采集记录为 no_data_for_date 时说明自算输入（stock_daily 当日行）未就绪——先补日线（`fetch_daily`/`qstock-scheduler daily`）再重跑 `limitpool`，不当作上游故障。
   - 缺失先补抓后重查：`fetch_board_snapshot(trade_date, sections=<缺失 section 逗号分隔>)`（sections 取值含 `zt_pool,strong_stocks`；`market_boards` 属 `boards`）、`fetch_market_snapshot(trade_date)`；重查仍缺则列入 `data_gaps`，对应归因维度降级。
   - **核心数据 `zt_pool` 全缺且补抓失败时中止分析**，向用户明确报告缺失范围与失败原因，不得基于残缺数据生成涨停结论。注意 zt_pool 补抓受 THS 低频纪律约束：当日已采集即跳过；封禁（`error_kind=upstream_unreachable`）当日勿重试。
3. **昨日 zt_pool 同查**（晋级率输入）：取前一交易日的 `zt_pool`。前一交易日以自然日回溯逐个探测（周末/节假日自然跳过，最多回溯 5 个自然日，命中 `count>0` 即停）。昨日数据缺失（含补抓失败）时：今日梯队照常分析，晋级率维度降级并在 `data_gaps` 标注"无昨日涨停池，晋级率不可算"。
4. **组装 promote 输入，经管道算晋级率与梯队**：
   - 口径：**晋级率分母只含昨日连板股**——昨日 `zt_pool` 行筛 `pool_type="zt"` 且 `consecutive_boards >= 2`（首板稀释分母，不进昨日输入）；**今日 boards 取当日全部涨停股**（`pool_type="zt"`，含首板——是否晋级只看今日仍涨停，与今日几板无关）。组装：
     ```json
     {"status":"ok",
      "yesterday":{"trade_date":"<昨日>","boards":[{"stock_code":"600001","limit_count":2}]},
      "today":{"trade_date":"<当日>","boards":[{"stock_code":"600001","limit_count":3}]}}
     ```
     `limit_count` 取 `consecutive_boards` 字段；该字段为 null/缺失时按 1（首板）处理，不得猜测连板数。promote 输出的 `promoted`/`failed` 即昨日连板股中今日仍涨停（晋级）/未涨停（断板）的代码列表。
   - 调用形态（与 sepa 调 indicator-tools 同法，输入经临时文件或 heredoc 传 stdin）：
     ```bash
     <promote 输入 JSON> | python skills/indicator-tools/scripts/indicators.py --indicator promote
     ```
   - 退出码语义：0 = ok / insufficient_data（昨日无连板股时晋级率无意义，如实标注，不算错误）；1 = 输入错误（修正输入后重试）；2 = 参数错误。
   - 采信输出：`promote_rate`（昨日连板股中今日仍涨停家数 / 昨日连板家数 × 100，%）、`promoted`/`failed`（晋级/断板代码列表）、`ladder`（今日各连板高度家数）、`ladder_detail`（各高度的代码分布）。**这些数字只取管道输出，agent 不得自行计算或改写**。
5. **涨停归因三维度**（口径见下节，均由 agent 基于查询结果聚类/统计）。
6. **连板龙头逐一分析**：取 `ladder_detail` 中最高板个股（`ladder` 最大键对应的代码列表），逐只列出代码/名称/连板数/涨停时间（`limit_up_time`）/**封单额（`sealed_amount`，万元）**/板型（`limit_up_type`）/题材（`reason_type` + `strong_stocks.reason`；概念维度已退役（issue #119）不再有"命中概念板块"路，`industry` 列 THS 无行业字段恒 null，题材归因以 `reason_type` 为主），结合资讯写点评（资讯取法与引用约定见下节）。一般涨停股（非最高板）不逐个分析。
7. **结论落库**：`save_conclusion(subject_type="market", subject_code="_market", trade_date=<yyyymmdd>, conclusion_type="limit_up.close", payload=<结论 JSON>)`。**返回 `status != "ok"` 即中止**，向用户报告失败原因——不产生"分析了但没存下"的假象，落库成功才算分析完成。
8. **核验（可选）**：`query_conclusions(subject_type="market", trade_date=<yyyymmdd>, conclusion_type="limit_up.close")` 确认落库行存在且仅一行；重跑时步骤 7 返回 `outcome: "updated"`。

## 涨停归因三维度

1. **板块聚类**：两路信息交叉——`zt_pool.reason_type`（涨停原因题材聚类，THS 源承载，主路）；`strong_stocks.reason`（入选理由聚类，取强势股池与涨停池的代码交集后按 reason 关键词归类；自算口径下为"60日新高/多次涨停"规则文本，聚类价值有限，如实标注）。可选增强：涨停股 × 申万行业归属交叉（经成分字典 `sw_industry_class` point-in-time 换口径，行业级背景参考，非概念级）。**概念板块交叉命中已随概念维度退役**（issue #119，THS 概念成分准入闸未过、无成分字典自算来源），不再作为一路；东财链路时代的 `zt_pool.industry` 行业聚类在 THS 源下同样不可用（该列恒 null）。互相印证得出当日涨停主线板块/题材。
2. **大小票划分**：以 `stock_code` + `trade_date` 关联当日 `market_snapshot`（快照约 5000+ 行，覆盖全部涨停股），取 `market_cap`（总市值）/ `float_cap`（流通市值）。按总市值分档：小票 <100 亿、中票 100–500 亿、大票 ≥500 亿（阈值为本 skill 约定口径，报告中注明）。快照中查不到的个股标注"快照未覆盖"，不猜测市值。
3. **业绩/题材粗分（启发式）**：取 `market_snapshot.pe_ratio`——PE 为负（亏损）或畸高（如 >100）粗分为题材/亏损股，其余粗分为业绩股。**报告中必须注明这是基于单日 PE 的启发式粗分**，不构成基本面判断；PE 缺失的个股标注"PE 缺失"单列，不计入任一类。

## 资讯佐证（龙头点评）

- 先读库：`query_news(subject_type="stock", subject_code=<6 位代码>, start=<近 7 日起>, limit=10)`（按资讯发布时间倒序；`count:0` 属正常，非错误）。
- 库内不足再搜（可选降级）：`mx_search(query=<问句>, subject_type="stock", subject_code=<代码>)` 搜近期资讯并落库后重读。mx-search 每日配额有限（默认 20 次/日，响应回显 `quota:{skill,used,limit}`）；本地配额触顶（`status:"error"`）、上游日限（code=113）或 `MX_APIKEY`/`PG_DSN` 未配置时跳过，资讯缺失记入 `data_gaps`，不阻塞分析。
- 引用约定（与 daily-review 一致）：每条引用注明**标题 + 发布时间 + 来源**；`source` 可空，缺失时标"来源未标注"；`url` 可空（有链接附链接，无链接以"标题 + 来源 + 时间"为标识），**不虚构链接**。资讯只作观点佐证，不构成数字来源。

## 输出形态

本技能**不写独立 markdown 报告文件**。产出两件：

1. **落库结论**：`limit_up.close` 结论行（见下节 payload 契约）。
2. **对话内分析文本**：梯队分布、晋级率、归因三维汇总、龙头逐一点评、数据缺口，直接在对话中呈现。

daily-review 调用本技能时，经 `query_conclusions(conclusion_type="limit_up.close")` 读结论并把 `summary` 与关键数字编进复盘报告的涨停/强势股结构章节。

### 结论 payload 契约（limit_up.close）

payload 结构由本 skill 约定（server 不校验，见 ADR 0003），字段：

- `summary`（一句话摘要，供 daily-review 直接引用）
- `ladder` / `ladder_detail`（今日连板高度家数与代码分布，原样取自 promote 输出）
- `promote_rate`（晋级率 %，原样取自 promote 输出；昨日数据缺失时为 null）
- `promoted` / `failed`（晋级/断板代码列表，原样取自 promote 输出）
- `attribution`（归因三维汇总：`sectors` 主线板块/题材、`size_split` 大小票家数分布、`earnings_theme_split` 业绩/题材粗分家数，各自注明口径）
- `leaders`（龙头列表：每只含 `stock_code`/`stock_name`/`limit_count`（连板数）/`limit_up_time`/`sealed_amount`（封单额，万元，可 null）/`limit_up_type`（板型，可 null）/`theme`/`comment`（点评）/引用资讯）
- `data_gaps`（缺失维度列表，无缺失为空数组）

## 数据缺失红线

- **数据缺失不编造**：某维度数据缺失时，在结论 `data_gaps` 与对话文本中标注，该维度降级为定性描述或直接缺省，不得编造家数、晋级率、市值或点评依据。
- **晋级率/梯队只认管道输出**：`promote_rate`、`ladder`、`ladder_detail`、`promoted`、`failed` 一律取自 indicator-tools promote 指标输出，agent 不得手算；管道返回 `insufficient_data` 时如实标注，不得用其他口径凑数。
- **停牌表述**：连板股历史序列中停牌交易日的占位行（`query_daily` 行带 `suspended=true`/`suspended_basis`，词汇见 CONTEXT.md"停牌占位行"）由 indicator-tools 管道入口自动剔除（剔除明细见响应 `excluded_suspended`），无需手工预处理；文本表述统一为"停牌"（不写"无数据"）。
- **核心数据全缺即中止**：`zt_pool` 当日数据全缺且补抓失败时，中止分析并向用户报告缺失范围与失败原因；关键数据（strong_stocks / market_snapshot / market_boards / 昨日 zt_pool）部分缺失时标注缺失维度后继续。
- **落库失败即中止**：`save_conclusion` 失败时向用户报告失败，不算分析完成。
