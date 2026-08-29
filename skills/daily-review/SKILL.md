---
name: daily-review
description: 每日复盘编排（午市/全天），市场级总结。当用户请求每日复盘/收盘复盘/午市复盘时使用；经 MCP 查询做数据就绪检查，agent 生成复盘结论与 markdown 报告，结论经 save_conclusion 落库（daily_review.close / daily_review.midday），报告由本 skill 落盘、路径记入结论 payload。
---

# 每日复盘

收盘后（午市或全天）基于当日盘面数据做的市场级总结（术语见 `CONTEXT.md`）。本 skill 为每日复盘编排的 agent skill，**轻脚本、重编排**：编排逻辑在本文件，agent 自身即 LLM，复盘结论与 markdown 报告由 agent 手写生成，不调外部 LLM API。数据一律来自 MCP 工具（`query_board_data`、`query_snapshot`、`fetch_board_snapshot`、`fetch_market_snapshot`），不直接访问数据库。结论经 `save_conclusion` 落库（通用结论表，见 ADR 0003 `docs/adr/0003-generic-conclusions-table.md`）；markdown 报告不走 MCP，由本 skill 落盘，路径记入结论 payload。

## 触发与模式

- 由用户手动触发，无常驻调度。
- `mode` 仅两个取值：`close`（默认，收盘后全天复盘）、`midday`（午市盘中复盘，产物须标注午市时点）。
- 结论类型与模式一一对应：`daily_review.close` / `daily_review.midday`；subject 固定为 `subject_type="market"`、`subject_code="_market"`。
- 同日两个模式各自独立成行（业务键含 conclusion_type），同日同模式重复跑为 upsert，不产生重复结论；close 不覆盖 midday。

## 编排流程（按顺序执行）

1. **确定交易日与模式**：`trade_date` 格式 `yyyymmdd`，缺省取当日，用户可指定。
2. **数据就绪检查**：对当日逐表查行数——
   - 核心 section（任一 `count=0` 即数据不全）：`market_indices` / `market_boards` / `zt_pool` / `strong_stocks` 经 `query_board_data(table, trade_date)`，`market_snapshot` 经 `query_snapshot(trade_date)`。
   - 可选：`lhb_basic` 经 `query_board_data("lhb_basic", trade_date)`。
   - 核心缺失先补抓后重查：`fetch_board_snapshot(trade_date, sections=<缺失 section 逗号分隔>)`（sections 取值 `indices,boards,zt_pool,strong_stocks,lhb`）、`fetch_market_snapshot(trade_date)`；重查仍缺则列入 `data_gaps`，结论降级。
   - `lhb_basic` 为 0 不阻断：close 模式仅记 warning（龙虎榜盘后发布，可能尚未更新），midday 模式属预期内。
   - **核心 section 全部缺失且补抓失败时中止复盘**，向用户明确报告缺失范围与失败原因，不得基于残缺数据生成完整复盘结论。
3. **读数**：`query_board_data` 取各核心表当日全部行；`query_snapshot` 取当日全市场行（广度与成交聚合用）；lhb 各表有数据则一并取（`lhb_basic` / `lhb_stock_statistic` / `lhb_yyb_capital` / `lhb_yyb_most`）。
4. **生成两份产物**：agent 手写结构化结论 JSON 与 markdown 复盘报告（内容框架与报告格式见下）。所有数字必须来自步骤 3 的查询结果，绝不编造。
5. **报告落盘**：写入 `skills/daily-review/reports/daily_review_<yyyymmdd>_<mode>.md`（目录不存在则创建）。
6. **结论落库**：`save_conclusion(subject_type="market", subject_code="_market", trade_date=<yyyymmdd>, conclusion_type="daily_review.<mode>", payload=<结论 JSON>)`；payload 必须含 `report_path`（报告相对仓库根的路径）。**返回 `status != "ok"` 即中止复盘**，向用户报告失败原因与已生成产物的位置——不产生"分析了但没存下"的假象，落库成功才算复盘完成。
7. **核验（可选）**：`query_conclusions(subject_type="market", trade_date=<yyyymmdd>, conclusion_type="daily_review.<mode>")` 确认落库行存在且仅一行；重跑时步骤 6 的 `save_conclusion` 返回 `outcome: "updated"`。

## 复盘内容框架（五维度，对应数据表）

1. **指数表现**：`market_indices`——主要指数点位、涨跌幅、振幅、量比。
2. **市场广度与成交**：`market_snapshot` 聚合——涨/跌/平家数、涨停/跌停家数、两市成交额。
3. **板块轮动**：`market_boards`——行业板块为主、概念板块为辅，领涨/领跌板块与领涨股。
4. **涨停/强势股结构**：`zt_pool`——涨停/跌停/炸板家数、连板高度、炸板率（炸板 ÷（涨停 + 炸板））；`strong_stocks`——入选理由聚类。
5. **龙虎榜资金**：`lhb_basic` 净买卖前列、`lhb_stock_statistic` 近三月常客、`lhb_yyb_capital` / `lhb_yyb_most` 席位动向；数据缺失则标注，不强行展开。

各表字段名以 `query_board_data` 实际返回为准。

## 结论 payload 契约

payload 结构由本 skill 约定（server 不校验，见 ADR 0003），建议字段：

- `mode`、`report_path`（必填；报告落盘路径）
- `overall_stance`（总体立场一句话）、`core_logic`（核心逻辑）
- `indices` / `breadth` / `sectors` / `limit_up_structure` / `lhb_capital`（对应五维度，缺失维度置 null 并在 `data_gaps` 标注）
- `risk_alerts`（风险提示）
- `data_gaps`（缺失维度列表，无缺失为空数组）

## 报告格式

固定章节：标题 `## <yyyymmdd> 每日复盘（收盘）` / `## <yyyymmdd> 每日复盘（午市）`（含日期与时点）→ 总体立场一行 → 一、指数表现 → 二、市场广度与成交 → 三、板块轮动 → 四、涨停/强势股结构 → 五、龙虎榜资金动向 → 六、风险提示 → 数据缺口（逐条列出缺失维度及其对结论的影响）→ 末尾注明数据来源表与快照时点。

## 数据缺失红线

- **数据缺失不编造**：某维度数据缺失时，在结论 `data_gaps` 与报告"数据缺口"章节标注，该维度结论降级为定性描述，不得编造点位、金额或家数。
- **核心数据全缺即中止**：就绪检查核心 section 全部缺失且补抓失败时，中止复盘并向用户报告缺失范围与失败原因；部分缺失时标注缺失维度后继续。
- **落库失败即中止**：`save_conclusion` 失败时向用户报告失败，不算复盘完成。
