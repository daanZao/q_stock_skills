---
name: daily-review
description: 每日复盘编排（午市/全天），市场级总结。当用户请求每日复盘/收盘复盘/午市复盘时使用；经 MCP 查询做数据就绪检查，agent 生成复盘结论与 markdown 报告，结论经 save_conclusion 落库（daily_review.close / daily_review.midday），报告由本 skill 落盘、路径记入结论 payload；核心数据上游不可达（三态契约）时中止复盘并落异常报告（files/daily_review_<date>_anomaly.md）；采集第四态 planned_absent（已定方向待接线的 section 占位缺席）非故障——不触发中止线、不进异常报告误报、按"计划缺席"降级出报；market_boards 当日缺失（非故障）时板块轮动节整节降级切换申万行业自算口径（query_sw_board_daily）；涨停/连板章节经 limit-up-analysis 子技能，指数历史经 query_index_daily，相关性矩阵、量价判定与偏离值累计经 indicator-tools 管道（corr/vol_price/deviation）计算。
---

# 每日复盘

收盘后（午市或全天）基于当日盘面数据做的市场级总结（术语见 `CONTEXT.md`）。本 skill 为每日复盘编排的 agent skill，**轻脚本、重编排**：编排逻辑在本文件，agent 自身即 LLM，复盘结论与 markdown 报告由 agent 手写生成，不调外部 LLM API。数据一律来自 MCP 工具（盘面/快照经 `query_board_data`、`query_snapshot`、`fetch_board_snapshot`、`fetch_market_snapshot`，指数历史经 `query_index_daily`，资讯经 `mx_search`、`query_news`），不直接访问数据库；相关性矩阵、量价判定与偏离值累计经 indicator-tools 管道（`corr` / `vol_price` / `deviation`，指标目录见 `skills/indicator-tools/SKILL.md`）计算，agent 不得手算这些数字；涨停/连板章节由 limit-up-analysis 子技能承担（见 `skills/limit-up-analysis/SKILL.md`），其晋级率/梯队数字只认其落库结论 `limit_up.close`。结论经 `save_conclusion` 落库（通用结论表，见 ADR 0003 `docs/adr/0003-generic-conclusions-table.md`）；markdown 报告不走 MCP，由本 skill 落盘，路径记入结论 payload。

## 触发与模式

- 由用户手动触发；技能不内置调度，收盘后定时执行的现成配置见末节"调度配置"。
- `mode` 仅两个取值：`close`（默认，收盘后全天复盘）、`midday`（午市盘中复盘，产物须标注午市时点）。
- 结论类型与模式一一对应：`daily_review.close` / `daily_review.midday`；subject 固定为 `subject_type="market"`、`subject_code="_market"`。
- 同日两个模式各自独立成行（业务键含 conclusion_type），同日同模式重复跑为 upsert，不产生重复结论；close 不覆盖 midday。

## 编排流程（按顺序执行）

1. **确定交易日与模式**：先取当前日期时间，判断今日是否处于交易日交易时段——工作日 9:30–15:00（含午间休市）一律视为交易时段；周末与节假日为非交易日。**必须询问用户复盘哪一天，不得静默默认当日**。**若当前处于交易时段（或当日收盘数据尚未就绪），当日复盘不可执行**：明确提示用户只能选前一交易日或更早——盘中执行会拉取未完结的盘中 bar，污染指标与数据库。**收盘前（含以 midday 名义）不得对当日执行 close 内容**；midday 模式仅限午市时段（约 11:30–13:00 前后）对当日的盘中复盘。15:00 后的当日复盘仍须经步骤 2 数据就绪检查兜底。`trade_date` 格式 `yyyymmdd`。
   - **历史模式（`trade_date` < 今日）**：**禁用一切 fetch 步骤**（`fetch_board_snapshot`/`fetch_market_snapshot`/`init_database`/`mx_search` 一律不调；服务端防线（issue #78）只是第二道防线，skill 侧自觉不触发）——历史日期的缺失是既定事实，补抓只会污染或徒劳。就绪检查改为**读采集台账归因**（步骤 2 历史分支）；历史复盘可对缺失维度降级出报（见判定线），绝不补数。
   - **判定线（历史模式）**：核心 section（`market_boards`/`zt_pool`/`strong_stocks`/`market_indices`/`market_snapshot`）经台账归因判为**永久缺失**（未采集/采集失败，非上游无数据）≥ 2 项时，**开工前提示用户确认是否仍出报**（默认继续，用户可否决）；< 2 项直接降级出报，报告显著标注 + `data_gaps` 全列。**第四态 `planned_absent`（计划缺席）不计入本条判定**——计划缺席是已定方向的迁移占位（非故障、非未采集），相关维度按"计划缺席"降级出报即可。
2. **数据就绪检查**：
   - **历史模式分支（`trade_date` < 今日，先读台账后探测）**：逐核心 section 调 `query_collection_log(trade_date=<复盘日>, section=<台账 section 名>)` 归因——台账 section 名用采集词汇（`boards`/`zt_pool`/`strong_stocks`/`indices`/`market_snapshot`，分别对应表 `market_boards`/`zt_pool`/`strong_stocks`/`market_indices`/`market_snapshot`）——`count=0`（无台账行）= **未采集**；末行 `status=error` = **采集失败**；`status=empty` = **上游确认无数据**（正常空数据，走降级规则）；`status=ok` = 就绪；`status=planned_absent` = **计划缺席**（第四态：已定方向待接线的迁移占位，收盘后批每日落占位台账，非故障非未采集）——判定「计划缺席」，对应维度按降级规则出报、不计入"永久缺失"判定线，表述与"未采集/采集失败"严格区分（不得混用）。"未采集/采集失败"表述为"**未采集（非正常空数据）**"并进 `data_gaps`，与"上游确认无数据"严格区分（不得混用）；台账启用日（20261017）之前的日期恒无台账行，归因写"台账启用前，采集情况不可考"，按"未采集"同等进 `data_gaps`（第四态启用前的日期同理：无 planned_absent 行不臆断）。归因后逐表查行数确认（同下方当日模式的探测法，纯查询不 fetch）。**历史模式不调下方补抓/轮询/重试。**
   - 当日模式逐表查行数——
   - 核心 section（任一 `count=0` 即数据不全）：`market_boards` / `zt_pool` / `strong_stocks` 经 `query_board_data(table, trade_date)`；`market_indices` 整表必超结果预算，探测用单 code `query_board_data(table="market_indices", trade_date=<当日>, code="000001")`（`count>0` 即该 section 就绪，7 指数逐个取在步骤 3）；`market_snapshot` 经 `query_snapshot(trade_date=<当日>, aggregate="breadth,amount")` 探测（上涨+下跌+平盘家数 > 0 即就绪，结果同时是第一节广度/成交额数据源，复用不重查）。
   - 可选：`lhb_basic` 经 `query_board_data("lhb_basic", trade_date)`。
   - 核心缺失先补抓后重查（**仅当日模式**；历史模式禁 fetch，见步骤 1）：`fetch_board_snapshot(trade_date, sections=<缺失 section 逗号分隔>)`（sections 取值 `indices,boards,zt_pool,strong_stocks,lhb`）、`fetch_market_snapshot(trade_date)`。
   - **防竞态轮询**：抓取调用超时或报错后不得立即以 `count=0` 判缺失——server 端可能仍在落库（20261014 取证：超时调用约两分钟后同表 0→71 行）。按默认 **5s × 6 次**轮询重查缺失表，期间转正即继续；轮询结束仍为 0 才判缺失，区分"0 行"与"抓取进行中"。
   - **分段重试**：重试只用 `sections` 参数抓失败分段（单段 30s 内可返回），不整把重抓——已落库 section 幂等保留，重抓浪费上游调用。分段重试仍 `status=error` → 按三态中止线处理。
   - **boards 当日缺失判定线**：`market_boards` 经补抓、防竞态轮询与分段重试后仍 `count=0`，且 boards 段非 `status=error`（error 走下方三态中止线）→ 判「boards 当日缺失」，板块轮动节整节切换为申万行业自算口径，见"板块节降级：申万行业自算口径"节。
   - **三态判定与中止线**（三态契约见 ADR 0007）：补抓/分段重试后，任一核心 section `status=error`，或 `fetch_market_snapshot` 返回 `status=error`（`error_kind` 为 `upstream_unreachable` 或 `fetch_failed`）→ 进入"故障中止分支"（见下节），落异常报告、今日无复盘。`no_data_for_date`（上游正常响应但当日无数据，如非交易日/盘后未发布）与 `empty_ok`（查询条件合理的空集）为正常语义非失败，按"数据缺失红线"的降级规则继续，绝不触发中止。
   - **第四态 `planned_absent`（计划缺席，非故障非空数据）**：补抓响应中某 section `status="planned_absent"`（`note` 含缺席原因）= 该 section 已定方向待接线、迁移占位——**不触发中止线、不进异常报告、不按缺失红线降级**：对应维度按"计划缺席"出报（报告"数据就绪台账"节判定列记「计划缺席」、`data_gaps` 注明占位原因与已定方向，不记"未采集/采集失败"）；分段重试**跳过** planned_absent 的 section（重试只会得到同样的占位响应）。当前 planned_absent 清单以 `fetch_board_snapshot` 响应与采集台账（`status=planned_absent` 行）为准。
   - `lhb_basic` 为 0 不阻断（lhb 永不进核心集）：close 模式仅记 warning（龙虎榜盘后发布，可能尚未更新），midday 模式属预期内。lhb 段 `status=error`（三态结构化错误）与"未发布"（`rows:0 + empty_kind=no_data_for_date + note`）可区分：前者记入 `data_gaps`，后者属正常语义。
   - 指数历史（相关性/量价复盘的前置）：7 个主要指数逐个 `query_index_daily(index_code, limit=60)`（000001/399001/399006/000300/000016/000905/000688），`count<60` 的指数标注出来。指数日线属 `init_database` 轻量初始化部分，缺失时可经 `init_database` 补齐后重查；仍缺则归因与量价复盘的相关性维度降级，列入 `data_gaps`，不阻断其余复盘。
   - 资讯搜索落库（可选）：就绪检查后 `mx_search(query=<资讯问句>)` 搜近期资讯并落库 `news_items`——大盘级用缺省 subject（`market`/`_market`，如"今日A股市场要闻"），焦点个股传 `subject_type="stock"` + `subject_code=<6 位代码>`。mx-search 配额与 mx-data **独立计数**（默认各 20 次/日，响应回显 `quota:{skill,used,limit}`），单次返回上限约 15 条（响应内重复条目落库前已按 news_code 去重）；本地配额触顶（`status:"error"`，不调上游）、上游自身日限（code=113）或 `MX_APIKEY`/`PG_DSN` 未配置时跳过本步，资讯缺失记入 `data_gaps`，不阻塞复盘。
3. **读数**：`query_board_data` 取各核心表当日行——`market_indices` **逐 code 取**，7 大指数（000001/000016/000300/000688/000905/399001/399006）各一次 `query_board_data(table="market_indices", trade_date=<当日>, code=<指数代码>)`（整表千余行必超工具结果预算，禁止整表拉；indices 段抓取含 7 指数白名单定点补漏，正常应全命中，缺 code 记入 `data_gaps`、报告对应列标"—"）；`market_boards` / `zt_pool` / `strong_stocks` 整表取，超结果预算时走落盘通道（见下）。`market_snapshot` **不整表拉**（全市场约 5900 行、约 3MB）——广度与成交额走 server 聚合 `query_snapshot(trade_date=<当日>, aggregate="breadth,amount")`（就绪检查已取则复用），成交额前 75 走排序取前 N `query_snapshot(trade_date=<当日>, order_by="amount", limit=75)`（`order` 缺省 desc，行字段与整表一致，约 50KB 内）；聚合/topN 不可用（旧版 server）时走**落盘通道**（stdio JSON-RPC 驱动同一 MCP server 的 tools/call，结果落盘后本地取数，仍走 MCP 工具层、不直连数据库）：
   ```bash
   python skills/daily-review/scripts/mcp_dump.py --tool query_snapshot \
       --params '{"trade_date": "<yyyymmdd>"}' \
       --out /tmp/qstock_<yyyymmdd>/snapshot.json --local-aggregate breadth,amount
   ```
   server 路径缺省 `.venv/bin/qstock-mcp`（`--server` 可覆盖）；广度/成交额取 stdout 聚合，前 75 从落盘 rows 按 `amount` 降序本地取。lhb 各表有数据则一并取（`lhb_basic` / `lhb_stock_statistic` / `lhb_yyb_capital` / `lhb_yyb_most`）。**取数纪律：所有历史序列取数以 `trade_date` 为上限**——`query_daily` 显式传 `end=<trade_date>`（否则 `end` 缺省为执行日，执行日晚于复盘日时会把执行日盘中行带进序列末点）；`query_index_daily` 无 end 参数，取回后按 `≤ trade_date` 截断再喂管道。近期资讯经 `query_news` 读库（按资讯发布时间倒序）：大盘级 `query_news(subject_type="market", subject_code="_market", start=<近 7 日起>, limit=10)`，焦点个股按 `subject_type="stock"` + `subject_code` 同法读取；`count:0`（库内无资讯）属正常，非错误。新增块的取数路径：
   - **指数历史**：就绪检查已逐指数取回 60 根（`query_index_daily(index_code, limit=60)`，rows 按 trade_date 升序，含 OHLCV/amount/turnover_rate/amplitude/change_percent），直接复用，不重复查询。
   - **成交额前 75 名单**：`query_snapshot(trade_date=<当日>, order_by="amount", limit=75)` 排序取前 75（落盘通道备用：从落盘 rows 本地按 `amount` 降序取）；名单（代码/名称/成交额/涨跌幅）即报告附录表格的数据源。
   - **前 75 个股历史序列**：逐股 `query_daily(stock_code, start=<trade_date 前推约 90 自然日>, end=<trade_date>)`（明确接受 75 次调用——热门股成交对其指数/板块影响大，是重要因子）；**历史场景一律用 start/end 显式区间**（`days` 与 `start/end` 互斥，且 `days` 以执行日为锚，历史复盘会带进执行日后的序列）；单股查询失败或行数不足时记入 `data_gaps` 并从相关个股分析中剔除，不中断其余个股。
   - **两市成交额环比基准**：`query_snapshot(<前一交易日>, aggregate="breadth,amount")` 取昨日聚合（前一交易日探测法与 limit-up-analysis 同：自然日回溯，最多 5 个自然日，命中上涨+下跌+平盘家数 > 0 即停）；环比 = 当日 `total_amount` ÷ 昨日 `total_amount` - 1。
   - **板块历史（板块 5/10 日涨跌前置）**：`query_board_data("market_boards", trade_date=<逐日>)` 逐日取回最近 11 个交易日的全部板块行（**不加 code 过滤**，每日一次调用、本地按板块名过滤；前一交易日探测法同上；单日全板块行约 155 行，远低于结果预算）。申万口径行（board_type=industry_l1/industry_l2）经 `backfill --source sw` 历史深至 1999，5/10 日涨跌常态化可算；库内历史不足 11 个交易日时按实际可得日数降级，5/10 日涨跌栏标"—"并入 `data_gaps`。存量东财口径行（industry/concept，source 为 NULL/'push2his'）仍是自初次落库日起逐日积累、初期大面积落"—"属预期——报告板块轮动节只消费申万口径行，东财存量行不参与本节计算。boards 当日缺失的降级日不走本路径，板块历史改由 `query_sw_board_daily` 逐日调用取得（见"板块节降级"节）。
   - **板块估值维度（申万分析报表，T+1）**：`query_board_data("sw_board_valuation", trade_date=<当日>)` 取申万一级/二级板块的换手率/PE/PB/流通市值（万元）/股息率（次晨批 `morning` 落库；当日盘中/晚间复盘时该表当日行尚不存在——T+1 次晨才就绪，收盘版报告不消费本表，次日修订版可用）。
   - **前 75 概念归属（已退役）**：概念板块随东财口径一并退役（issue #118，申万体系无概念指数，THS 概念 ticket 若过闸将重建）——本块不再执行，概念相关栏目（第二节概念强度、第四节概念聚类）整栏删除；存量 market_boards 概念行仅为历史数据，不参与求交。
4. **涨停/连板子模块（close 模式）**：编排调用 limit-up-analysis 技能（其自行做数据就绪检查、promote 管道计算并落库 `limit_up.close`）；随后 `query_conclusions(conclusion_type="limit_up.close", trade_date=<当日>)` 读其 payload，取 `summary`/`ladder`/`promote_rate`/`attribution`/`leaders` 编入复盘。**midday 模式不调用**（该技能仅 close 模式）：涨停章节降级为 `zt_pool` 结构统计（涨停/跌停/炸板家数、连板高度、炸板率（口径见"口径附注"））加 `strong_stocks` 入选理由聚类。
5. **指标管道计算**（输入经临时文件或 heredoc 传 stdin，与 limit-up-analysis 调 promote 同法；退出码语义：0 = ok / insufficient_data，1 = 输入错误，2 = 参数错误）：
   - **指数两两相关性**：7 份 `query_index_daily` 输出组装 corr 输入 `{"status":"ok","series":{<index_code>:{"status":"ok","rows":[...]}}}`；corr 窗口由传入序列长度控制（`--tail` 不适用），按序列尾部切 20 根 / 60 根各喂一次，得 20 日与 60 日两张矩阵（相关性标准口径为涨跌幅序列，两处都用 `--on change_percent`）：
     ```bash
     <corr 输入 JSON> | python skills/indicator-tools/scripts/indicators.py --indicator corr --on change_percent
     ```
   - **个股-所属指数相关性**：前 75 逐股与其所属指数做一次 corr（个股 `query_daily` 输出 + 该指数 `query_index_daily` 输出两个 series，尾部 20 根，同样 `--on change_percent`），识别跟涨指数还是独立行情。所属指数按上市板归属：沪市主板（60 开头）→ 000001、科创板（688 开头）→ 000688、深市主板（00 开头）→ 399001、创业板（30 开头）→ 399006；北交所个股（4/8/9 开头）无对应主要指数，跳过并标注。**板块相关性不做**（板块历史自初次落库日起积累，深度不满足相关性窗口；板块维度只做 5/10 日涨跌，见下）；个股相关性只对所属指数计算。
   - **大盘量价判定**：上证指数（000001）的 `query_index_daily` 输出直接喂 vol_price（其 rows 含 close/amount，与 `query_daily` 输出契约同形）：
     ```bash
     <query_index_daily 输出 JSON> | python skills/indicator-tools/scripts/indicators.py --indicator vol_price
     ```
     取输出末点（当日）的 `price_chg`/`amount_chg`/`vol_price`/`divergence`；与两市成交额环比（当日快照聚合 ÷ 昨日快照聚合 - 1）互相印证。
   - **前 75 异常放量标注**：逐股把 `query_daily` 输出喂 vol_price，取末点 `amount_chg`；`amount_chg ≥ +100%`（成交额翻倍，本 skill 约定口径，报告中注明）的标注为异常放量，列入复盘。
   - **板块 5/10 日涨跌**：对板块轮动节上榜板块，把步骤 3 取回的板块历史行重排为 `query_daily` 契约（`close=latest_price`，OHLCV/amount 同名直取，数据重排不属于指标计算），喂 chg 管道 `--window 5` / `--window 10` 取末点；价格比值与逐日 change_percent 连乘数学等价（实测差 <1e-4），且不受上游涨跌幅四舍五入漂移影响。降级日（自算口径）的 5/10 日涨跌做法见"板块节降级"节。
   - **连板股偏离值（严重异动监控，仅 close 模式）**：对 `limit_up.close` 梯队的连板股逐股组装 deviation 输入——`{"status":"ok","stock":<该股 query_daily 输出>,"benchmark":<所属指数 query_index_daily 输出>}`：
     ```bash
     <deviation 输入 JSON> | python skills/indicator-tools/scripts/indicators.py --indicator deviation --windows 10,30
     ```
     基准指数按上市板归属（同个股-指数相关性规则；北交所个股跳过并标注）；**新股（上市未满窗口 + 5 个交易日，以 query_daily 序列自上市首日起且行数不足判断）传 `--skip-leading 5`**——上市前 5 日无涨跌幅限制，涨跌不计入异动。取末点 `dev10`/`dev30` 对照交易所阈值预警：主板/创业板/科创板 10 日 +100%、30 日 +200%，北交所 10 日 +150%、30 日 +300%（下跌方向：-50%/-70%，北交所 -60%/-75%）；**累计达到阈值的 80% 即标注预警**。"N 日内 M 次同向异常波动"情形不监控（异常波动仅出公告不停牌）。
   - **采信纪律**：相关系数、`price_chg`/`amount_chg`、`vol_price` 形态标签、`divergence`、偏离值累计一律取管道输出，agent 不得手算或改写；`insufficient_data` 处理见"数据缺失红线"。
6. **生成两份产物**：agent 手写结构化结论 JSON 与 markdown 复盘报告（内容框架与报告格式见下）。所有数字必须来自步骤 3–5 的查询与管道结果，绝不编造；归因叙事由 agent 撰写，但每个数字必须锚定 query / corr / vol_price 的输出。
7. **报告落盘**：写入 `skills/daily-review/reports/daily_review_<yyyymmdd>_<mode>.md`（目录不存在则创建）。
8. **结论落库**：`save_conclusion(subject_type="market", subject_code="_market", trade_date=<yyyymmdd>, conclusion_type="daily_review.<mode>", payload=<结论 JSON>)`；payload 必须含 `report_path`（报告相对仓库根的路径）。**返回 `status != "ok"` 即中止复盘**，向用户报告失败原因与已生成产物的位置——不产生"分析了但没存下"的假象，落库成功才算复盘完成。
9. **核验（可选）**：`query_conclusions(subject_type="market", trade_date=<yyyymmdd>, conclusion_type="daily_review.<mode>")` 确认落库行存在且仅一行；重跑时步骤 8 的 `save_conclusion` 返回 `outcome: "updated"`。

## 故障中止分支与异常报告

**触发线**：步骤 2 补抓/分段重试后，任一核心 section（`indices`/`boards`/`zt_pool`/`strong_stocks`）`status=error`，或 `fetch_market_snapshot` 返回 `status=error`（`error_kind` 为 `upstream_unreachable` 或 `fetch_failed`，三态契约见 ADR 0007）。第四态 `planned_absent` **不在触发线内**——计划缺席非故障，失败 section 清单不得收录 planned_absent 的 section（见模板末"计划缺席（非故障）"节）。

**动作（顺序执行）**：

1. **立即中止复盘**：不执行后续步骤，不落库任何结论（无 `daily_review.<mode>`），不写复盘报告——故障场景不降级出报，宁可今日无复盘。
2. **落异常报告** `files/daily_review_<yyyymmdd>_anomaly.md`（仓库根 `files/`，沿用阻塞清单命名），模板如下。
3. **向用户复述要点**：缺哪些 section、三态分类、建议（稍后重试 / 今日无复盘）。

异常报告模板：

````markdown
# <yyyymmdd> 每日复盘异常报告（未产出复盘）

- **日期 / 模式**：<yyyymmdd> / <close|midday>
- **状态**：复盘中止于数据就绪阶段；未落库结论、未产出复盘报告。
- **建议**：<稍后重试（upstream_unreachable，附工具 retry_advice）/ 今日无复盘>

## 失败 section 清单

| section / 表 | 三态分类 | 工具报错 |
|---|---|---|
| <boards> | upstream_unreachable | <工具返回的 error 串> |
| <market_snapshot> | upstream_unreachable | <...> |

## 关键证据

- 工具返回：`error_kind` / `retry_advice` / `attempted_sources` 逐源明细（含逐源 failure_kind）原样摘录，区分单源问题与集群性故障。
- 主机侧可达性复测（在主机执行并附结果）：
  ```bash
  curl -s -m 8 -o /dev/null -w "%{http_code}\n" \
    "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&fltt=2&invt=2&fid=f12&fs=m:1+s:2&fields=f12,f14,f2"
  curl -s -m 8 -o /dev/null -w "%{http_code}\n" "https://quote.eastmoney.com/"
  ```

## 已落库部分

<本次已成功 section 与行数；已落库数据保留，恢复后经 sections 参数分段幂等补抓缺失分段，不必整把重抓>

## 计划缺席（非故障，不触发中止）

<当日 status=planned_absent 的 section 及缺席原因（note 原样摘录）；计划缺席是已定方向待接线的迁移占位，本节仅为信息记录——该类 section 不进入上方"失败 section 清单"，无本节时整个删掉>
````

## 板块节降级：申万行业自算口径（boards 当日缺失）

**触发条件**：步骤 2 中 `market_boards` 当日经补抓、防竞态轮询与分段重试后仍 `count=0`，且 boards 段非 `status=error`（error 走故障中止分支，不降级）→ 板块轮动节**整节切换**为 `query_sw_board_daily` 自算口径。前置依赖：成分字典与行业名称表已导入（`import_board_classification` / `import_sw_industry_meta`）；未导入时 `query_sw_board_daily` 恒 `count:0`，板块节按原缺口规则降级并在 `data_gaps` 注明"自算前置未就绪"。

**切换方式（取数）**：`query_sw_board_daily(trade_date=<当日>, level=1)` 取申万一级行业全表作为板块节主表（行业码/行业名/加权涨跌幅/涨平跌家数/成分成交额/成分数）；需要明细时加调 `level=2`。**概念板块强度栏目随东财口径一并缺失**：整栏删除并入 `data_gaps`（自算口径无概念板块数据）。

**口径标注模板**（板块节开头原样标注，不得省略）：

> 本节为申万行业自算口径（成交额加权）：按申万成分字典 point-in-time 归属 × 库内个股日线聚合（basis=sw_amount_weighted），行业涨跌幅为成分股成交额加权、停牌股（无成交）剔除后归一；与申万官网发布指数口径（market_boards 申万行）不可直接比较，本节数值不与报告内申万官网口径栏目交叉引用。

行内 `adj` 字段为本次实际使用的复权口径（issue #92 起按当日 stock_daily 各 adj 行数动态选择、多者胜平局取 qfq）；`adj ≠ 'qfq'` 时在模板后追加一句"个股日线口径为 adj=<实际值>"（截面涨跌/成交额与复权方式无关，数值不受影响）。顶层 `dict_as_of` 为成分字典最近生效日，明显早于当日（如超过一个季度）时在 `data_gaps` 注明"成分字典陈旧"。

**叙述层断交**（降级日强制）：申万官网口径栏目 = `market_boards` 申万板块涨跌（industry_l1/l2）、涨停/强势股行业聚类（`zt_pool.industry` / `strong_stocks.industry`，东财行业名经成分字典换口径后可用）、前 75 行业聚类。降级日规则：板块节数值不与上述栏目交叉引用、不做涨跌对照；报告内出现两套行业口径时，自算节措辞用"申万一级行业（自算）"，其他节维持申万官网口径原样；总论的板块主线一句话可引用自算口径的领涨/领跌方向，但须带"（申万自算口径）"后缀。

**5/10 日窗口**：对上榜行业逐日调用 `query_sw_board_daily(trade_date=<逐日>, level=1, industry_code=<2 位一级前缀>)`（最近 11 个有效交易日，前一交易日探测法与板块历史同），把逐日 `change_pct` 连乘重排为 `query_daily` 契约的 `close` 净值序列（基数任意，如 1000 起；数据重排不属于指标计算），喂 chg 管道 `--window 5` / `--window 10` 取末点。窗口内某日该行业无行（stock_daily 缺当日）则对应栏标"—"并入 `data_gaps`，不得手算连乘凑数。注意每日权重随当日成交额变动，连乘语义为"日度再平衡的成交额加权收益"，口径标注已涵盖、不另行声明。

**故障日限制**：东财故障当日 `stock_daily` 亦无当日行（快照不落库、baostock 次日才发布个股日线），`query_sw_board_daily` 当日同样无源（`count:0`）——板块节当日仍按既有缺口规则降级出报（标"—"入 `data_gaps`），不因此改走故障中止分支；次晨 baostock 发布后次晨批/`fetch_daily` 把 `stock_daily` 补齐（次晨批写 adj=none 行，issue #92 起本工具自动选用当日占优 adj，无需等 qfq 补抓），可重跑本工具补算并修订报告（报告覆盖落盘、结论 upsert）。本分支的价值场景是**补做历史复盘**与**次日修订**，不改变当日晚上的降级事实。

**停牌表述规则**：`stock_count` 为 point-in-time 归属成分股总数（含停牌占位与当日无行情股），`counted_count` 为参与加权数，差即停牌/无当日行情数。行业行或节注写明"成分 N 只（M 只停牌或无当日行情，剔除）"（N=`stock_count`，M=N−`counted_count`）；`amount` 为全部有当日行成分的成交额合计（停牌行 amount 为 null 天然不贡献）。M>0 时必须显式表述，不得只报加权涨跌幅。

演练产物（合成数据样例，含上述标注模板与停牌表述的实际运用）：`skills/daily-review/examples/daily_review_20261014_board_section_swcalc.md`。

## 复盘内容框架（总体立场 + 六节）

**写作总原则**：总体立场只给结论、论据全部后置；**每个数据点全报告只有一个出处**（如炸板率只在第三节出现，总论与其他节引用结论而不复述数字）；能表格化的不堆文字，没有数据支撑的叙述（如仅凭印象的日内回顾）不写。

0. **总体立场**（≤5 行摘要）：递进结构——大盘一句话 → 分类指数一句话 → 板块主线一句话 → 一句归因推论。涨跌停家数、连板梯队、龙虎榜等细节**不进总论**。
1. **一、指数表现**：`market_indices` 七指数合并单表（收盘点位 / 涨跌幅 / 振幅 / 量比 / 成交额 / **20 日涨跌** / **20 日区间位置**——区间上下沿与今日所处位置，经 `query_index_daily` 60 日序列与 chg/rolling 管道）；**涨跌家数**（全市场及分市场、环比昨日）与**两市成交额及环比**并入本节（`market_snapshot` 聚合）。
2. **二、板块轮动**：`market_boards` 申万双级口径行业表（board_type=industry_l1 一级 31 / industry_l2 二级 124，issue #118 东财口径退役）带**当日 + 5 日 + 10 日涨跌**（chg 管道口径，申万口径行经 `backfill --source sw` 历史深至 1999、常态化可算，见步骤 3"板块历史"）；抓取响应的 `planned_absent_fields` 已机器可读声明申万不发布的三字段（主力资金净流入/量比/领涨股，永久弃用口径）——本节不设对应栏目、不入 `data_gaps`、列恒 NULL 不表述为缺口；**boards 当日缺失时整节切换申万行业自算口径**（`query_sw_board_daily` 一级行业表 + 口径标注 + 叙述层断交，见"板块节降级"节）；**概念板块强度栏目随东财口径一并退役**：整栏删除（自算口径无概念板块数据）。
3. **三、涨停/连板与龙虎榜**：close 模式引用 limit-up-analysis 子技能的 `limit_up.close` 结论——`summary`、连板梯队、晋级率、归因三维、龙头点评原样引用并注明来源；midday 模式降级为 `zt_pool` 结构统计（涨停/跌停/炸板家数、连板高度、炸板率（口径见"口径附注"））；`strong_stocks` 入选理由聚类两种模式均保留。**涨停/跌停/炸板率与封板节奏为全报告唯一出处**。**严重异动监控（仅 close 模式）**：连板股 10/30 日偏离值累计（deviation 管道输出）与临近阈值预警（达交易所阈值 80% 即预警）；**高标躲异动回调时提示观察同概念后排承接**（一两句提示，不展开）。**龙虎榜并入本节**：`lhb_basic` 净买卖前列、`lhb_stock_statistic` 近三月常客、`lhb_yyb_capital` / `lhb_yyb_most` 席位动向，与情绪结构交叉阅读；数据缺失则标注，不强行展开。
4. **四、成交额前 75 结构分析**：口径为 `query_snapshot` 当日快照按成交额（`amount`）降序前 75（`order_by="amount", limit=75`，落盘通道备用，见步骤 3）。**行业聚类分一级/二级两档**——"前 75 中 X 行业占 N 只、合计成交 Y 亿"（行业归属经申万成分字典 `sw_industry_class` point-in-time 归属换口径，一级按行业码前 2 位、二级按前 4 位，与 boards 主口径同源）；**概念强度栏目已随概念板块退役**（同第二节）；结合热门股对其指数的影响因子（成交额占两市比例、经 corr 与所属指数的 20 日相关性）；异常放量、量价背离标注保留。定性分析为主；完整 75 只名单放报告附录表格。无法确认归属的个股单列"归属未确认"，不得凭记忆归类。
5. **五、归因与量价复盘**：
   - **相关性**：7 个主要指数两两相关性矩阵（20 日/60 日，corr 输出），看市场共振/分化；前 75 个股与所属指数的 20 日相关性，识别跟涨指数还是独立行情。
   - **量价**：大盘以上证指数日线喂 vol_price，得当日成交额环比与量价形态（放量上涨/缩量上涨等）及 `divergence` 判定，与两市成交额环比印证；前 75 中异常放量个股标注。
   - **归因**：领涨/领跌板块 + 板块内涨停家数做**定性**归因（`market_boards` 无板块权重，算不了指数点数的精确贡献，只做定性）；资讯佐证按"资讯引用约定"随文标注。本节是总论归因推论的论据所在，展开写。
6. **六、风险提示**：**只写前文未覆盖的增量风险**（政策变量、外部事件、数据异动等）；agent 判断有必要时可补充，但禁止复述前文已述内容（如高位退潮、量价背离等已在对应章节论述的不再重复）。

各表字段名以 `query_board_data` / `query_snapshot` 实际返回为准；金额字段（成交额/市值/龙虎榜金额等）单位统一为**万元**（ADR 0006），报告展示换算（如亿元）按万元起算（÷1e4）。

## 口径附注（W6）

- **振幅口径**：`market_indices.amplitude` 与 `index_daily.amplitude` 字段存在但长期为 null（无来源）。报告中需要振幅时按**派生口径**自算并注明"（派生）"：振幅 =（当日最高 − 当日最低）÷ 昨收 × 100%（经 20261013 创业板指数据交叉验证：派生 4.6885% ≈ 东财口径 4.69%）。字段为 null 属现状，不报错不入 `data_gaps`；派生所需 high/low/pre_close 同行为 null 时该栏标"—"。
- **资金流字段永久弃用**（issue #113，spec #111/#102）：板块/概念维度的主力资金净流入等资金流字段为永久弃用面——已从采集清单删除（无任何抓取/落库路径），报告不设该栏目、`data_gaps` 不列、不降级表述；历史报告中该栏目为弃用前产物。类似弃用面（整 section）从采集清单删除后同样不得出现在任何报告/落库尝试中。
- **申万板块口径与三字段缺席**（issue #118，spec #111）：boards 行业维度主口径为申万官网双级（board_type=industry_l1 一级 31 / industry_l2 二级 124），历史经 `backfill --source sw` 深至 1999；申万不发布的量比/领涨股/资金流三字段按 planned_absent 语义处理——`fetch_board_snapshot` 响应 `planned_absent_fields` 与采集台账 detail 机器可读声明，列恒 NULL，不设栏目、不入 `data_gaps`、不表述为数据缺口；板块估值维度（换手率/PE/PB/流通市值/股息率）为 T+1（次晨批落 `sw_board_valuation`），收盘版报告不消费、次日修订版可用。
- **炸板率**（单点定义）：炸板率 = 炸板家数 ÷（涨停家数 + 炸板家数）。全报告唯一数值出处在涨停/连板节，其他节引用结论不复述。
- **corr 序列末日剔除**：corr 输入序列末日早于 `trade_date` 的（该标的当日无有效数据），剔除该序列并在矩阵旁标注覆盖率（如"4/7"）；与"行数不足 20/60 窗口剔除"同属剔除规则，分别注明原因。

## 结论 payload 契约

payload 结构由本 skill 约定（server 不校验，见 ADR 0003），建议字段：

- `mode`、`report_path`（必填；报告落盘路径）
- `overall_stance`（总体立场一句话）、`core_logic`（核心逻辑）
- `indices`（第一节：指数点位/涨跌幅 + 20 日涨跌/区间位置摘要，**并入涨跌家数与两市成交额及环比**，原 `breadth` 字段不再单列）
- `sectors`（第二节：申万双级行业当日/5 日/10 日涨跌要点；概念强度随概念板块退役恒置 null；降级日（申万自算口径）注明 `basis: "sw_amount_weighted"`，概念强度置 null）
- `limit_up_lhb`（第三节，合并原涨停与龙虎榜字段）：close 模式含 `limit_up.close` 结论的摘要与关键数字（`summary`/`ladder`/`promote_rate`，原样引用）+ `deviation_alerts`（连板股偏离值预警列表：`[{stock_code, dev10, dev30, threshold, ratio}]`，无临近标的为空数组）+ `lhb`（净买卖前列/常客/席位动向摘要）；midday 模式为 `zt_pool` 降级统计，deviation 不做、`deviation_alerts` 置 null
- `limit_up_ref`（对 limit_up.close 结论键的引用）：close 模式为 `{"conclusion_type": "limit_up.close", "trade_date": <yyyymmdd>, "summary": <原样引用>}`；midday 模式或子模块未跑/失败时置 null 并在 `data_gaps` 标注
- `top75_structure`（第四节）：`{"clusters": [{"板块", "家数", "合计成交额"}], "industry_level2": [...], "concept_strength": [...] 或 null, "coverage": <可归属家数/75>, "index_influence": <热门股对指数影响要点>}`；快照缺失时置 null 并入 `data_gaps`
- `attribution_vol_price`（第五节）：`{"index_corr": {"window_20": <矩阵或摘要>, "window_60": <矩阵或摘要>}, "market_vol_price": {"price_chg", "amount_chg", "vol_price", "divergence"}, "abnormal_amount": <异常放量代码列表>, "attribution": <定性归因要点>}`；各子项缺失置 null 并入 `data_gaps`
- `risk_alerts`（第六节：**仅增量风险**，禁止复述前文）
- `data_gaps`（缺失块/子项列表，无缺失为空数组）

## 资讯引用约定

报告引用资讯为观点佐证（如板块轮动引用相关政策新闻、风险提示引用减持公告），散布在各维度章节，不单独设资讯章节：

- 每条引用注明**标题 + 发布时间 + 来源**；`source` 可空，缺失时标"来源未标注"。
- `url` 可空（研报等条目无原文链接）：有链接附链接，无链接以"标题 + 来源 + 时间"为标识，不虚构链接。
- 资讯只作佐证，不构成数字来源。

## 报告格式

固定章节：标题 `## <yyyymmdd> 每日复盘（收盘）` / `## <yyyymmdd> 每日复盘（午市）`（含日期与时点）→ **总体立场**（≤5 行摘要，递进：大盘→分类指数→板块主线→一句归因推论，无论据无细节数字）→ 一、指数表现（七指数合并单表 + 涨跌家数与两市成交额）→ 二、板块轮动（申万一级+二级行业当日/5 日/10 日涨跌；概念强度栏目已退役不设）→ 三、涨停/连板与龙虎榜（close 模式注明"本节梯队与晋级率引自 limit_up.close 结论"；午市为 zt_pool 结构统计；含严重异动监控与龙虎榜资金）→ 四、成交额前 75 结构 → 五、归因与量价复盘 → 六、风险提示（仅增量）→ 数据缺口（逐条列出缺失维度及其对结论的影响）→ **数据就绪台账**（各核心表/section 一行：count / 判定（就绪·未采集·采集失败·上游确认无数据·计划缺席）/ 归因（台账 source、error_kind、planned_absent 原因或"台账启用前不可考"）；历史模式此节为台账归因的落盘记录，当日模式记录探测与补抓结果）→ 附录：成交额前 75 完整名单（表格：代码/名称/成交额/涨跌幅/行业归属/与所属指数 20 日相关性，缺失列标"—"）→ 末尾注明数据来源表与快照时点。引用资讯按上节约定随文标注。**数据点唯一出处**：炸板率/涨跌停家数只在第三节，成交额环比只在第一节，各节引用结论不复述数字。

## 数据缺失红线

- **数据缺失不编造**：某维度数据缺失时，在结论 `data_gaps` 与报告"数据缺口"章节标注，该维度结论降级为定性描述，不得编造点位、金额或家数。
- **指标只认管道输出**：相关系数矩阵、`price_chg`/`amount_chg`、`vol_price` 形态与 `divergence`、板块 5/10 日涨跌（chg）、偏离值累计（deviation）一律取自 indicator-tools 管道输出，agent 不得手算；管道返回 `insufficient_data`（如指数历史不足窗口、序列交集过短）时如实标注降级，不得用其他口径凑数。
- **停牌表述**：`query_daily` 每行带 `tradestatus`/`suspended`/`suspended_basis`（停牌占位行，词汇见 CONTEXT.md）；指标管道入口已自动剔除停牌占位行（剔除明细见响应 `excluded_suspended`），无需也不许手工预处理。报告中个股停牌统一表述为"停牌"（不写"无数据"）；停牌期间序列末点不前的个股（序列末日早于 `trade_date`）按口径附注的 corr 剔除规则处理。
- **板块历史不足降级**：申万口径行（industry_l1/l2）经 `backfill --source sw` 历史深至 1999，可用交易日不足 5/10 日窗口时对应涨跌栏标"—"并入 `data_gaps`，不得硬算（正常回补后不应出现；若出现先查回补台账）。`market_boards` **当日**缺失（非故障，判定线见步骤 2）不按本条降级，板块轮动节整节切换申万自算口径（见"板块节降级"节）；自算口径自身无源（stock_daily 当日亦无行，故障日限制）时才回到本条标"—"。
- **概念栏目降级**：mx-data 概念查询不可行、配额不足或返回不可解析时，概念强度相关栏目整栏删除并入 `data_gaps`，不得凭记忆归概念。
- **指数历史缺失降级**：某指数 `query_index_daily` 行数不足 20/60 日窗口时，该指数从对应 corr 矩阵剔除并在矩阵旁标注；不足 7 个指数时矩阵按可得子集出，注明覆盖率。
- **前 75 个股序列部分缺失**：单股 `query_daily` 失败或行数不足时，该股从相关性与量价标注中剔除、名单表格对应列标"—"，聚类的"合计成交"仍以快照为准（快照在则名单在），覆盖率在报告中注明。
- **归因只定性**：`market_boards` 无板块权重，指数涨跌归因只做"领涨/领跌板块 + 板块内涨停家数"的定性表述，不得编造板块对指数点数的贡献值。
- **故障即中止，空数据才降级**：核心 section 或 `market_snapshot` 抓取 `status=error`（三态分类 `upstream_unreachable` / `fetch_failed`，ADR 0007）时进入故障中止分支——落异常报告、今日无复盘，不得降级出报。本节其余降级规则（标"—"、入 `data_gaps`、维度降级继续）仅适用于**正常空数据**（`no_data_for_date` 上游正常响应但当日无数据、`empty_ok` 查询条件合理的空集），故障场景不适用。
- **落库失败即中止**：`save_conclusion` 失败时向用户报告失败，不算复盘完成。
- **不绕过工具直写数据库**：工具入口失败（含上游脏数据撞键、落库缺陷）时记入 `data_gaps` 并向用户报告缺陷、等待修复后重跑；不得 import 仓库内部函数直连数据库写入——直写会绕过 schema 约束与工具层语义，且让缺陷隐形。

## 调度配置

本技能**不内置调度**；本节是给用户自行安装定时任务的现成配置，按需选用其一。定时触发等价于手动触发 close 模式——交易日判断与数据就绪检查由技能自身流程兜底（核心缺失先补抓后重查、故障即中止，见"编排流程""故障中止分支"与"数据缺失红线"），调度侧无需判断交易日、无需等数据就绪。

### macOS launchd

写入 `~/Library/LaunchAgents/com.qstock.daily-review.plist` 后 `launchctl load ~/Library/LaunchAgents/com.qstock.daily-review.plist` 生效。`StartCalendarInterval` 按工作日（周一至周五）15:45（收盘后）唤起；`ProgramArguments` 为占位符——`kimi -p <提示词>` 或等效 CLI，按实际 agent 唤起方式替换。注意 launchd 不读 shell 配置：`PG_DSN`（及可选 `MX_APIKEY`）需经 plist 的 `EnvironmentVariables` 或包装脚本显式给出。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.qstock.daily-review</string>
    <!-- 占位符：按实际 agent 唤起方式替换，如 kimi -p "执行每日复盘（close 模式）" -->
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/kimi</string>
        <string>-p</string>
        <string>执行每日复盘（close 模式）</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/q_stock_skills</string>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>15</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>15</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>15</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>15</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>15</integer><key>Minute</key><integer>45</integer></dict>
    </array>
</dict>
</plist>
```

### crontab 备选

一行（`crontab -e`），命令同为占位符，按实际唤起方式替换：

```cron
45 15 * * 1-5 cd /path/to/q_stock_skills && /path/to/kimi -p "执行每日复盘（close 模式）"
```
