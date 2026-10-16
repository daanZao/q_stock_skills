# 同花顺板块实时截面接口：字段覆盖与稳定性调研（issue #64）

- **日期**：2026-10-16
- **背景**：为"板块双源冗余采集"（东财主 + 同花顺灾备**异表**）核实 THS 侧**当日全板块截面**采集源。前置调研见 `docs/research/board-hist-backfill-sources.md`（issue #55，d.10jqka 年 K 线历史回补已确认可用）。本票补截面这一半。
- **方法**：读 .venv 内 akshare 1.18.94 源码定位 URL；`.venv/bin/python` 实际调用；curl/requests 直测原始端点。实测脚本在 `.scratch/ths_probe*.py`（未入库），关键输出见末节。
- **重要变更**：东财 push2/push2his 集群故障自 2026-10-14 持续至 10-15，**2026-10-16 10:45 实测已恢复**（`17.push2` clist 返回真实板块数据，`total=496`；push2his kline 200）。灾备需求仍成立（本次故障持续约 2 个交易日），但主源当前可用。

## 结论（推荐）

1. **行业截面有现成批量接口**：akshare `stock_board_industry_summary_ths`（底层 `q.10jqka.com.cn/thshy/index/.../ajax/1/` 分页 HTML），2 个请求拿全 90 个行业，字段含涨跌幅/总成交量/总成交额/净流入/上涨下跌家数/领涨股——**比东财 market_boards 现有实时口径还全**（东财侧 volume/amount 本就为 None）。
2. **概念截面无批量接口**：`q.10jqka.com.cn/gn/` 只有"概念时间表"（日期/概念名称/驱动事件/龙头股/成分股数量），**无涨跌幅等行情列**（实测 `field/199112` 排序页同样只回时间表）。概念截面只能逐板块抓详情页 `board-infos`（约 375 请求/日），字段：今开/昨收/最高/最低/成交量(万手)/板块涨幅/涨跌家数/资金净流入(亿)/成交额(亿)，**无领涨股**。
3. **截面是纯当日的**：无日期参数。历史回补只能靠 d.10jqka 年 K 线（OHLC+成交量+成交额，字段比截面少；且盘中只有 T-1 数据，当日行收盘后才出）。
4. **反爬强度低但属无承诺 web 接口**：实测 v cookie 不带也 200 返回真实数据；akshare 路径依赖 `ths.js`（py_mini_racer 算 hexin-v）。HTML 解析依赖 class 名（`cate_inner`/`board-infos`/`page_info`），比 d.10jqka 静态 JS 文件脆弱。每日一次（2～400 请求）限速 ≤2 req/s + 重试 + 失败台账即可。
5. **THS 灾备表建议字段集**见末节"可承载字段集清单"：行业截面 11 字段每日可采；概念截面 10 字段（需逐板块拼装）；`volume_ratio`、概念领涨股、板块指数点位（latest_price 只能派生）不可采。

## 接口清单（akshare 1.18.94 源码定位）

| 函数 | 源码 | 请求目标 | 形态 | 字段 |
|---|---|---|---|---|
| `stock_board_industry_name_ths` | `stock_feature/stock_board_industry_ths.py:68` | `q.10jqka.com.cn/thshy/detail/code/881272/` 页内 `cate_inner` 链接 | HTML 爬取 | name, code（881xxx） |
| `stock_board_industry_summary_ths` | 同文件 :331 | `q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/{n}/ajax/1/` 分页 | HTML 表格 ×N 页 | 板块/涨跌幅/总成交量/总成交额/净流入/上涨家数/下跌家数/均价/领涨股/领涨股-最新价/领涨股-涨跌幅 |
| `stock_board_industry_index_ths` | 同文件 :121 | `d.10jqka.com.cn/v4/line/bk_{881xxx}/01/{year}.js` | 年 K 线 JS | 日期/开/高/低/收/成交量/成交额 |
| `stock_board_industry_info_ths` | 同文件 :88 | `q.10jqka.com.cn/thshy/detail/code/{code}/` 页内 `board-infos` | HTML 爬取 | 今开/昨收/最低/最高/成交量(万手)/板块涨幅/涨幅排名/涨跌家数/资金净流入(亿)/成交额(亿) |
| `stock_board_concept_name_ths` | `stock_feature/stock_board_concept_ths.py:71` | `q.10jqka.com.cn/gn/detail/code/307822/` + `gn/index/field/addtime/.../page/{n}/` 分页合并 | HTML 爬取 ~39 页 | name, code（30xxxx 外显码） |
| `stock_board_concept_summary_ths` | 同文件 :273 | 同上 gn 时间表 | HTML 表格 | 日期/概念名称/驱动事件/龙头股/成分股数量（**非行情截面**） |
| `stock_board_concept_index_ths` | 同文件 :124 | 详情页 `<input id="clid">` 取内码 → `d.10jqka.com.cn/v4/line/bk_{内码}/01/{year}.js` | 年 K 线 JS | 日期/开/高/低/收/成交量/成交额 |

注：akshare 未封装概念行情截面（因为它在 q.10jqka 上不存在批量形态）；`stock_board_concept_summary_ths` 名字像截面，实际是概念创立时间表，不能当截面用。

## 实测记录（2026-10-16 10:35–10:45，本机，东财故障窗口刚结束）

| 调用 | 结果 | 样本/备注 |
|---|---|---|
| `stock_board_industry_name_ths()` | OK 0.7s | 90 个行业，code 全为 881xxx（如 半导体=881121、白酒=881273） |
| `stock_board_concept_name_ths()` | OK 6.7s（39 页） | 375 个概念，code 为 30xxxx 族（308xxx×104、300xxx×86、301xxx×83、309xxx×73、302/307/306/305/304 少量）。注意：详情页 `涨幅排名 x/390` 显示行情口径约 390 个概念，name_ths 少约 15 个（疑新设概念未进时间表页），清单需以详情页可达性兜底 |
| `stock_board_industry_summary_ths()` | OK 0.3s，90 行（2 页：50+40） | 首行：军工装备 涨跌幅 4.41%、总成交量 792.13、总成交额 176.65、净流入 11.43、上涨 80/下跌 2、领涨股 晟楠科技 24.02%。原始表头带单位：涨跌幅(%)/总成交量（万手）/总成交额（亿元）/净流入（亿元），akshare 重命名后丢单位 |
| `stock_board_concept_summary_ths()` | OK 但仅 50 行 | 概念时间表，非行情；且分页 `read_html` 在末页 `ValueError: No tables found` 提前 break |
| 概念详情页 clid | OK | gn 30xxxx → 指数内码 886xxx（308614→886056、309121→886071）；行业 881121→881121（恒等）。`bk_886073`（铜缆高速连接）年 K 线 200 正常 |
| 详情页 `board-infos`（行业+概念同构） | OK | 今开/昨收/最低/最高/成交量(万手)/板块涨幅/涨幅排名/涨跌家数(涨/跌)/资金净流入(亿)/成交额(亿)。**无最新价点位、无领涨股**（行业领涨股只在 summary 表） |
| `stock_board_industry_index_ths("半导体")` | OK | 数据最新至 2026-10-15（T-1，当日行收盘后才有）；概念 AI手机（886071）同至 10-15 |
| 无 cookie 直取行业截面 | OK | 不带 v cookie 也 200 + 真实数据（page1=50 行、page2=40 行）；akshare 的 hexin-v 当前非强制 |
| 东财复核 | **已恢复** | `17.push2.eastmoney.com/api/qt/clist/get`（fs=m:90+t:2）200，返回真实板块 JSON（total=496）；push2his kline 200 |

## 字段差异表（THS vs 东财 market_boards 口径）

market_boards 列（`src/qstock_mcp/adapters/base.py:77` BOARD_FIELDS）与当前东财实时填充情况（`map_board_rows`，`_board_em.py:54`；volume/amount/OHLC 等本就置 None）对照：

| market_boards 列 | 东财实时现状 | THS 行业截面（summary，2 请求） | THS 概念（board-infos 拼装，~375 请求） | d.10jqka 年 K（历史） |
|---|---|---|---|---|
| board_type / board_name | ✓ | ✓（行业 90） | ✓（概念 ~375–390） | ✓ |
| board_code | BKxxxx | 881xxx（异体系） | 30xxxx 外显 + 886xxx 指数内码（异体系） | 881/885/886 |
| latest_price | ✓ | ✗ 无点位；可派生 = 昨收×(1+涨跌幅)，或收盘后取年 K 当日行 | 同左（board-infos 有昨收+板块涨幅） | ✓ 收盘价（T-1） |
| change_percent | ✓ | ✓ 涨跌幅(%) | ✓ 板块涨幅 | 可派生 |
| change_amount | ✓ | ✗ 无直接字段；可派生 | 同左 | 可派生 |
| volume | None | ✓ 总成交量（万手） | ✓ 成交量（万手） | ✓（股，量纲不同需注意） |
| amount | None | ✓ 总成交额（亿元→落库转万元，ADR 0006） | ✓ 成交额（亿→万元） | ✓（元→万元） |
| amplitude | None | ✗ | 可派生（board-infos 有最高/最低/昨收） | 可派生 |
| high/low/open/pre_close | None | ✗（summary 无；详情页 board-infos 有） | ✓ board-infos 今开/昨收/最高/最低 | ✓ OHLC |
| volume_ratio | None | ✗ | ✗ | ✗ |
| stock_count | None | ≈ 上涨+下跌家数（无平盘家数） | 同左 | ✗ |
| leading_stock / leading_change | ✓ | ✓ 领涨股+最新价+涨跌幅 | ✗ 无（需另抓成分股页排序首行，成本+375 请求） | ✗ |
| 净流入 | （无此列） | ✓ 净流入（亿元）——增量字段 | ✓ 资金净流入（亿） | ✗ |
| 均价 | （无此列） | ✓ | ✗ | ✗ |

要点：

1. THS 截面**反向超出**东财口径：volume/amount/涨跌家数/净流入在东财实时采集里全是 None，THS 每日可采。
2. THS 截面**缺**：板块指数点位（latest_price 只能派生）、volume_ratio、平盘家数、概念领涨股、换手率（market_boards 本来也无换手率列，SNAPSHOT_FIELDS 层面不涉及）。
3. 行业 summary 表无 OHLC；若灾备表要 OHLC，行业也需走详情页（90 请求）或直接等年 K 当日行（T+1 可得，对"每日盘后采集"足够）。

## 代码体系与清单规模

- **行业**：881xxx，90 个，name_ths 一次拿全，稳定。
- **概念**：外显代码 30xxxx 族（308/300/301/309/302/307/306/305/304xxx），name_ths 得 375 个；指数内码（K 线/board 详情 clid）为 885xxx（老概念）/886xxx（新概念），308614→886056。行情口径约 390 个（涨幅排名分母），name_ths 清单少约 15 个——灾备实现应允许"清单外的 code 单点补抓"。
- **与东财 BK 体系映射**：行业名大多与东财一致（半导体/白酒等 90 个级别可人工+程序校对映射表）；概念名存在出入（东财"白酒概念" vs THS 命名习惯不同，且概念集不同构——东财 496 个行业+概念 vs THS 90+390），需映射表+人工兜底，**且按既定决策入独立新表、口径不混排**。

## 历史深度

- 截面接口（summary ajax、board-infos）**无日期参数，只给当日实时**；历史截面（某日全板块涨跌幅/涨跌家数/领涨股）THS 侧无源。
- 历史回补只能靠 d.10jqka 年 K 线（issue #55 已确认）：单板块×单年一个文件，字段仅 日期/OHLC/成交量（股）/成交额（元），**无涨跌幅（可派生）、无涨跌家数、无领涨股、无净流入**；盘中最新只到 T-1。
- 即：灾备表的"截面类字段"（涨跌家数/领涨股/净流入）**只能从启用日起逐日积累，无法回补历史**——与 #55 结论一致（不伪造，历史行这些字段保持 None）。

## 反爬/限流与每日一次稳定性评估

- **鉴权**：akshare 所有 q.10jqka 请求带 `Cookie: v=<hexin-v>`（`ths.js` 经 py_mini_racer 计算）；d.10jqka 年 K 线实测免鉴权（#55）；本次实测**行业截面无 cookie 也返回 200 真实数据**。当前 hexin-v 非强制，但 THS 历史上间歇性强制过，实现应保留 akshare 的 v cookie 路径。
- **限流**：未见明确限频；本次概念清单 39 页连发 + 各端点十余次请求全部 200。每日一次的量级（行业 2 请求；概念全量 ~375 请求）在 ≤2 req/s 限速下约 3 分钟，风险低。
- **脆弱点**：q.10jqka 是 HTML 解析（依赖 `cate_inner`/`board-infos`/`page_info` class 与表格列序），THS 改版即断，比 d.10jqka 静态 JS 脆弱；akshare 的 `read_html` 在空页抛 `ValueError: No tables found`（概念时间表末页即如此），灾备实现自写解析时应容错。
- **建议**：灾备采集器带限速 + 单次重试 + 失败台账（与现有 fetch_chain 三态契约对齐，ADR 0007）；截面字段每日盘后采一次即可满足灾备定位，概念全量拼装（375 请求）若嫌重可只采行业截面 + 概念年 K（T-1 收盘）。

## THS 灾备表可承载字段集（明确清单）

**每日可采（当日截面）**

- 行业（2 请求/日）：`board_name`、`board_code(881xxx)`、`change_percent`、`volume(万手)`、`amount(亿→万元)`、`net_inflow(亿→万元)`、`up_count`、`down_count`、`avg_price`、`leading_stock`、`leading_price`、`leading_change`。
- 概念（~375 请求/日，board-infos 拼装）：`board_name`、`board_code(30xxxx+886xxx 双码)`、`change_percent`、`volume(万手)`、`amount(亿→万元)`、`net_inflow(亿→万元)`、`up_count`、`down_count`、`open`、`pre_close`、`high`、`low`。
- 派生（可选）：`latest_price = pre_close × (1 + change_percent/100)`、`amplitude = (high−low)/pre_close`、`change_amount = pre_close × change_percent/100`。

**只有历史 K 线有（T-1 起，逐日或回补）**

- `open/high/low/close`（行业截面本身无 OHLC，概念有）、`volume(股)`/`amount(元)` 的官方收盘值、`latest_price`=收盘价。

**完全没有（任何 THS 源都无）**

- `volume_ratio`、换手率（板块级）、平盘家数、概念领涨股/领涨股涨跌幅（除非加抓成分股页）、历史日期的涨跌家数/净流入/领涨股截面。

## 源码引用

- `akshare/stock_feature/stock_board_industry_ths.py:68,88,121,331`；`akshare/stock_feature/stock_board_concept_ths.py:71,124,273`
- `src/qstock_mcp/adapters/base.py:77`（BOARD_FIELDS）；`src/qstock_mcp/adapters/_board_em.py:54`（map_board_rows 东财实时口径）
- `docs/research/board-hist-backfill-sources.md`（issue #55，d.10jqka 年 K 线与历史回补结论）
