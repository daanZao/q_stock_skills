# 价格序列依赖面清点：谁在假设"价格是前复权"（issue #144，已作废）

> **作废声明（2026-11-04）**：本报告的分析、结论与落地建议全部作废，不得作为 #158、相关 task、实现或验收的依据。保留本文仅用于追踪历史讨论，不代表其中任何结论有效；后续以 #158 及 2026-11-04 重新形成的研究为准。

2026-10-31，基线 `4ef2b33`（`temp(#134)`）。范围：`src/qstock_mcp`（server / tools_* /
parity_* / repository / sql）、`skills/` 四个 skill（`sepa`、`indicator-tools`、
`daily-review`、`limit-up-analysis`）的 `SKILL.md` 与 `scripts/`、`tests/`（只统计）。

**方法与边界**：纯本地静态阅读 + `grep`，未触网、未连库、未跑测试；未创建分支、
未改动 `src/` 与 `tests/` 任何文件。`qfq` 字面量在 `src/`+`skills/`+`tests/` 共
**108 处命中**，全部逐条归类；命中不等于依赖（取数侧口径参数 ≠ 消费侧假设），
下表只收"读价格 / 定口径"的点。

背景：目标是把个股复权从"库存 qfq 序列"改成"raw（不复权）+ 因子表 → qfq/hfq
派生"。因此"隐含前复权"的判定标准是三条之一：

1. **口径写死**：默认值、DDL 注释、文档措辞把 qfq 当唯一口径；
2. **跨日比较**：把不同交易日的价格放在同一尺度上算（窗口极值、均线、区间收益、
   回撤），只有被复权重写过的序列才自洽；
3. **按 adj 列筛行**：把 `adj` 当存储维度而非派生视图，读写都按它精确匹配。

## 0. 结论摘要

"处" = 下表一行（同一判定单元；一行可覆盖同一文件的多行或一组用例）。共 **58 行**。

| 类别 | 处数 | 说明 |
| --- | --- | --- |
| **必须改** | 38 | 不改则改造后出现错误数值、永久性重抓/漏采，或契约文本与实现不一致 |
| **待定** | 11 | 取决于派生时点（读时派生 vs 物化派生）与派生列口径决策 |
| **无影响** | 9 | 单日截面、指数、板块点位、非价格表，天然与复权无关 |

其中"必须改"里 5 处只是 `SKILL.md` 措辞（标为「需改（文档）」），代码不动。

最重的三处：`repository._SELECT_SQL`（唯一的读数入口，按 `adj` 精确匹配）、
`tools_strong._window_max_close`（60 日窗口内 `MAX(close)`，跨日比较的典型）、
`skills/sepa/SKILL.md:14`（唯一在契约文本里明写"adj 用默认 qfq"的地方）。

## 1. 库层与仓储层

| 文件:行 | 读的列/用的结构 | 隐含前复权？ | 改造影响 | 备注 |
| --- | --- | --- | --- | --- |
| `src/qstock_mcp/sql/001_stock_daily.sql:1` | DDL 注释「个股日线（前复权 OHLCV）」 | 是（明文） | 需改 | 表语义被注释钉死为单一口径；raw 化后应改成「不复权 OHLCV + 因子派生」 |
| `src/qstock_mcp/sql/001_stock_daily.sql:17` | `adj text NOT NULL DEFAULT 'qfq'` | 是 | 需改 | 任何未显式给 `adj` 的 INSERT 静默获得 qfq 标签（现状两条 upsert 都显式传值，但这个默认值是"qfq 是常态"的编码） |
| `src/qstock_mcp/sql/001_stock_daily.sql:21` | `UNIQUE (stock_code, trade_date, adj)` | 是（间接） | 需改 | 把"同一日多口径并存"写进主键；raw 为唯一真相后业务键应退化为 `(stock_code, trade_date)`，因子另表 |
| `src/qstock_mcp/repository.py:45-49` | `_SELECT_SQL`：`WHERE stock_code=%s AND adj=%s AND trade_date BETWEEN ...` | 是 | 需改 | **唯一的价格读出口**；派生方案无论读时算还是物化，都必须先改这条 SQL |
| `src/qstock_mcp/repository.py:101-108` | `select_daily(conn, code, adj, start, end)` | 是 | 需改 | `query_daily` 的全部行数据来自此函数；签名里的 `adj` 是"取哪个存储版本"，派生后要么去掉、要么改为"派生哪个口径" |
| `src/qstock_mcp/repository.py:111-118` | `select_dates(... adj ...)` | 是 | 需改 | 自愈缺口判定的依据：只认 `adj=qfq` 行。raw 落库后会把"raw 有、qfq 无"判成缺口，**反复重抓同一区间**（运维级风险，不只是数值错） |
| `src/qstock_mcp/repository.py:1159-1163` | `select_latest_daily_date`：`SELECT max(trade_date) FROM stock_daily`（注释明确"不限 adj"） | 否 | 无需 | issue #92 已解耦；raw 化后行为只会更自然 |
| `src/qstock_mcp/repository.py:1166-1182` | `select_dominant_adj`：按当日各 `adj` 行数多者胜、平局取 `qfq` | 是 | 需改 | **口径选择逻辑本身**。raw+因子后"当日占优口径"这个概念不存在（只剩一个口径）；两个调用方（`tools_sw_board`、`tools_strong`）都靠它 |
| `src/qstock_mcp/repository.py:1196-1234`（`:1206-1215`、`:1223`） | `_SW_BOARD_AGG_SQL`：`SUM(change_percent*amount)/SUM(amount)`、`LEFT JOIN stock_daily d ON ... AND d.adj=%s` | 否（口径）／是（join 键） | 待定 | 加权用的是 `change_percent`，与复权方式确实无关——**前提是 `change_percent` 仍取上游按除权前收盘算的值**；若派生时用 raw close 自算 `change_percent`，除权日会出现假跌，这条前提就破了（见 §6.2）。join 的 `adj=%s` 需随 §1 一起改 |

## 2. 工具层（MCP 面 + 核心层）

| 文件:行 | 读的列/用的结构 | 隐含前复权？ | 改造影响 | 备注 |
| --- | --- | --- | --- | --- |
| `src/qstock_mcp/server.py:42`、`:47` | `fetch_daily(adj: str = "qfq")` + docstring「adj: qfq/hfq/none」 | 是 | 需改 | MCP 契约面；默认值即口径承诺，18 个工具里 2 个带价格口径参数 |
| `src/qstock_mcp/server.py:58-60` | `query_daily(adj: str = "qfq")` | 是 | 需改 | 唯一的价格序列出口，`sepa`/`indicator-tools` 全链路默认走这里 |
| `src/qstock_mcp/server.py:86-99` | `query_sw_board_daily` 不暴露 `adj`，内部用当日占优口径 | 是（间接） | 需改 | 隐藏口径：调用方无法选，只能事后从响应顶层 `adj` 读；派生后这里要明确「本工具用的是派生 qfq 口径」 |
| `src/qstock_mcp/tools_daily.py:226` | `fetch_daily(..., adj: str = "qfq")` | 是 | 需改 | 与 server 同源，核心层默认值 |
| `src/qstock_mcp/tools_daily.py:267` | `query_daily(..., adj: str = "qfq")` | 是 | 需改 | 同上 |
| `src/qstock_mcp/tools_daily.py:31-66`（`:35`、`:47`、`:53`、`:65`） | `ensure_coverage(conn, adapters, code, adj, ...)`：抓取、落库、读回全按 `adj` 贯穿 | 是 | 需改 | 抓取口径 = 落库标签 = 读取口径，三合一。raw 化后抓取永远取 raw，派生发生在写入之后（或读取时） |
| `src/qstock_mcp/tools_daily.py:215` | `select_daily(conn, stock_code, adj, ...)` | 是 | 需改 | 与 §1 的 `_SELECT_SQL` 同一问题 |
| `src/qstock_mcp/tools_strong.py:61-63` | `_window_max_close`：`SELECT stock_code, MAX(close) FROM stock_daily WHERE adj=%s AND trade_date BETWEEN ...` | 是（最重） | 需改 | **60 日新高的跨日价格比较**。qfq 序列被历史除权重写，窗口内同一尺度才成立；换成 raw 后除权日前后不可比，会同时产生伪新高与漏选 |
| `src/qstock_mcp/tools_strong.py:46-50` | `_today_rows`：当日 `close/change_percent/volume/amount/turnover_rate`（`adj=当日占优`） | 是 | 需改 | 当日截面的 `close` 会写进 `strong_stocks.latest_price`，与 60 日窗口必须同口径 |
| `src/qstock_mcp/tools_strong.py:111-120` | `adj = select_dominant_adj(trade_date)` 一次选定，同时喂当日截面与 60 日窗口 | 是 | 需改 | 潜在静默失效：若当日占优口径是 `none`（次晨批场景）而窗口里只有 qfq 行，`MAX(close)` 拿到空集 → `is_high` 恒 False → **当天强势股整批漏选且不报错** |
| `src/qstock_mcp/tools_sw_board.py:86-91` | `adj = select_dominant_adj(...)` → `select_sw_board_daily(..., adj)` | 是（间接） | 需改 | 与 §1 `select_dominant_adj` 同源；输出顶层回带 `adj` |
| `src/qstock_mcp/tools_init.py:211` | 全量回溯 `ensure_coverage(conn, adapters, code, "qfq", FULL_HISTORY_START, today)` | 是 | 需改 | **历史数据的唯一来源**写死 qfq；raw 化后这里必须改成 raw，并接上因子回补（否则历史全是"派生口径的旧数据"） |
| `src/qstock_mcp/tools_coverage.py:39`、`:58` | `verify_history_coverage(..., adj: str = "qfq")` → `_dates(..., adj)` | 是 | 需改 | 覆盖核验默认只数 qfq 行：raw 落库后**全市场被判 gap**（误报），且 `--adj` 未暴露给调用方，排障会误判数据缺失 |
| `src/qstock_mcp/tools_morning.py:52-70` | 次晨批 `fetch_astock_daily` → `map_astock_daily_rows`（`adjustflag`→`adj`） | 否 | 无需 | 天然带口径，且是现存唯一的 `adj=none`（raw）行来源，改造后正好是主干 |
| `src/qstock_mcp/adapters/baostock_adapter.py:23`、`:110` | `_ADJUSTFLAG = {"qfq":"2","hfq":"1","none":"3"}` / 反向映射 | 否 | 无需 | 只是协议映射，不带默认假设 |

## 3. 取数回落、parity 与 CLI

| 文件:行 | 读的列/用的结构 | 隐含前复权？ | 改造影响 | 备注 |
| --- | --- | --- | --- | --- |
| `src/qstock_mcp/adapters/tencent_adapter.py:113-119`、`:388` + `src/qstock_mcp/adapters/_daily_common.py:11` | 请求 `qfqday` 缺失时回落 `day`，把 raw 行**标成 `adj=qfq` 落库**，仅置 `adjust_fallback` 标记 | 是（标签失真） | 需改 | 这是现存的"raw 冒充 qfq"通道（ADR 0010 承认"响应无法区分"）。raw+因子模型正好消灭它：raw 本来就该是唯一落库口径 |
| `src/qstock_mcp/adapters/base.py:278`（另有 sina `:202`、tencent `:370`、baostock `:152`、akshare `:30`） | 适配器签名 `fetch_daily(..., adj: str = "qfq")` | 是 | 待定 | 若上游仍保留"直接取 qfq"的能力，接口可不动；若统一只取 raw，签名要比改 |
| `src/qstock_mcp/daily_parity.py:27-36`、`:46-49` | 价格绝对容差 `0.011`（注释：qfq 两位小数口径）、`hfq` 跳过 OHLC 与 `change_percent` | 是 | 待定 | 改造后对拍分两层：raw↔多源 raw 全字段（容差可直接沿用），派生 qfq↔上游 qfq 需另设容差（派生舍入 vs 上游舍入） |
| `src/qstock_mcp/daily_parity.py:129`、`:144`、`:170` | `compare_daily_sources(..., adj: str = "qfq")`、`adj not in ("qfq","hfq","none")`、`if adj == "hfq": note` | 是 | 需改 | 对拍入口默认口径；`hfq` 基准因源而异这条结论在派生模型下反而可修（同因子表 → 基准一致） |
| `src/qstock_mcp/fetch_chain.py:134`、`:144` | `fetch_daily_with_fallback(..., adj="qfq")`、`a.fetch_daily(code, start, end, adj)` | 是 | 待定 | 链本身对口径透明，只透传；改造点在"链要不要传 qfq" |
| `src/qstock_mcp/scheduler.py:382`、`:848-849`、`:961` | `cmd_daily(adj="qfq")`、`--adj` 缺省 `qfq`（help「复权口径，缺省 qfq」） | 是 | 需改 | CLI 固定入口的默认承诺；全市场回溯/补采都从这里进 |
| `src/qstock_mcp/migrate_volume.py:26`、`:50-61` | `classify_unit`：`amount×1e4÷volume ÷ close` 反推单价，比值 ≈1 判股、≈100 判手 | 是（间接） | 待定 | 隐含"close 与当日 VWAP 同尺度"，只有 `adj≈1`（近年无除权）才成立；重除权股的历史行会被判 `anomaly`。raw 化后这条机检反而变准，属"改造顺带修好" |
| `src/qstock_mcp/index_parity.py` / `snapshot_parity.py` / `sw_board_parity.py` / `stock_list_parity.py` | 指数点位、快照最新价、板块点位、清单 | 否 | 无需 | 均无 `adj` 概念（`sql/010` 明确"指数无复权概念，无 adj 列"），`sw_board_parity` 的自算腿只比 `change_pct`/家数 |

## 4. skill 层（口径约定 + 真正算数的代码）

| 文件:行 | 读的列/用的结构 | 隐含前复权？ | 改造影响 | 备注 |
| --- | --- | --- | --- | --- |
| `skills/sepa/SKILL.md:14` | 「`query_daily(stock_code, days=400)`（adj 用默认 `qfq` 前复权…）」 | 是（明文） | 需改 | **全仓唯一把 qfq 写进 skill 契约的地方**。改造后应改为"默认派生 qfq（raw×因子）"，语义不变、来源变 |
| `skills/sepa/SKILL.md:18` | `rolling_max/rolling_min --window 250`（52 周高/低） | 是 | 需改（文档） | 窗口极值必须同口径；数字由 `indicators.py` 算，代码不改，措辞要改 |
| `skills/sepa/SKILL.md:19` | `maxdd --window 250`（最大回撤） | 是 | 需改（文档） | 回撤 = 价格/窗口内最高-1，跨除权日只有 qfq 自洽 |
| `skills/sepa/SKILL.md:20` | `chg --window 250`（250 日涨幅 → RS 近似） | 是 | 需改（文档） | 区间收益同理；是 RS 评分的唯一输入 |
| `skills/sepa/SKILL.md:44` | 趋势模板第 6/7 条：现价 ≥ 52 周低点×1.3、≥ 52 周高点×0.75 | 是 | 需改（文档） | 现价与 52 周极值必须同一口径，否则铁律判定全歪 |
| `skills/sepa/SKILL.md:22` | `diff1 --on ma200`（200 日线方向） | 否（尺度不变） | 无需 | 逐日差分，等比缩放不改符号；只要序列是同一口径即可，qfq/raw 都能用 |
| `skills/indicator-tools/SKILL.md:32` | 输入契约示例 `"params": {..., "adj": "qfq", ...}` | 是（契约） | 待定 | 该 JSON 结构是 ADR 0001 的跨仓库稳定契约；`adj` 已有回显位，需明确"行内价格口径由 `params.adj` 声明"（目前 rows 不带到行级） |
| `skills/indicator-tools/SKILL.md:53`、`:86-97` | 「rolling/maxdd/chg 默认窗口 250」、各指标计算方法 | 是（隐式） | 需改（文档） | 计算方法本身口径无关，但"52 周高/低""区间收益"的语义依赖传入序列 |
| `skills/indicator-tools/scripts/indicators.py:141-165`、`:202-231` | `calc_macd`/`calc_boll`/`calc_rsi`/`calc_vol_price`（`price_chg=(close/前收-1)*100`） | 是（隐式） | 无需 | **代码对口径完全透明**：只吃 `rows[].close`，不读 `adj`、不查库。喂 raw 才出错，责任在喂的那一侧 |
| `skills/daily-review/SKILL.md:46` | 前 75 逐股 `query_daily(stock_code, start=…90 自然日, end=…)`（不带 `adj` → 默认 qfq） | 是 | 需改 | 75 次调用全靠默认值；派生后要么保持默认语义（推荐），要么此处显式传参 |
| `skills/daily-review/SKILL.md:63` | 逐股喂 `vol_price`，`price_chg` 来自 close 环比 | 是 | 待定 | 只取末点 `amount_chg`（量），`price_chg` 不参与判定 → 影响小；但除权日的 `vol_price` 形态标签会错 |
| `skills/daily-review/SKILL.md:142` | 「行内 `adj` 字段为本次实际使用的复权口径（…多者胜平局取 qfq）；`adj ≠ 'qfq'` 时追加口径句」 | 是 | 需改 | 依赖 `select_dominant_adj` 的整个叙事；派生后"当日占优口径"消失，这段模板要重写 |
| `skills/daily-review/SKILL.md:148` | 「次晨批写 adj=none 行，issue #92 起本工具自动选用当日占优 adj，无需等 qfq 补抓」 | 是 | 需改 | 同上；且这句正是 raw 行已进库的证据 |
| `skills/daily-review/SKILL.md:64`、`:146` | 板块 5/10 日涨跌：`close=latest_price` 重排喂 `chg`；降级日 `change_pct` 连乘 | 否 | 无需 | 板块点位/板块涨跌幅是板块自身口径，无个股复权概念 |
| `skills/daily-review/scripts/mcp_dump.py:51-63` | 广度：`latest_price` vs `pre_close` 分桶 | 否 | 无需 | 单日快照，上游 `pre_close` 已按除权调整，与库存序列无关 |
| `skills/limit-up-analysis/SKILL.md:23` | strong_stocks 自算依赖 `stock_daily` 当日行（"先补日线再重跑"） | 是（间接） | 待定 | 只影响就绪检查措辞与重跑指引，不含价格计算 |
| `skills/limit-up-analysis/SKILL.md:83` | 停牌占位行经 `query_daily` 进入管道、由管道剔除 | 否 | 无需 | 剔除判据是 `suspended`/OHLC 全等，等比缩放后依然全等 |

## 5. tests（只统计，不修改）

判据：用 `ast` 逐个 test 函数体查 `qfq` 字面量（脚本见 §7），得 **28 个用例 / 10 个文件**。

| 文件:行 | 读的列/用的结构 | 隐含前复权？ | 改造影响 | 备注 |
| --- | --- | --- | --- | --- |
| `tests/test_sw_board_daily.py:243-293` | `test_adj_dominant_none` / `test_adj_dominant_qfq` / `test_adj_tie_prefers_qfq`（3 例） | 是 | 需改 | 直接测"占优口径选择"，概念消失后整组作废（预期改为"派生口径一致性"） |
| `tests/test_zt_pool_ths.py:205-216`、`:251-293` | `_seed_daily` 恒写 `adj="qfq"`；`TestStrongSelfCalc` 60 日新高 4 例（1 例含 qfq 字面量） | 是 | 需改 | 60 日新高的唯一回归测试，种子数据是 qfq，raw 化后必然红 |
| `tests/test_repository.py:30-73` | 5 例：`upsert_daily(..., "qfq", ...)` + `select_daily/select_dates(..., "qfq", ...)` | 是 | 需改 | 业务键与读数契约的基线测试 |
| `tests/test_fetch_daily.py:25`、`:50`、`:76-218` | 6 例：期望 `params.adj == "qfq"`、种子写 qfq | 是 | 需改 | 含响应契约断言（`:50`） |
| `tests/test_query_daily.py:50-59` | 2 例：自愈幂等/中段缺口，断言按 qfq 读 | 是 | 需改 | 与 `select_dates` 的 adj 判定同命运 |
| `tests/test_tradestatus.py:78-83` | 1 例：迁移保留数据，插 qfq 行 | 是 | 需改 | 停牌列语义，价格口径只是顺带 |
| `tests/test_tencent_adapter.py:1-25 处` | 10 例：`qfqday`/`day` 响应键解析、回落识别 | 是 | 待定 | 取数面接口不变则全部保留；只取 raw 后回落分支可整体删除 |
| `tests/test_sina_adapter.py:116` | 1 例：Sina 适配器多取天数并过滤 | 是 | 待定 | 同上 |
| `tests/fakes.py:71`、`:429`、`tests/test_live_sample.py:41`、`tests/test_migrate_volume.py:54`、`skills/indicator-tools/tests/test_indicators.py:1 处` | fake 适配器默认参数、种子 INSERT 写 `'qfq'`、契约样例含 `adj` | 是 | 待定 | 无 qfq 断言，只做数据构造；随业务键调整而改 |

## 6. 三条结论

### 6.1 必须改（38 处 = 8 + 4 + 26）

按"不改就一定出错"分三类：

**A. 读数与写数键（8 处）**：`_SELECT_SQL`（repository:45-49）、`select_daily`
（:101）、`select_dates`（:111）、`select_dominant_adj`（:1166）、`ensure_coverage`
的 adj 贯穿（tools_daily:31-66）、`tools_daily:215`、`_init_backfill` 硬编码 `"qfq"`
（tools_init:211）、`verify_history_coverage` 默认 `adj="qfq"`（tools_coverage:39,58）。
其中 `select_dates` / `verify_history_coverage` 是**运维级故障点**而非数值问题：raw
落库后它们会把已有数据判成缺口，触发无限重抓与"覆盖率为 0"误报。

**B. 跨日价格比较（4 处）**：`_window_max_close`（tools_strong:61-63）、`_today_rows`
（:46-50）、`compute_strong_rows` 的选口径（:111-120）、`tools_sw_board:86-91`。
`tools_strong` 那条最危险：口径不匹配时是**静默漏选**（空集 → `is_high` 恒 False），
不报错、不落 anomaly。板块聚合 SQL 的 `adj` join（repository:1223）同类但列在"待定"
（该 SQL 的口径问题在 `change_percent`，见 6.2）。

**C. 契约与默认值（26 处）**：`sql/001` 三处（注释/DEFAULT/UNIQUE）、server 三处
（两处签名 + `query_sw_board_daily` 隐藏口径）、tools_daily 两处签名、`daily_parity`
一处默认值、scheduler 一处、tencent `adjust_fallback` 通道、`SKILL.md` 九处
（sepa 14/18/19/20/44、indicator-tools 53、daily-review 46/142/148）、`tests/` 六行
（§5 前六行）。这些不改不会立刻算错，但契约文本与实际口径不一致，下一个消费方会
按旧文本写代码。

### 6.2 可延后（11 处）

- **`change_percent` / `amplitude` / `change_amount` 的重算口径**（`_SW_BOARD_AGG_SQL`、
  `daily_parity`、`vol_price`）：这三个是上游派生列。`sw_board` 加权"与复权方式无关"
  成立的前提是**这些列继续取上游值**（上游按除权前收盘算）。若派生时用 raw close
  自算 `change_percent`，除权日会出现约 -X% 的假跌，板块加权、涨跌家数、`vol_price`
  形态标签会同时失真。建议：`change_percent` 继续透传上游，自算只用于校验。
  `daily-review:63` 的 `vol_price` 只采信末点 `amount_chg`（量），`price_chg` 不参与
  判定 → 影响面同样限于形态标签。
- **`daily_parity` 容差与 hfq 结论**：派生模型下"各源 hfq 基准不同"可以被修（同因子表
  → 同基准），但那是收益不是前提，可排在主改造之后。
- **`migrate_volume` 机检**：raw 化后自动变准，无需为它专门排期。
- **适配器 `adj` 参数与 `adjust_fallback` 通道**：只要上游仍允许"直接取 qfq"，适配器
  接口可以不动；`adjust_fallback` 可在 raw 成为唯一落库口径后再删。
- **`indicator-tools` 契约文本**、**`limit-up-analysis` 就绪检查措辞**、**tests 的
  fake/种子数据**、**`fetch_chain` / `adapters.base` 的 `adj` 形参**：随主改造一起改即可，
  没有独立风险。

### 6.3 无影响（9 处）

- **单日截面**：`market_snapshot`（`latest_price`/`pre_close`）与 `mcp_dump.py` 的广度
  分桶；`_mark_suspension` 的 OHLC 全等启发式（等比缩放后仍全等）。
- **指数**：`index_daily`、`query_index_daily`、`index_parity`（`sql/010:2` 明确"指数无
  复权概念，无 adj 列"）。
- **板块点位与板块涨跌幅**：`market_boards.latest_price`、`query_sw_board_daily` 的
  `change_pct` 连乘口径（daily-review:64、:146）。
- **非价格表**：`zt_pool`（含"近 10 日多次涨停"的家数统计——只用代码与连板数）、
  `lhb_*`、`strong_stocks` 的落库行（`latest_price` 只是透传当日 close，不做跨日比较）。
- **口径透明的工具**：`indicators.py` 全部指标代码、`corr`/`deviation`（吃
  `change_percent`）、`sepa` 的 `diff1 --on ma200`（差分符号尺度不变）。
- **写入口径自带**：`tools_morning:52-70` + `baostock_adapter:23,110`（`adjustflag`
  映射成 `adj`，落的就是 `none`）；`select_latest_daily_date`（已不限 `adj`）；
  `limit-up-analysis:83` 的停牌占位行剔除（`suspended`/OHLC 全等判据与口径无关）。

9 处无需改的清单即 §1-§5 表中标「无需」的 9 行。

## 7. 改造要点（给后续实现 issue 的输入）

1. **派生时点二选一**：读时派生（`select_daily` 内 join 因子表，行级带 `adj`）或物化
   派生（新表/新列）。前者不改存储契约、但要动 `_SELECT_SQL`；后者要处理 `adj` 列的
   去留与 `UNIQUE` 键。**两种都必须先改 §6.1-A 的 8 处**，其中 `select_dates` /
   `verify_history_coverage` 若不改会持续误判缺口。
2. **响应契约**：`query_daily` 现在只在 `params.adj` 回显口径，`rows` 不带行级口径。
   建议派生后在顶层加 `price_basis`（如 `{"adj":"qfq","factor_source":"..."}`），
   避免消费方拿 raw 行当 qfq 用——这正是 `adjust_fallback` 已经踩过的坑。
3. **60 日新高必须先解决**：`tools_strong` 的口径不匹配是静默漏选，且它是
   `strong_stocks` 的唯一来源，会被 `daily-review`→`limit-up-analysis` 直接消费。
4. **对拍分两层**：raw↔多源 raw（可沿用现有容差）+ 派生 qfq↔上游 qfq（新容差）。
   `daily_parity` 的 hfq 特例可在派生模型下收敛。
5. **`change_percent` 保持上游透传**（见 §6.2），否则板块加权与涨跌家数会连锁失真。

## 8. 未覆盖与后续

- `files/`（历史运行记录）、`docs/`（`knowledge-graph.md:102/106/107` 三处、
  `adr/0004:15`「全量回溯固定前复权」、`adr/0010:43-50`、`daily-source-cli.md:56`）
  属于**契约文档层**，与上表 §6.1-C 同性质，需要在实现 issue 里一并改；本文未逐行列表。
- 未执行任何测试与 SQL（本次为静态清点），`tests/` 统计口径是"函数体内含 `qfq`
  字面量"，`test_zt_pool_ths.py` 的 60 日新高组里只有 1 例含字面量，实际受影响
  用例数高于统计值。
- 附：命令证据
  ```bash
  grep -rn "qfq" --include=*.py --include=*.sql --include=*.md src/ skills/ tests/ | wc -l   # 108
  grep -rn "stock_daily" --include=*.py --include=*.sql src/                                  # 读数面全集
  grep -rn "adj: str = \"qfq\"" src/qstock_mcp/ src/qstock_mcp/adapters/                       # 默认值全集
  # tests 统计：ast 遍历 FunctionDef，取含 'qfq' 的 test_* 函数体 → 28 例 / 10 文件
  ```
