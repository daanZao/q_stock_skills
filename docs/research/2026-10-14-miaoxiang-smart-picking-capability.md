# 妙想「智能选股」能力调研：mx-xuangu skill 的发现与输入/输出契约

日期：2026-10-14

**调研问题**：用户想用「给一批股票 + 一组选股条件 → 输出满足条件的子集」的能力（条件示例：52 周最高价的 70% 以上、站上 30 周线或 200 日线、长期均线拐头、温和放量；以及对涨停股做形态、量价换手条件研究）。回答四点：(1) 妙想是否有独立的智能选股 skill；(2) 其输入/输出/配额/账号契约；(3) 现有 mx-data 端点能否表达这些条件；(4) mx-zixuan 到底是什么、ADR 0005 的排除理由是否需要修订。

## 调查方法

- 读本仓库一手资产：`.scratch/mx-skills/REPORT.md`（mx-data 调研）、`.scratch/mx-skills/mx-data-compare-report.md`（issue #19 实测）、`.scratch/mx-skills/mx-search-live-report.md`（issue #20 实测）、`.scratch/mx-skills/mx-data/mx-data/SKILL.md`、`src/qstock_mcp/mx_client.py`、`docs/adr/0005-miaoxiang-proxy-integration.md`、`skills/daily-review/SKILL.md`、`skills/sepa/SKILL.md`。
- 读官方文档：[东方财富妙想Skills安装指南](https://marketing.dfcfw.com/res/download/A620260623NIYC2U.md)（枚举妙想 skill 全集与各 skill 下载链接）、[妙想Skills 页面](https://dl.dfcfs.com/m/itc4)。
- 按安装指南给出的官方链接下载 mx-xuangu / mx-zixuan 两个 zip（`https://marketing.dfcfw.com/res/download/A620260623PHDKPP.zip`、`A6202603314TMGR1.zip`），解压到 `/tmp/mx-research/`（**临时目录，未入仓库、未修改仓库任何文件**；如需留存建议移入 `.scratch/mx-skills/`），通读其 SKILL.md 与 Python 脚本。
- **未执行任何消耗配额的调用**（`.scratch/mx-skills/run_mx_query.py` 只读未跑；`MX_APIKEY` 未触碰）。mx-xuangu 的全部结论来自官方 SKILL.md 与脚本源码静态分析，**未经实测**。

## 妙想 skill 全集（官方安装指南枚举，共 6 个）

| slug | 官方名 | 端点 | 性质 | 本仓库状态 |
|---|---|---|---|---|
| mx-data | 妙想金融数据 | `/finskillshub/api/claw/query` | 自然语言查数 | 已接入（`mx_query`） |
| mx-search | 妙想资讯搜索 | `.../news-search` | 资讯检索 | 已接入（`mx_search`） |
| **mx-xuangu** | **妙想智能选股** | **`.../stock-screen`** | **条件选股** | **未发现、未接入** |
| mx-zixuan | 妙想自选股管理 | `.../self-select/get`、`.../self-select/manage` | 账户自选股查/增/删 | ADR 0005 排除 |
| mx-moni | 妙想模拟组合管理 | （未细查） | 模拟组合操作 | ADR 0005 排除 |
| mx-poster | 妙想AI社区 | （未细查） | 社区内容 | 未发现 |

来源：安装指南"下载安装"各节与目录树；端点前三个来自各 skill 脚本源码（`mx_xuangu.py:120`、`mx_zixuan.py` 的 SKILL.md 接口说明、`mx_client.py:15-16`）。

**注意**：`.scratch/mx-skills/REPORT.md`（2026-09-01）只调研了 mx-data/mx-search 两个包，未枚举全集；ADR 0005 提到 mx-zixuan/mx-moni 但未提 mx-xuangu/mx-poster。即 **mx-xuangu 属于"当时未发现"，不是"评估后放弃"**——这与 `docs/research/2026-10-14-miaoxiang-skill-mcp-usage.md:56` 对 mx-zixuan/mx-moni"评估后放弃"的定性不同。

## 结论一：存在独立的智能选股 skill —— mx-xuangu（妙想智能选股）

官方名"妙想智能选股"，slug `mx-xuangu`，版本 1.0.4（`_meta.json`，publishedAt 2026-03-18，与 mx-data 1.0.5 同日发布、同一 ownerId）。官方定位描述："为 OpenClaw 提供通过行情、技术、财务等指标进行选股的能力。通过指标、技术信号、主营业务、主要产品等筛选股票"（[妙想Skills 页面](https://dl.dfcfs.com/m/itc4)，页面为 JS 渲染，该描述经搜索快照取得）；skill 内描述："根据用户问句中的选股条件，智能解析并转换为 API 可识别的选股规则，调用东方财富官方选股接口进行筛选"（mx-xuangu/SKILL.md:21-22）。

- **端点**：`POST https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen`（mx_xuangu.py:120），与 mx-data/mx-search 同域名同前缀。
- **它是独立 skill、独立端点，不是 mx-data 的一种用法**——虽然调用形态同构（apikey header + 单个自然语言字段）。

## 结论二：输入/输出/配额/账号契约

**输入**：自然语言条件串，body 为 `{"keyword": "<问句>"}`（mx_xuangu.py:145-147），无结构化参数。官方示例（SKILL.md:47-71）覆盖：

- 行情条件："今日涨幅大于2%的A股"、"成交量大于10亿的股票"、"股价在10元到20元之间"；
- 财务条件："净利润增长率大于30%的股票"、"ROE大于15%，净利润连续三年增长"；
- 估值条件："市盈率小于20并且市净率小于2"；
- **范围限定**："新能源板块市盈率小于30的股票"、"沪深300成分股中分红率最高的10只股票"——即股票池可以限定为**板块/指数成分股**；
- 组合条件用逗号分隔。

**"在这 10 只指定股票里筛"是否可行：文档未明示，但有一条强线索**——响应字段释义中 `totalCondition.describe` 的示例是"今日涨跌幅在 [1.5%,2.5%] 之间 **且 股票代码**"，`parserText` 示例同样以分号分隔出"股票代码"单条件（SKILL.md:153-155），说明后端解析器支持把**股票代码作为筛选条件**。把 10 个代码写进问句（如"在 600519、300750、…… 中筛选出……"）很可能可解析，**但未经实测，需持 key 验证**。技术形态类条件（"站上 200 日线"、"52 周最高价 70% 以上"、"均线拐头"、"温和放量"）在官方示例中完全没有出现——示例只到"指标、技术信号"的概括描述，**这类条件的可解析性是本项目场景的最大未知，必须实测**。

**输出**（mx_xuangu.py:153-184 + SKILL.md:104-155）：

- `data.data.allResults.result.dataList[]` 全量行 + `columns[]` 列定义（`key`/`title`/`unit`/`dateMsg`），行键含 `SECURITY_CODE`、`SECURITY_SHORT_NAME`、`MARKET_SHORT_NAME`、`NEWEST_PRICE`、`CHG` 等；
- 兜底：`data.data.partialResults`（Markdown 表格字符串，脚本自带解析器）；
- 条件回显：`responseConditionList[]`（每个单条件的描述与匹配股数）、`totalCondition`（组合条件与总命中数）、`parserText`（解析后的条件文本）——**这个回显对验证"我的条件被正确理解"很有价值**，正好对冲 mx-data 已知的 NL 解析不透明问题；
- 业务层状态：`status=0` 成功、`data.code=100` 解析成功、`resultType=2000` 标准选股结果。

**配额**：SKILL.md 异常表同样列 `code=113 今日调用次数已达上限`、`401/密钥不存在`（SKILL.md:91-98），语义与 mx-data 相同。按 ADR 0005"按妙想各 skill 分别记录请求次数"的口径，mx-xuangu 大概率是**独立日配额**（与 mx-data 的 20 次/日池分开），但文档未给数值、响应无用量回显，**是否共享池需实测**。若接入，`mx_quota.py` 需加 `mx-xuangu` 维度。

**账号体系**：纯 apikey（`MX_APIKEY`，与其他妙想 skill 共享同一把 key），**不绑定东财通行证账户、不读写用户任何账户数据**——与 mx-zixuan 本质不同（见结论四）。从 ADR 0005 的"数据服务定位"看，mx-xuangu 属于数据查询类，不在当初的排除范围内。

**与本仓库的接入成本**：`MxClient`（`src/qstock_mcp/mx_client.py`）只需加一个 `stock_screen(keyword)` 方法（新端点常量 + 同 `_post` 骨架），与 query/search 完全同构。

## 结论三：mx-data 能否表达这些条件——部分能，但都不是最优路径

- **形态类 yes/no 问句（"贵州茅台是否站上 200 日线"）在全部实测记录中没有出现过**。issue #19 的 7 次 mx-data 实测全是财务/分红类（mx-data-compare-report.md:11-27）；技术指标条件的表现**无实测数据**，不能预设可行。
- **mx-data 能取历史日线**（SKILL.md 示例"比亚迪近一年每个交易日的开盘价收盘价成交量"，`.scratch/mx-skills/mx-data/mx-data/SKILL.md:55`；REPORT.md:33-36 列为宣称能力），所以"用 mx-data 取数 + 本地算条件"技术上成立——但本仓库已有更好的路径：**`query_daily`（三源确定性日线）+ indicator-tools 管道**，这正是 sepa skill 的现有做法（`skills/sepa/SKILL.md` 的趋势模板检查即"52 周高点 70% 以上、站上均线"这类条件的本地计算版，零配额消耗、口径确定）。用户列举的条件与 SEPA 趋势模板高度重合，**用 mx-data 做这件事没有增量价值，反而引入 NL 不确定性和配额消耗**。
- mx-data 在此场景还有两个已知硬伤：复权口径不透明（REPORT.md:47——"52 周最高价/均线"对复权敏感）与未披露期间静默回退（mx-data-compare-report.md:26）。
- **单问句携带多只股票已验证可行**：daily-review 已用 `mx_query` 单问句批量查多股概念归属以省配额（`skills/daily-review/SKILL.md:34`）；issue #19 实测也确认实体识别稳定（同语义 3 种问法结果一致，mx-data-compare-report.md:76）。但注意 NL 层会冗余带出同名港股行，消费方须按 `secuCode` 过滤（ADR 0005:12）。

**小结**：用户的"10 只池 + 形态条件筛选"需求，确定性路径（query_daily + indicator-tools）已覆盖；mx-xuangu 的增量价值在**反向场景**——不设股票池、用条件在全市场/板块里海选（如"今日涨停且换手率大于 X"），以及用 `responseConditionList` 回显做条件可解析性的快速试探。

## 结论四：mx-zixuan 是自选股管理，不是条件选股；ADR 排除理由成立且无需修订

mx-zixuan 官方名"妙想自选股管理"："基于**东方财富通行证账户**数据……支持通过自然语言**查询、添加、删除自选股**"（mx-zixuan/SKILL.md:5、19-24）。端点 `.../self-select/get`（查）与 `.../self-select/manage`（增删，body 为自然语言指令，SKILL.md:125-134）。它操作的是**用户东财账户下的自选股列表**，是典型的账户读写操作——ADR 0005"账号操作类，超出数据服务定位"的定性准确。

- mx-zixuan **不是**"智能选股/条件选股"能力；真正的条件选股是 mx-xuangu。
- 即使只取 mx-zixuan 的"查询自选股"只读用法，它返回的是账户既有自选股清单（含最新价/涨跌幅/换手率/量比快照，SKILL.md:100-108），**不接受筛选条件**，与"给定 10 只 + 条件筛子集"的需求无关。ADR 0005 的排除对它依然成立。
- **需要修订的不是 mx-zixuan 的排除，而是 ADR 0005 的 skill 枚举不完整**：mx-xuangu（数据查询类、纯 apikey）当时未被发现，按 ADR 0005 自己的分类标准应属可接入对象。若决定接入，建议以 ADR 补充条款记录；接入形态可直接复用 `_mx_exchange` 骨架（配额先检、触顶不调上游）加一个 `mx_stock_screen(keyword)` 透传工具，定位 proxy 面（不规格化、不落库）。

## 待实测清单（持 key 验证前不可当下结论）

1. mx-xuangu 是否支持任意代码清单作为股票池（"在 600519、300750…中筛选"）；
2. 技术形态条件的可解析性："站上 200 日均线"、"股价高于 52 周最高价的 70%"、"30 周均线拐头向上"、"温和放量"、换手率/量比条件；
3. 配额：mx-xuangu 日配额数值、是否与 mx-data 共享池、code=113 触发形态是否与文档一致；
4. 涨停场景："今日涨停的 A 股"类问句能否直接返回涨停池（与库内 `zt_pool` 交叉验证）；
5. 响应稳定性：同一问句多次调用结果是否一致（对照 mx-data 的 NL 确定性实测方法，issue #19）。

## 附：本次新增一手材料的存放说明

mx-xuangu / mx-zixuan 的官方 zip 解压于 `/tmp/mx-research/`（临时目录，会随系统清理消失）。若后续要接入 mx-xuangu，建议把 zip 与解压目录按 mx-data/mx-search 的先例移入 `.scratch/mx-skills/` 作为实测资产与 fixture 模板（ADR 0005:14 的约定）。
