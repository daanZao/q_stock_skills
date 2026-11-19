# ADR 0020：申万板块官网发布行情、本地自算与 source 层级

2026-11-19，issue #199。本 ADR 补记并锁定 #75/#92/#105/#111/#118 已实施但分散在 issue、代码和手册中的设计，不改变板块主口径、采集链或消费行为。

## Context

东财链路退役时，boards 行业维度选择了申万一级/二级官网发布指数作为主口径，同时保留更早实现的库内成分股自算作降级与交叉验证。此后出现了三类歧义：

- 把申万官网发布的指数行情与“申万成分字典 × 个股日线”的本地自算混称为 `swsresearch`；
- 把 current 中由官网指数最新价/昨收本地换算的涨跌幅，误认为按成分股聚合；
- 把工具响应/采集台账的 `source='swsresearch'` 与 `market_boards.source` 列混为一谈。

这些误解会改变复盘板块口径、历史序列拼接和就绪归因，不能仅依赖代码注释反复澄清。

## Decision

### 1. 官网发布行情与本地自算是两条独立数据链

**`market_boards` 承载上游发布板块指数行情及退役留档，不承载成分股本地聚合结果。**

- current：`sw-boards` / close → `SwSResearchAdapter.fetch_boards` → 申万 current → `map_sw_current_rows` → `upsert_board_rows`。`latest_price=l8`、`pre_close=l3`，本地计算 `change_percent=(l8-l3)/l3×100`、`change_amount=l8-l3` 和振幅。这里的 L 列输入是官网指数点位，不是成分股。
- trend：`backfill --source sw` → `fetch_sw_board_history` → 申万 trend → `map_sw_trend_rows` → `upsert_board_history`。`change_percent=markup`、`change_amount=hike` 为官网发布值；preclose 取上一行、振幅本地计算。
- 申万成分分类 xls 是 `sw_industry_class` 的参考事实来源，不等于 `market_boards` 的涨跌幅由成分股计算。

**`query_sw_board_daily` 是独立的查询时自算链。**

- point-in-time 申万成分字典 × 库内个股日线，按成交额加权计算 `change_pct`，并输出涨跌家数、成交额和成分覆盖。
- 口径固定标为 `basis='sw_amount_weighted'`；结果不写入 `market_boards`。
- #75 建立该工具，#92 修正横截面 adj；#105/#111/#118 将其保留为官网发布行情的降级与交叉验证，而非 current/trend 的写入函数。

官网发布指数收益与本地成分股成交额加权收益不是同一算法。任何报告或对拍必须保留实际 `basis`，不得把两条链拼成无口径标识的同一序列。

### 2. `swsresearch` 必须按来源层级解释

- **适配器/动作来源**：`SwSResearchAdapter.name='swsresearch'` 可出现在工具响应和采集台账，current 与 trend 都可能使用该标记。
- **表内行级来源**：current 经 `BOARD_FIELDS` 通用 upsert，字段集不含 source，因此 `market_boards.source=NULL`；trend 由 `upsert_board_history` 显式写 `source='swsresearch'`。
- **存量留档**：NULL 还可能包含旧东财实时行，`push2his` 可能存在于退役回补留档。必须结合 `board_type`、业务日期和台账归因，不能只用 source 判断全部官网数据或本地自算。

手册或对拍响应中的 `source='swsresearch'` 若指适配器/采集动作，不能据此推断表内 current 行也写了该值。

### 3. 消费与覆盖边界

- 当前 daily-review 以申万官网发布的 `industry_l1/industry_l2` 行为板块主口径，缺失时按既有规则整节降级到 `query_sw_board_daily`，并做叙述层口径断交。
- current 仅取当前实时/收盘截面，历史日期走 trend 回补；current 无行不能由 trend 的历史能力反向解释。
- `market_boards` 的物理业务键是 `(trade_date, board_type, board_name)`，不是 board_code；唯一键不含 source，同键不同来源不能并存为多套事实。
- 本 ADR 不决定未来是否改成“全部本地自算”。若要推翻官网主源 + 自算降级的既有选择，须以新证据另起 spec/ADR。

## Considered Options

- **官网 current/trend 主源 + 本地自算降级**：采用。官网提供发布指数和深至 1999 的历史，自算提供无官网时的独立降级与交叉验证；两者价值不同。
- **全部使用本地成分股自算**：未采用。它不能复现官网发布指数算法，且不能替代官网历史序列；如未来选择它，属于主口径变更而非文字澄清。
- **把 `swsresearch` 当作全链路单一来源标志**：拒绝。响应、台账和表内列的写入机制不同，强行等同会误报来源与口径。

## Consequences

- 数据库字典、CONTEXT、板块手册和消费侧 spec 必须区分 current、trend、自算及来源层级。
- 字段级来源应如实标注：current 涨跌幅为 L，trend 的 markup/hike 为 U；自算结果只标 `sw_amount_weighted`，不标成官网发布值。
- 复盘不得因 current/自算其中一条链缺行，就默认另一条链同口径缺失；降级必须显式切换并标注。
- 若未来修改 current 的 source 落库行为、统一表内来源或改变主口径，需要同步本 ADR、字典和迁移验收；历史行不会因文档澄清自动改写。

## Evidence

- 决策：[逐能力面替代源定稿 #105](https://github.com/daanZao/q_stock_skills/issues/105)、[剔除东财迁移 spec #111](https://github.com/daanZao/q_stock_skills/issues/111)、[申万 boards 行业迁移 #118](https://github.com/daanZao/q_stock_skills/issues/118)。
- 自算：[query_sw_board_daily #75](https://github.com/daanZao/q_stock_skills/issues/75)、[横截面 adj 修正 #92](https://github.com/daanZao/q_stock_skills/issues/92)。
- 代码：`src/qstock_mcp/adapters/sw_adapter.py`、`_sw_board.py`，`src/qstock_mcp/tools_board.py`、`repository.py`、`tools_sw_board.py`、`price_reader.py`。
- 调研：`research/sw-boards-mapping` 分支的 `docs/research/2026-10-20-sw-boards-mapping.md`；操作手册 `docs/sw-boards-cli.md`。
