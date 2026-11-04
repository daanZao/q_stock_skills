# Skill 消费侧复权迁移：一手源码盘点与改造方向（#157）

日期：2026-11-04  
范围：#157 待核实 spec、#159 单一价格读取接缝、#162 `query_daily` 新契约、#166 切换/legacy、#167 daily/T+0/北交所，以及 #149 的 SEPA 专项。  
结论性质：改造方向与测试计划，不是实现，也不是已完成迁移的验收报告。

## 0. 证据边界与总判断

本文只使用以下一手材料：

- 当前工作树的 `src/qstock_mcp`、`skills`、相关测试和 ADR；所有 `path:line` 均在本次研究中重新核对。
- GitHub Issues [#158](https://github.com/daanZao/q_stock_skills/issues/158)、[#159](https://github.com/daanZao/q_stock_skills/issues/159)、[#162](https://github.com/daanZao/q_stock_skills/issues/162)、[#166](https://github.com/daanZao/q_stock_skills/issues/166)、[#167](https://github.com/daanZao/q_stock_skills/issues/167)、[#157](https://github.com/daanZao/q_stock_skills/issues/157)、[#149](https://github.com/daanZao/q_stock_skills/issues/149) 的当前正文。
- 八份 `docs/research/2026-10-31-*.md` 已整体作废；本文没有引用其结论、实测数值或推断作为证据。

最重要的现状判断是：**目标契约已经由 Issue 定义，但当前代码尚未实现它。** 当前 `query_daily` 仍默认直读旧 `stock_daily` 的 qfq 行，响应没有 `price_basis` 或 `adjust`；强势股、覆盖核验和申万板块自算仍各自直接访问 `stock_daily`。因此不能只做 skill 文档替换，也不能在 skill 内自行复权；必须先完成 #159 的逐值不变收敛，再由 #158/#160/#161 等数据面工作提供新事实与派生，最后接 #162/#166/#167 和消费侧改造。

目标数据语义固定为：

```text
raw + B                         两个事实
F(t)       = B(t) / B0
hfq(t)     = raw(t) × F(t)       物化
qfq(t)     = hfq(t) / F_latest   读时派生；F_latest 按股票各自取值
```

`query_daily` 继续默认 qfq；每行增加 `price_basis`，顶层增加 `adjust{anchor,events,coverage}`。`complete` 必须同时满足 raw 连续、事件完整、B0 可信、因子路径合规且无未决异常。legacy 只读隔离，跨 2023-01-01 边界不混排。

### 优先级含义

- **必须先改**：不改会产生错误价格、假 complete、跨边界混排或迁移旁路；是切读或正式消费结论的前置。
- **可延后**：不阻塞 #159 的 prefactor 或首轮正确切读，可在兼容、审计展示和编排体验阶段完成；若文中限定“正式启用某 skill 前”，则以该限定为准。
- **无影响**：指数公式或业务定义不变；仍可能需改注释、fixture 或输入元数据，不能把“算法无影响”理解成“无需验证”。

## 1. 当前源码事实：读路径尚未收敛

### 1.1 `query_daily` 与仓储

- MCP 薄包装和核心层的默认参数都仍是 `adj="qfq"`：`src/qstock_mcp/server.py:52-61`、`src/qstock_mcp/tools_daily.py:262-275`。
- 自愈先按 `(stock_code, adj, start, end)` 查旧表日期，再按同一个 adj 补抓：`src/qstock_mcp/tools_daily.py:31-79`。
- 查询完成后直接调用 `select_daily`：`src/qstock_mcp/tools_daily.py:162-218`；最终响应只有现有 `params/range/data_range/count/healed/rows` 等字段：`src/qstock_mcp/tools_daily.py:278-299`。
- `_SELECT_SQL` 和 `select_dates` 都直接访问 `stock_daily` 并过滤 adj：`src/qstock_mcp/repository.py:45-49`、`src/qstock_mcp/repository.py:101-118`。行内目前没有 `price_basis`，顶层没有 `adjust`。
- 旧写入键是 `(stock_code, trade_date, adj)`：`src/qstock_mcp/repository.py:32-43`。该写入模型与 raw 主键 `(stock_code, trade_date)` 不同，不能只换 SELECT 表名。

### 1.2 其他个股价格读取旁路

- 强势股：直接查交易日、当日截面和 60 日 `MAX(close)`：`src/qstock_mcp/tools_strong.py:31-66`；入口还先调用 `select_dominant_adj`：`src/qstock_mcp/tools_strong.py:108-121`。
- 覆盖核验：`_dates` 是动态表 SQL，个股默认只数 qfq 行：`src/qstock_mcp/tools_coverage.py:11-23`、`src/qstock_mcp/tools_coverage.py:39-59`。
- 申万自算：最新日期、占优 adj 和聚合 SQL 都直接读 `stock_daily`：`src/qstock_mcp/repository.py:1169-1192`、`src/qstock_mcp/repository.py:1206-1244`；工具层依次选择日期和 adj：`src/qstock_mcp/tools_sw_board.py:81-107`。
- live 抽样虽经 `query_daily`，但又调用上述覆盖核验并将所有 `stock_daily.status != ok` 视为失败：`src/qstock_mcp/tools_live_sample.py:76-109`、`src/qstock_mcp/tools_live_sample.py:338-345`。

这正是 #159 要消除的结构性问题。skill 不直连数据库，不能替代这个服务端接缝；ADR 0001 同时要求指标留在 skill 侧，`query_daily` JSON 是跨仓库稳定契约：`docs/adr/0001-mcp-server-boundary.md:3-13`。

## 2. 实施顺序：先 prefactor，再切契约，最后开放消费

### 2.1 #159：单一价格读取接缝，legacy 默认行为逐值不变

**必须先改。** 建议由仓储层拥有一个读取模块/接口，至少覆盖四种消费形状，而不是只把 `select_daily` 改名：

1. 单股区间序列：`query_daily` 与自愈日期探测。
2. 单日全市场截面：强势股和申万聚合。
3. 每股窗口序列/窗口极值：60 日新高；若下推 SQL，也必须由同一接缝拥有。
4. 覆盖与最新日期：coverage、live-sample、申万缺省日期和强势股交易日历。

写入管线不应伪装成“价格读取接缝”的一部分；raw、事件、hfq、state 的写入由 #158/#167 的统一采集/重建管线负责。

prefactor 阶段的约束：

- 默认实现仍读旧 `stock_daily`，qfq/hfq/none 的每个字段值、排序、空结果、停牌标注、错误和 note 均不变。
- 不在 #159 提前除以因子、改默认 adj 或拼接 legacy；也不要把 #162 的元数据半套进旧响应。
- 为后续实现切换预留内部 provider/开关，但 #166 所要求的 `QSTOCK_PRICE_SOURCE=new|legacy` 对外切换、回滚和手册属于后续，不在 prefactor 中假装完成。
- 所有旧旁路 SQL 必须真正消失或被接缝内部封装；仅让 `query_daily` 走新函数而保留 tools_strong/tools_coverage/申万直读，不满足 #159。

### 2.2 #162：派生 qfq 与可机读的完整性

**必须先改，依赖 #159 和新数据面。** `query_daily` 的兼容边界是“默认 qfq、旧字段保留、纯增量字段”，不是“新旧价格数值永远相同”。旧 qfq 与新派生值的差异应归因，不能用旧值绑死正确的新算法。

消费侧可依赖的最低契约：

- 每行 `price_basis` 为 `qfq`、`hfq`、`raw` 或 legacy 查询的 `legacy_qfq`；同一响应不得混用。
- 顶层 `adjust.anchor` 是该股 `F_latest` 对应的实际锚点，`events` 带事件信息，`coverage` 为 `complete/partial/unknown`。
- `partial/unknown` 可以带行返回，但必须有 note；调用方不得仅凭 HTTP/MCP 调用成功或 `status=ok` 判定“数据可以使用”。
- qfq 使用**每股自己的** `F_latest`。强势股等全市场计算不能取一个全局除数，也不能直接对 hfq 做跨股票比较。
- 历史查询的 anchor 不必等于请求的 `end`：后续事件会重锚历史 qfq。消费方应记录 anchor，不应另造“请求日 anchor”或擅自把历史 qfq 当作当时实际成交价。

`complete` 的门槛必须集中实现并复用：raw 连续、事件集完整、B0 可信、权威 B 或通过对账的正式 pctChg 降级、无未决异常。北交所价格比、legacy、T+0 未确认和已知缺口不得判 complete。

### 2.3 #166：legacy 与切换边界

**必须先改。** 读取接缝负责执行，不让各 skill 自己判断表名或拼 rows：

- 完全位于 2023-01-01 以前的 legacy-only qfq 查询：每行 `price_basis=legacy_qfq`，`coverage=partial` 并有 note。
- 跨边界 qfq：明确范围错误并提示分段查询；不能返回一段 legacy_qfq 加一段新 qfq。
- 新窗口外的 hfq/raw：数据不可用；不得从 legacy qfq 反推。
- 正常期间不双写 legacy；回滚时 raw 与因子事实采集继续。skill 不需要、也不应实现第二采集器。
- 立即回滚、运行数日后的修复回滚和 roll-forward 都由小型测试库演练；这与 skill 文档改造是不同交付物。

### 2.4 #167 与 init/morning：消费方的数据就绪语义随写入改变

**必须先改的相关服务端路径：**

- 当前 `daily` 仍按 adj 走腾讯→新浪→baostock 并写旧表：`src/qstock_mcp/scheduler.py:375-475`。目标应改为 raw+B+重建；全市场 baostock 不可用时保留缺口，不自动做全市场替代源回填。
- 当前 morning 把全市场行直接 `upsert_daily_rows`：`src/qstock_mcp/tools_morning.py:52-80`；适配器还把 `adjustflag` 映射成旧 adj：`src/qstock_mcp/adapters/baostock_adapter.py:109-142`。目标是 raw、事件、派生和状态在统一管线中幂等处理，并区分“处理完成”与“校验后可以使用”。
- 当前 init 全量回溯固定从 1990 写 qfq：`src/qstock_mcp/tools_init.py:193-250`。目标不能再沿此路径生成新 qfq；新模型只覆盖规定回填窗口，窗口外保持 legacy 隔离。轻量 init 的股票清单、快照和指数部分不因个股复权改变。

skill 的就绪检查应消费 coverage 和异常状态，不再以“旧表当日有 qfq/none 行”为充分条件。单只 T+0 自愈仍可返回数据，但替代源或因子未确认时只能是 partial；北交所单只查询保留，缺行/缺键不能解释为无事件。

## 3. 按消费语义分组的改造方向

### A. 价格水平与连续价格序列：必须先改

这一组直接使用 OHLC 的绝对水平或跨公司行为连续序列，是复权错误会改变结论的核心面。

#### A1. indicator-tools 的价格序列指标

当前实现只是从 rows 抽取数值列，再调用纯函数：`skills/indicator-tools/scripts/indicators.py:248-307`、`skills/indicator-tools/scripts/indicators.py:369-395`。它不读取 `params.adj` 来决定算法，也不校验价格口径。

- MA、MACD、BOLL、RSI、diff1/diff2、rolling_max/min、maxdd、chg 和 vol_price 的**数学公式不重写**；正确做法是喂入同一 `price_basis` 的连续序列。
- **必须增加输入口径校验与元数据透传**：单股 rows 的 `price_basis` 必须一致；输出增加顶层 `price_basis`，并透传/摘要 `anchor`、`coverage`。多标的 corr 若消费价格列，应按标的保留 basis/anchor，不可假设不同股票 anchor 相同。
- `rolling_max/rolling_min` **不需要新增计算口径参数**。复权方式由 `query_daily(adj=...)` 和输入 `price_basis` 决定；再增加一个会改算法的 `--price-basis` 容易制造“参数写 qfq、rows 实为 raw”的双重真源。如需防线，只加“期望口径校验”，不做二次复权。
- 兼容旧 fixture 时可以接受缺失的新字段，但只能标为 pre-contract/unknown，不能伪造 complete。新 `query_daily` 的契约测试必须要求每行字段存在。
- 停牌剔除规则保持不变：`skills/indicator-tools/scripts/indicators.py:356-395`。事件可在停牌期间生效，但停牌占位行仍不进入交易指标。

现有输入示例和指标目录需要同步：`skills/indicator-tools/SKILL.md:26-53`、`skills/indicator-tools/SKILL.md:59-97`。这属于 #157 的 skill 实施票，不在本研究中改文档。

#### A2. SEPA：52 周高低与趋势模板

当前 SEPA 只检查 `status` 和 `count >= 250`，明确使用默认 qfq：`skills/sepa/SKILL.md:12-25`。52 周高低由 250 日 rolling 极值提供，趋势模板第 6/7 条直接与现价比较：`skills/sepa/SKILL.md:42-46`。

**必须先改的消费门槛：**

1. 除 `status=ok` 和长度外，正式趋势结论要求 `adjust.coverage=complete`。
2. 所有消费行必须为 `price_basis=qfq`；legacy_qfq、raw、hfq 或混合 basis 不得静默进入同一趋势模板。
3. 在结构化结论中记录 `price_basis`、anchor、coverage；partial/unknown 按数据缺口终止或降级，不输出“八条全部通过”。
4. 历史分析保留 anchor。不要把历史 qfq 高低描述成当时可直接成交的历史报价。

**#149 的建议定稿：** 52 周高/低、现价、50/150/200 日线及 VCP 价格结构统一使用同一 qfq 序列。现金分红等公司行为通过复权因子反映在序列中；报告名称应写“前复权 52 周高/低”，而非“历史实际成交最高/最低价”。raw 高低与 qfq 高低不得交叉比较。若未来另需“历史名义成交价”或“含分红总收益”指标，应另立语义，不在 #149 内混成一个开关。

#149 不要求重写整个 SEPA 方法论；基本面、催化剂、风险管理与妙想验证不是复权迁移面。

#### A3. `tools_strong`：60 日新高

当前定义是当日 dominant adj 的 close 等于 60 日最大 close：`src/qstock_mcp/tools_strong.py:7-16`、`src/qstock_mcp/tools_strong.py:108-149`。

**必须先改：**

- 60 日新高改经单一接缝读取该股 qfq 派生序列；不能对 raw close 求 max，也不能对 hfq 忘记逐股除以各自 `F_latest`。
- 交易日历、当日 change_percent/volume/amount/turnover_rate 使用 raw 事实；价格水平比较使用 qfq。两者应由接缝组成 coherent row，避免跨表日期或口径错配。
- 删除 `select_dominant_adj` 依赖。新模型没有按 adj 多行的横截面选择问题。
- 某股 raw 缺口、事件未知、B0 不可信或 rebuild_pending 时，不能把“未参与计算”写成“不是新高”。初版应排除该股的 60 日新高判定并输出可统计的排除原因；多次涨停分支仍可由 zt_pool 独立成立。
- `latest_price` 若继续填 qfq，必须带口径/anchor；若业务需要当日实际成交价，应另取 raw close，不能让同一个无标注字段兼有二者。

`strong_stocks` 是 limit-up-analysis 和 daily-review 的上游，所以这是消费切换前置，不只是内部重构。

### B. 涨跌幅、收益与量价比率：算法无影响，完整性门槛必须先改

#### B1. `change_percent` 消费

- corr 的标准用法直接消费上游 `change_percent`：`skills/indicator-tools/SKILL.md:99-110`；实现对齐日期后算 Pearson，不复权：`skills/indicator-tools/scripts/indicators.py:403-429`。
- deviation 直接对个股与基准指数的 `change_percent` 做差并滚动累计：`skills/indicator-tools/scripts/indicators.py:469-545`。
- 因此 corr（`--on change_percent`）与 deviation **无复权算法改造**。不得用相邻 raw close 自算涨跌幅替代权威字段，也不得为偏离值额外乘复权因子。

但 `status=ok` 不等于窗口可信。初版消费策略建议保守：个股腿 `coverage != complete` 时不进入正式 corr/deviation 数值结果，列入 data_gaps；指数腿没有个股 adjust 块，不应被错误要求提供该字段。未来若 coverage 增加结构化原因，可评估“仅因子未确认但权威 pctChg/raw 完整时允许 change_percent 消费”，这是可延后优化，不是首轮迁移前提。

#### B2. chg、maxdd、RSI、vol_price 等比率类指标

这些公式在正数全局缩放下部分结果不变，但仍依赖公司行为前后的连续序列，不能改吃 raw。公式不变，输入校验同 A1。

daily-review 当前前 75 个股用 corr、vol_price，连板股用 deviation：`skills/daily-review/SKILL.md:52-70`。应增加 coverage/basis 门槛，把 partial/unknown 个股从相应指标剔除并记录覆盖率；不得把 note 当作装饰后继续出确定数字。其“查询失败或行数不足才剔除”的旧规则需扩展：`skills/daily-review/SKILL.md:207-215`。

其中前 75“异常放量”最终只采信 `amount_chg`：`skills/daily-review/SKILL.md:63`。金额本身不复权，但当前 vol_price 输入同时计算价格环比；首轮可随整体 partial 保守剔除，未来按结构化缺口原因放宽纯金额消费属于可延后优化。

### C. raw 截面与申万板块：接缝必须先改，聚合公式无影响

申万自算只使用 `change_percent` 和 `amount` 加权，并统计成分/有效行：`src/qstock_mcp/repository.py:1213-1240`。这些字段不需要乘 F，因此：

- **必须**把 `LEFT JOIN stock_daily ... d.adj=%s` 收敛到单一 raw 截面接缝：`src/qstock_mcp/repository.py:1231-1234`。
- `select_dominant_adj` 及“多者胜、平局 qfq”应退役；raw 主键天然避免一股多 adj 重复计入。
- change_pct、up/down/flat、amount、stock_count、counted_count 的公式保持不变。成分字典 point-in-time、字典 `dict_as_of` 与停牌/无行情剔除也不变。
- 不应要求申万 raw 截面先派生 qfq；但必须暴露 raw 覆盖缺口，不能把事件未知伪装成无行情。
- 对外 `adj` 字段若暂时保留用于兼容，只能是过渡字段；新口径应以 `price_basis=raw` 表达，skill 不再解释“占优 adj”。

daily-review 的两段模板必须随之改：`skills/daily-review/SKILL.md:138-148`。删除“当日 stock_daily 各 adj 行动态选择”和“次晨写 adj=none、无需等 qfq”的旧叙述，改为 raw 截面及 raw coverage；5/10 日 `change_pct` 连乘和日度再平衡语义不变：`skills/daily-review/SKILL.md:146-150`。

### D. coverage、init、live-sample：必须先改

`tools_coverage` 不能继续把“qfq 日期行数齐”当作历史覆盖完整：

- 默认按 raw 交易日连续性核验，并同时检查事件集、B0/state、正式降级对账和未决异常。
- `query_daily.adjust.coverage` 与 CLI coverage 应复用同一判定内核，避免一个报 complete、另一个报 gap。
- 保留交易日历、首末日期、missing_days 等有用结构，增加事件/基线/异常维度；raw 缺行、事件未知和 rebuild_pending 必须可分别归因。
- CLI 当前无 adj 参数、直接采用核心默认 qfq：`src/qstock_mcp/scheduler.py:561-583`。迁移后不应仅把默认改成 hfq 来“过关”，因为派生表完整不能证明 raw/事件完整。
- init 回溯验收要断言 raw+事件+hfq+state，而不是旧 `stock_daily` 行数；当前旧断言见 `tests/test_init_full.py:110-122`。
- live-sample 覆盖状态需认识 partial/unknown。它当前把每项非 ok 都归 error：`src/qstock_mcp/tools_live_sample.py:338-345`；北交所显式样本可为 partial，但不得汇总成“全量 complete”。抽样结果应分别报告 raw 可用、事件未知和查询失败。

### E. 四个 skill 的编排与文本

| Skill | 必须先改 | 可延后 | 无影响 |
| --- | --- | --- | --- |
| indicator-tools | 校验单股 rows basis 一致；coverage/anchor/basis 透传；新契约 fixture |  richer 的分指标缺口原因展示；旧 fixture 的兼容提示 | 所有数学公式；promote；deviation/corr 的 change_percent 算法 |
| sepa | complete+qfq 门槛；52 周高低与现价同 qfq；结论记录 anchor/basis/coverage | 更细的 VCP 教学示例、总收益等另立研究 | 基本面、妙想验证、催化剂和仓位规则 |
| daily-review | 前 75/连板序列 coverage 门槛；申万模板去 dominant-adj；data_gaps 记录新原因 | 基于结构化原因放宽纯 amount/change_percent 消费；报告展示优化 | 指数、快照、market_boards、板块 chg、结论 upsert 流程 |
| limit-up-analysis | strong_stocks 就绪文本改 raw+coverage；60 日新高缺口可区分 | 在结论中补充 strong coverage 汇总 | promote、zt_pool、涨跌停池采集、封单额、市场快照市值/PE 归因 |

limit-up-analysis 当前并不从个股 raw/qfq 重新判定涨停：晋级率输入是昨/今 zt_pool 的代码与连板数：`skills/limit-up-analysis/SKILL.md:27-40`。因此**不要借复权迁移新增一套涨跌停价格算法**。需要改的是 strong_stocks 自算输入说明，目前仍写“stock_daily 当日行未就绪就先补 daily”：`skills/limit-up-analysis/SKILL.md:20-25`。新文本应要求统一 raw+因子管线及 coverage 检查，并允许 zt_pool/多次涨停分支独立给出结果。

daily-review 的 `query_daily(end=<trade_date>)` 规则继续保留：`skills/daily-review/SKILL.md:43-46`。它防止把执行日之后的行带入序列；qfq 的 anchor 则可能因后续公司行为晚于复盘日，两者不是同一概念，报告中应分别记录。

## 4. 明确可延后或无影响的旁支

- **指数**：#158 明确不在范围。`query_index_daily`、指数 corr/vol_price/chg、大盘 Stage 判断不因个股 raw+B 改造而改公式。不要给指数 rows 强行添加假的个股 adjust 状态。
- **快照**：market_snapshot 的广度、成交额 topN、市值和 PE 消费不变；快照不能因此成为历史 raw 来源。
- **基本面与资讯**：get_fundamentals、mx_query/mx_search/query_news 与复权迁移无关。
- **promote 与 zt_pool**：晋级率、连板梯队、涨停原因、封单额不读复权价格。
- **申万官网 market_boards 与估值表**：直接发布板块数据的读取不变；只有库内个股自算路径需要 raw 接缝。
- **结论存储**：save_conclusion/upsert 不变；只扩充各 skill 自己拥有的 payload 元数据。
- **daily --check**：仍是上游对拍，不可替代 raw+B 派生、coverage 或消费验收。
- **indicator-tools 输出展示细节**：只要首轮已有 basis/anchor/coverage 可审计，更美观的文本、分原因放宽策略可延后。

## 5. 测试策略

### 5.1 #159 prefactor：先冻结 legacy 行为

目标是逐值不变，不是验证新算法。

1. 对旧 `stock_daily` 固定 qfq/hfq/none 行，比较接缝引入前后 `select_daily`/`query_daily` 完整 dict：字段、值、顺序、空集、healed、note、source、tradestatus、suspended 均一致。
2. 对默认 qfq 做显式断言；重复查询幂等、中段缺口不重抓和全源失败不返回旧数据继续沿用 `tests/test_query_daily.py:22-91`。
3. 停牌权威/启发式行为不变，保留 `tests/test_tradestatus.py` 的现有场景。
4. strong 的上升/下降/次新股/多次涨停 fixture 逐值一致；现有种子与断言在 `tests/test_zt_pool_ths.py:206-218`、`tests/test_zt_pool_ths.py:249-310`。
5. 申万 point-in-time、停牌归一和 dominant-adj 旧行为在 prefactor 阶段逐值一致；现有定值见 `tests/test_sw_board_daily.py:116-142`、`tests/test_sw_board_daily.py:242-318`。
6. 静态搜索确保生产代码不再有接缝外 `stock_daily` 价格 SQL；测试种子 SQL 不计为生产旁路。

### 5.2 raw+B/F/hfq/qfq：定值与全量门控

1. 使用 #158 §3.6 的 688300 事件向量核对 `B0/B/F/hfq/qfq`；另覆盖 600381 的 f>1/停牌事件和 600094 的恢复上市边界。不得增加 f<=1 或 F 单调约束。
2. 同一 raw+事件集重复重建，hfq/state 哈希一致；新增、修订、撤销事件只重建受影响区间，qfq 随新 F_latest 重锚。
3. 多股票同响应/截面使用不同 F_latest，专门防止误用全局除数。
4. 对 raw 缺口、事件请求失败、成功空事件、B0 不可信、未决异常、正式 pctChg 降级分别断言 coverage；只有全部满足时才 complete。
5. legacy-only、跨边界 qfq、新窗口外 hfq/raw 分别做契约测试；断言无混排、无反推。
6. T+0 替代源、北交所单只/显式批补均为 partial；缺行或缺键不写成“无事件”。

### 5.3 indicator-tools 与 SEPA

1. 保留现有手算数值、min_periods、null 头部和退出码测试；当前基线在 `skills/indicator-tools/tests/test_indicators.py:61-196`。
2. 同一数值序列分别附 qfq 完整元数据与旧 pre-contract 输入，算法值一致；新输入额外断言 basis/anchor/coverage 透传。
3. 混合 price_basis、partial/unknown、缺失 basis 的新契约输入要有明确行为；不得静默标 complete。
4. corr/deviation 保留 change_percent 定值与停牌对齐测试；指数腿无 adjust 不应报错，个股腿 partial 按约定拒绝或降级。
5. 构造跨公司行为的 250+ 日 SEPA fixture，独立计算 qfq 52 周高低和现价比较；至少覆盖 688300 类送转事件。断言 raw 与 qfq 不混用、趋势第 6/7 条使用同一序列，partial 不出正式通过结论。
6. rolling_max/min 不新增会改变数值的参数；测试重点是输入口径，不是再写一套复权算法。

### 5.4 strong、coverage、申万与 skill 编排

1. strong 用公司行为前后的 raw/hfq/qfq 定值验证 60 日新高；覆盖“raw 假跳空但 qfq 连续”和“不同股票不同 F_latest”。事件未知/B0 不可信的股票有排除统计，不被误判为非新高。
2. 多次涨停分支在 60 日价格不可用时仍按 zt_pool 独立成立，reason 不虚构新高。
3. coverage 分别注入 raw 缺日、事件缺失、B0 异常、rebuild_pending；即使 hfq 行数齐也不得 complete。CLI 与 `query_daily` 对同一 fixture 结论一致。
4. 申万在 raw 新模型上重跑现有加权定值；断言无 adj 重复计入、结果与复权因子无关，缺口经 raw coverage 呈现。
5. live-sample 增加北交所 partial、事件未知和 raw 完整三态，不能只用 ok/error 掩盖差异。
6. skill 层以固定 query_daily JSON 做端到端 stdin/stdout 验收；daily-review/limit-up-analysis 的 LLM 文本部分用契约清单检查 data_gaps、anchor/basis 和 strong 就绪表述，不伪称能做确定性语言单元测试。

### 5.5 切换与回滚

按 #166 在小型 `qstock_test` fixture 上演练：legacy-only、跨边界拒绝、立即回滚、运行数日后由统一管线修复 legacy 缺口、roll-forward。断言正常期不双写、回滚期 raw+因子采集不停、新表保留。不得为测试复制或重放全量业务数据。

建议实施期运行：

```bash
.venv/bin/pytest tests/test_query_daily.py tests/test_tradestatus.py \
  tests/test_zt_pool_ths.py tests/test_sw_board_daily.py \
  tests/test_first_run.py tests/test_init_full.py tests/test_live_sample.py
.venv/bin/pytest skills/indicator-tools/tests/
```

本次研究对当前未改代码执行了上述相关现状集合（合并一次运行），结果为 **135 passed in 19.25s**。这只证明当前 prefactor 前基线可运行，不证明新 raw+B 契约已经实现。

## 6. 范围边界与实施票划分

- **#158**：数据模型、因子、采集、存量迁移和总验收；本文不重复设计 DDL，也不执行数据库变更。
- **#159**：先做所有个股价格读取的 prefactor，默认旧表，逐值不变。它是消费改造的共同前置。
- **#162**：默认 qfq 的内部派生、行级 basis、顶层 adjust、coverage 与 legacy 查询契约。skill 不应自行实现这些规则。
- **#166**：开关、只读 legacy、回滚与 roll-forward；不在 skill 中写切换或补库逻辑。
- **#167**：daily 语义、T+0 自愈、北交所开关；skill 只消费其 coverage/异常结果。
- **#157**：本研究对应的是规划，不是实施。实际修改四个 skill 的代码/SKILL.md 应另起实施票，并等服务端契约可用后落地。
- **#149**：只定稿 SEPA 52 周高低及其 rolling 消费口径；不扩成 SEPA 全方法论重写，也不引入总收益指标。
- 指数、ETF/基金/可转债、快照作历史 raw、全市场替代源自动回填、新涨跌停判定算法均不在本次消费迁移范围。

## 7. 最终改造清单

按提交/验收顺序，最小闭环为：

1. **必须先做 #159**：`query_daily`、tools_strong、tools_coverage、tools_sw_board、live-sample 间接覆盖全部收敛；legacy 默认逐值不变。
2. **必须先完成数据面与 #162/#166/#167**：raw+B/F/hfq/qfq、统一 coverage、2023 边界、daily/morning/init/T+0/BSE 语义可用。
3. **必须先改价格水平消费者**：strong 60 日新高、SEPA/rolling 及 indicator-tools 的 basis+coverage 防线。
4. **同步改编排文本**：daily-review 的 partial 剔除和申万 raw 模板；limit-up-analysis 的 strong 就绪说明。
5. **算法保持不变并回归**：change_percent corr/deviation、promote、指数、快照、申万加权公式、结论存储。
6. **可延后**：基于更细 coverage reason 放宽纯 amount/change_percent 消费，以及报告展示优化；不得以此拖延首轮的完整性门控。

核心原则是：**复权只在统一数据/读取接缝发生一次，指标只消费带口径与覆盖状态的序列；价格水平、权威涨跌幅和 raw 截面分别处理，legacy 永不与新序列混排。**
