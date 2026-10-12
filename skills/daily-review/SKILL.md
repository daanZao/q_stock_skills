---
name: daily-review
description: 每日复盘编排（午市/全天），市场级总结。当用户请求每日复盘/收盘复盘/午市复盘时使用；经 MCP 查询做数据就绪检查，agent 生成复盘结论与 markdown 报告，结论经 save_conclusion 落库（daily_review.close / daily_review.midday），报告由本 skill 落盘、路径记入结论 payload；涨停/连板章节经 limit-up-analysis 子技能，指数历史经 query_index_daily，相关性矩阵与量价判定经 indicator-tools 管道（corr/vol_price）计算。
---

# 每日复盘

收盘后（午市或全天）基于当日盘面数据做的市场级总结（术语见 `CONTEXT.md`）。本 skill 为每日复盘编排的 agent skill，**轻脚本、重编排**：编排逻辑在本文件，agent 自身即 LLM，复盘结论与 markdown 报告由 agent 手写生成，不调外部 LLM API。数据一律来自 MCP 工具（盘面/快照经 `query_board_data`、`query_snapshot`、`fetch_board_snapshot`、`fetch_market_snapshot`，指数历史经 `query_index_daily`，资讯经 `mx_search`、`query_news`），不直接访问数据库；相关性矩阵与量价判定经 indicator-tools 管道（`corr` / `vol_price`，指标目录见 `skills/indicator-tools/SKILL.md`）计算，agent 不得手算这些数字；涨停/连板章节由 limit-up-analysis 子技能承担（见 `skills/limit-up-analysis/SKILL.md`），其晋级率/梯队数字只认其落库结论 `limit_up.close`。结论经 `save_conclusion` 落库（通用结论表，见 ADR 0003 `docs/adr/0003-generic-conclusions-table.md`）；markdown 报告不走 MCP，由本 skill 落盘，路径记入结论 payload。

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
   - 指数历史（相关性/量价复盘的前置）：7 个主要指数逐个 `query_index_daily(index_code, limit=60)`（000001/399001/399006/000300/000016/000905/000688），`count<60` 的指数标注出来。指数日线属 `init_database` 轻量初始化部分，缺失时可经 `init_database` 补齐后重查；仍缺则归因与量价复盘的相关性维度降级，列入 `data_gaps`，不阻断其余复盘。
   - 资讯搜索落库（可选）：就绪检查后 `mx_search(query=<资讯问句>)` 搜近期资讯并落库 `news_items`——大盘级用缺省 subject（`market`/`_market`，如"今日A股市场要闻"），焦点个股传 `subject_type="stock"` + `subject_code=<6 位代码>`。mx-search 配额与 mx-data **独立计数**（默认各 20 次/日，响应回显 `quota:{skill,used,limit}`），单次返回上限约 15 条（响应内重复条目落库前已按 news_code 去重）；本地配额触顶（`status:"error"`，不调上游）、上游自身日限（code=113）或 `MX_APIKEY`/`PG_DSN` 未配置时跳过本步，资讯缺失记入 `data_gaps`，不阻塞复盘。
   - **核心 section 全部缺失且补抓失败时中止复盘**，向用户明确报告缺失范围与失败原因，不得基于残缺数据生成完整复盘结论。
3. **读数**：`query_board_data` 取各核心表当日全部行；`query_snapshot` 取当日全市场行（广度与成交聚合用，同时供成交额前 75 选股）；lhb 各表有数据则一并取（`lhb_basic` / `lhb_stock_statistic` / `lhb_yyb_capital` / `lhb_yyb_most`）。近期资讯经 `query_news` 读库（按资讯发布时间倒序）：大盘级 `query_news(subject_type="market", subject_code="_market", start=<近 7 日起>, limit=10)`，焦点个股按 `subject_type="stock"` + `subject_code` 同法读取；`count:0`（库内无资讯）属正常，非错误。新增块的取数路径：
   - **指数历史**：就绪检查已逐指数取回 60 根（`query_index_daily(index_code, limit=60)`，rows 按 trade_date 升序，含 OHLCV/amount/turnover_rate/amplitude/change_percent），直接复用，不重复查询。
   - **成交额前 75 名单**：从 `query_snapshot` 当日全市场行按 `amount`（成交额）降序取前 75 只；名单（代码/名称/成交额/涨跌幅）即报告附录表格的数据源。
   - **前 75 个股历史序列**：逐股 `query_daily(stock_code, days=60)`（明确接受 75 次调用——热门股成交对其指数/板块影响大，是重要因子）；单股查询失败或行数不足时记入 `data_gaps` 并从相关个股分析中剔除，不中断其余个股。
   - **两市成交额环比基准**：`query_snapshot(<前一交易日>)` 取昨日全市场行，聚合昨日两市成交额供环比（前一交易日探测法与 limit-up-analysis 同：自然日回溯，最多 5 个自然日，命中 `count>0` 即停）。
4. **涨停/连板子模块（close 模式）**：编排调用 limit-up-analysis 技能（其自行做数据就绪检查、promote 管道计算并落库 `limit_up.close`）；随后 `query_conclusions(conclusion_type="limit_up.close", trade_date=<当日>)` 读其 payload，取 `summary`/`ladder`/`promote_rate`/`attribution`/`leaders` 编入复盘。**midday 模式不调用**（该技能仅 close 模式）：涨停章节降级为 `zt_pool` 结构统计（涨停/跌停/炸板家数、连板高度、炸板率 = 炸板 ÷（涨停 + 炸板））加 `strong_stocks` 入选理由聚类。
5. **指标管道计算**（输入经临时文件或 heredoc 传 stdin，与 limit-up-analysis 调 promote 同法；退出码语义：0 = ok / insufficient_data，1 = 输入错误，2 = 参数错误）：
   - **指数两两相关性**：7 份 `query_index_daily` 输出组装 corr 输入 `{"status":"ok","series":{<index_code>:{"status":"ok","rows":[...]}}}`；corr 窗口由传入序列长度控制（`--tail` 不适用），按序列尾部切 20 根 / 60 根各喂一次，得 20 日与 60 日两张矩阵：
     ```bash
     <corr 输入 JSON> | python skills/indicator-tools/scripts/indicators.py --indicator corr --on close
     ```
   - **个股-所属指数相关性**：前 75 逐股与其所属指数做一次 corr（个股 `query_daily` 输出 + 该指数 `query_index_daily` 输出两个 series，尾部 20 根），识别跟涨指数还是独立行情。所属指数按上市板归属：沪市主板（60 开头）→ 000001、科创板（688 开头）→ 000688、深市主板（00 开头）→ 399001、创业板（30 开头）→ 399006；北交所个股（4/8/9 开头）无对应主要指数，跳过并标注。
   - **大盘量价判定**：上证指数（000001）的 `query_index_daily` 输出直接喂 vol_price（其 rows 含 close/amount，与 `query_daily` 输出契约同形）：
     ```bash
     <query_index_daily 输出 JSON> | python skills/indicator-tools/scripts/indicators.py --indicator vol_price
     ```
     取输出末点（当日）的 `price_chg`/`amount_chg`/`vol_price`/`divergence`；与两市成交额环比（当日快照聚合 ÷ 昨日快照聚合 - 1）互相印证。
   - **前 75 异常放量标注**：逐股把 `query_daily` 输出喂 vol_price，取末点 `amount_chg`；`amount_chg ≥ +100%`（成交额翻倍，本 skill 约定口径，报告中注明）的标注为异常放量，列入复盘。
   - **采信纪律**：相关系数、`price_chg`/`amount_chg`、`vol_price` 形态标签、`divergence` 一律取管道输出，agent 不得手算或改写；`insufficient_data` 处理见"数据缺失红线"。
6. **生成两份产物**：agent 手写结构化结论 JSON 与 markdown 复盘报告（内容框架与报告格式见下）。所有数字必须来自步骤 3–5 的查询与管道结果，绝不编造；归因叙事由 agent 撰写，但每个数字必须锚定 query / corr / vol_price 的输出。
7. **报告落盘**：写入 `skills/daily-review/reports/daily_review_<yyyymmdd>_<mode>.md`（目录不存在则创建）。
8. **结论落库**：`save_conclusion(subject_type="market", subject_code="_market", trade_date=<yyyymmdd>, conclusion_type="daily_review.<mode>", payload=<结论 JSON>)`；payload 必须含 `report_path`（报告相对仓库根的路径）。**返回 `status != "ok"` 即中止复盘**，向用户报告失败原因与已生成产物的位置——不产生"分析了但没存下"的假象，落库成功才算复盘完成。
9. **核验（可选）**：`query_conclusions(subject_type="market", trade_date=<yyyymmdd>, conclusion_type="daily_review.<mode>")` 确认落库行存在且仅一行；重跑时步骤 8 的 `save_conclusion` 返回 `outcome: "updated"`。

## 复盘内容框架（七块）

1. **指数表现**：`market_indices`——主要指数点位、涨跌幅、振幅、量比；结合 `query_index_daily` 的近期序列（20/60 日）说明指数所处位置。
2. **市场广度与成交**：`market_snapshot` 聚合——涨/跌/平家数、涨停/跌停家数、两市成交额（含对昨日的环比）。
3. **板块轮动**：`market_boards`——行业板块为主、概念板块为辅，领涨/领跌板块与领涨股；与第五块的前 75 聚类结果交叉印证（领涨板块是否同时为成交集中方向）。
4. **涨停/连板分析**：close 模式引用 limit-up-analysis 子技能的 `limit_up.close` 结论——`summary` 一句话摘要、连板梯队与晋级率、归因三维、龙头点评，报告中原样引用其数字并注明来自该结论；midday 模式降级为 `zt_pool` 结构统计——涨停/跌停/炸板家数、连板高度、炸板率（炸板 ÷（涨停 + 炸板））；`strong_stocks` 入选理由聚类两种模式均保留。
5. **成交额前 75 结构分析**：口径为 `query_snapshot` 当日全市场行按成交额（`amount`）降序取前 75。以板块聚类为主——"前 75 中 X 行业/概念占 N 只、合计成交 Y 亿"；结合热门股对其指数/板块的影响因子（个股权重感：成交额占两市比例、经 corr 与所属指数的 20 日相关性）。定性分析为主；完整 75 只名单放报告附录表格。行业归属经数据交叉确认——`zt_pool.industry` / `strong_stocks.industry` 命中、`market_boards` 领涨/领跌板块与名单的交叉、资讯佐证；无法确认归属的个股单列"归属未确认"，不得凭记忆归类。
6. **归因与量价复盘**：
   - **相关性**：7 个主要指数两两相关性矩阵（20 日/60 日，corr 输出），看市场共振/分化；前 75 个股与所属指数的 20 日相关性，识别跟涨指数还是独立行情。
   - **量价**：大盘以上证指数日线喂 vol_price，得当日成交额环比与量价形态（放量上涨/缩量上涨等）及 `divergence` 判定，与两市成交额环比印证；前 75 中异常放量个股标注。
   - **归因**：领涨/领跌板块 + 板块内涨停家数做**定性**归因（`market_boards` 无板块权重，算不了指数点数的精确贡献，只做定性）；资讯佐证按"资讯引用约定"随文标注。
7. **龙虎榜资金**：`lhb_basic` 净买卖前列、`lhb_stock_statistic` 近三月常客、`lhb_yyb_capital` / `lhb_yyb_most` 席位动向；数据缺失则标注，不强行展开。

各表字段名以 `query_board_data` / `query_snapshot` 实际返回为准。

## 结论 payload 契约

payload 结构由本 skill 约定（server 不校验，见 ADR 0003），建议字段：

- `mode`、`report_path`（必填；报告落盘路径）
- `overall_stance`（总体立场一句话）、`core_logic`（核心逻辑）
- `indices` / `breadth` / `sectors` / `lhb_capital`（对应内容框架第一/二/三/七块，缺失块置 null 并在 `data_gaps` 标注）
- `limit_up_structure`（第四块）：close 模式为 `limit_up.close` 结论的摘要与关键数字（`summary`/`ladder`/`promote_rate`，原样引用）；midday 模式为 `zt_pool` 降级统计（涨停/跌停/炸板家数、连板高度、炸板率）
- `limit_up_ref`（对 limit_up.close 结论键的引用）：close 模式为 `{"conclusion_type": "limit_up.close", "trade_date": <yyyymmdd>, "summary": <原样引用>}`；midday 模式或子模块未跑/失败时置 null 并在 `data_gaps` 标注
- `top75_structure`（第五块）：`{"clusters": [{"板块", "家数", "合计成交额"}], "coverage": <可归属家数/75>, "index_influence": <热门股对指数/板块影响要点>}`；快照缺失时置 null 并入 `data_gaps`
- `attribution_vol_price`（第六块）：`{"index_corr": {"window_20": <矩阵或摘要>, "window_60": <矩阵或摘要>}, "market_vol_price": {"price_chg", "amount_chg", "vol_price", "divergence"}, "abnormal_amount": <异常放量代码列表>, "attribution": <定性归因要点>}`；各子项缺失置 null 并入 `data_gaps`
- `risk_alerts`（风险提示）
- `data_gaps`（缺失块/子项列表，无缺失为空数组）

## 资讯引用约定

报告引用资讯为观点佐证（如板块轮动引用相关政策新闻、风险提示引用减持公告），散布在各维度章节，不单独设资讯章节：

- 每条引用注明**标题 + 发布时间 + 来源**；`source` 可空，缺失时标"来源未标注"。
- `url` 可空（研报等条目无原文链接）：有链接附链接，无链接以"标题 + 来源 + 时间"为标识，不虚构链接。
- 资讯只作佐证，不构成数字来源。

## 报告格式

固定章节：标题 `## <yyyymmdd> 每日复盘（收盘）` / `## <yyyymmdd> 每日复盘（午市）`（含日期与时点）→ 总体立场一行 → 一、指数表现 → 二、市场广度与成交 → 三、板块轮动 → 四、涨停/连板分析（close 模式注明"本节数字引自 limit_up.close 结论"；午市为 zt_pool 结构统计）→ 五、成交额前 75 结构 → 六、归因与量价复盘 → 七、龙虎榜资金动向 → 八、风险提示 → 数据缺口（逐条列出缺失维度及其对结论的影响）→ 附录：成交额前 75 完整名单（表格：代码/名称/成交额/涨跌幅/行业归属/与所属指数 20 日相关性，缺失列标"—"）→ 末尾注明数据来源表与快照时点。引用资讯按上节约定随文标注。

## 数据缺失红线

- **数据缺失不编造**：某维度数据缺失时，在结论 `data_gaps` 与报告"数据缺口"章节标注，该维度结论降级为定性描述，不得编造点位、金额或家数。
- **指标只认管道输出**：相关系数矩阵、`price_chg`/`amount_chg`、`vol_price` 形态与 `divergence` 一律取自 indicator-tools 的 corr / vol_price 输出，agent 不得手算；管道返回 `insufficient_data`（如指数历史不足窗口、序列交集过短）时如实标注降级，不得用其他口径凑数。
- **指数历史缺失降级**：某指数 `query_index_daily` 行数不足 20/60 日窗口时，该指数从对应 corr 矩阵剔除并在矩阵旁标注；不足 7 个指数时矩阵按可得子集出，注明覆盖率。
- **前 75 个股序列部分缺失**：单股 `query_daily` 失败或行数不足时，该股从相关性与量价标注中剔除、名单表格对应列标"—"，聚类的"合计成交"仍以快照为准（快照在则名单在），覆盖率在报告中注明。
- **归因只定性**：`market_boards` 无板块权重，指数涨跌归因只做"领涨/领跌板块 + 板块内涨停家数"的定性表述，不得编造板块对指数点数的贡献值。
- **核心数据全缺即中止**：就绪检查核心 section 全部缺失且补抓失败时，中止复盘并向用户报告缺失范围与失败原因；部分缺失（含指数历史、前 75 个股序列）时标注缺失维度后继续。
- **落库失败即中止**：`save_conclusion` 失败时向用户报告失败，不算复盘完成。
