# q_stock_skills

面向通用 AI agent 的证券数据与分析能力：一个 MCP 数据服务（Python，stdio 入口）+ 四个可加载的 agent skill。它是旧 B/S 分析系统 Server 端的替代品——数据由 agent 直接获取，分析结论由 agent 用 skill 产出并持久化。只做 A 股。

**5 分钟全图认知见 [`docs/knowledge-graph.md`](docs/knowledge-graph.md)**；领域词汇见 [`CONTEXT.md`](CONTEXT.md)；设计决策见 [`docs/adr/`](docs/adr/)。

## 组成

- **MCP 数据服务**（`src/qstock_mcp`，14 个工具，五个能力面）：
  - `init_database`：幂等建表 + 轻量初始化（股票清单/全市场快照/主要指数日线），可选 `backfill_history=True` 全量回溯
  - `fetch_daily` / `query_daily`：个股日线抓取与查询，**查询自愈**——库内缺数据自动补抓头尾缺口；`days=N` 按交易日回溯
  - `query_index_daily`：主要指数历史日线纯读（7 个指数白名单），数据由 `init_database` 写入
  - `fetch_market_snapshot` / `query_snapshot`：全市场快照（约 5000+ 只），业务键幂等 upsert；抓取失败按三态错误契约分类上报（见下）；`query_snapshot` 支持服务端聚合（`aggregate=breadth,amount`，含分市场口径）与 `order_by+limit` 排序取前 N，大结果不必整表拉回
  - `fetch_board_snapshot` / `query_board_data`：盘面快照（指数/板块/涨跌停池/强势股/龙虎榜），section 级独立成败；核心 section（指数/板块/涨跌停池/强势股）上游不可达即整体报错并携带 per-section 明细，中途失败不回滚、可按 section 分段重试；指数段按 7 大指数白名单定点补漏（含科创 50）
  - `get_fundamentals`：基本面透传（proxy），不规格化、不落库
  - `mx_query` / `mx_search` / `query_news`：妙想自然语言查数与资讯搜索（独立日配额，搜索落库 `news_items`）+ 库内资讯纯读
  - `save_conclusion` / `query_conclusions`：分析结论读写，通用结论表 + JSON payload，同键 upsert
- **四个 agent skill**（`skills/`，自包含，任何通用 AI agent 可加载）：
  - `indicator-tools`：指标计算管道脚本（MA/MACD/BOLL/RSI/导数、corr 多标的相关性矩阵、vol_price 量价背离、promote 连板晋级率、deviation 个股对基准偏离值累计等），stdin 吃 `query_daily`/`query_index_daily` 输出、stdout 吐指标 JSON，纯标准库不触库
  - `sepa`：SEPA 方法论判断，经 MCP 取数 + indicator-tools 管道算指标
  - `limit-up-analysis`：涨停/连板分析——涨停归因（板块/大小票/业绩题材粗分）+ 连板梯队与晋级率 + 龙头点评，结论落库 `limit_up.close`，可单独调用
  - `daily-review`：每日复盘编排（总体立场 ≤5 行递进摘要 + 六节：指数表现/板块轮动（含 5/10 日趋势）/涨停连板与龙虎榜（含严重异动偏离值监控）/成交额前 75 结构/归因与量价复盘/风险提示），执行入口先确认复盘日、交易时段禁跑当日；数据源故障走中止分支并输出异常报告（不产出残缺复盘），取数优先走服务端聚合、备有 stdio 落盘通道脚本；结论经 `save_conclusion` 落库、markdown 报告落盘；附 launchd/cron 调度配置说明
- **数据源**（issue #121 定稿，spec #111 剔东财收口；efinance 已整体退役、东财运行时代码面清零）：多源 fallback——个股日线 腾讯 → 新浪 → baostock（issue #112，收盘后当晚含当日行、北交所 920xxx 可取）；全市场快照 腾讯批量报价 → 腾讯 rank 榜单 → 新浪直连（issue #116，主源 18 字段全覆盖、含北交所、时间戳含日期）；指数快照 腾讯批量报价 → 新浪（issue #117，腾讯为唯一含量比源、含科创50）；指数日线 腾讯 K 线 → baostock（issue #117，腾讯 T+0 当晚含当日行、覆盖科创50，baostock 次晨权威复核）；boards 行业段 申万官网单源（issue #118，申万故障时复盘降级库内自算 `query_sw_board_daily`）；lhb 交易所官方单源（issue #114/#121：深所 JSON + 上所定宽文本 + 北所 bseinfo.net；新浪互补校验走 `lhb --check` 对拍）；zt_pool 涨跌停/炸板三池由同花顺涨停揭秘单源承载（issue #120，低频纪律：每日每池一次 + 长退避 + 封禁探测），strong_stocks 库内自算（60 日新高 ∪ 近 10 日多次涨停）；股票清单 交易所官网源（akshare `stock_info_a_code_name`）→ baostock；基本面链 akshare（新浪财务摘要 + 百度股市通估值，issue #115）→ baostock。失败明确报错，绝不伪造数据；连接级故障（上游不可达）按三态错误契约结构化上报并附重试建议（ADR 0007）
- **数据库**：PostgreSQL 独立新库（14 张表，DDL 在 `src/qstock_mcp/sql/`），连接只读 `PG_DSN` 环境变量

## 安装与接入

要求 Python ≥ 3.11 与一个 PostgreSQL 实例。

```bash
pip install -e ".[sources,dev]"   # sources = akshare/baostock/curl_cffi 真实数据源库（efinance 已退役，issue #121）
```

注意 `qstock-mcp` 入口只装在执行 `pip install` 的那个环境里（本仓库即 `.venv/`），因此 `command` 应写成该环境内的**绝对路径**；或改用 `python -m` 形式指向同一解释器：

```json
{
  "mcpServers": {
    "qstock": {
      "command": "/path/to/q_stock_skills/.venv/bin/qstock-mcp",
      "env": { "PG_DSN": "postgresql://user:pass@host:5432/qstock" }
    }
  }
}
```

```json
{
  "mcpServers": {
    "qstock": {
      "command": "/path/to/q_stock_skills/.venv/bin/python",
      "args": ["-m", "qstock_mcp.server"],
      "env": { "PG_DSN": "postgresql://user:pass@host:5432/qstock" }
    }
  }
}
```

想脱离项目目录全局可用，就装进常驻环境（如 `pipx install .` 或用户级 pip），让 `qstock-mcp` 落在 PATH 上；否则一律写绝对路径。

同一次安装还会注册调度 CLI `qstock-scheduler`（见下节「定时采集与历史补录」）。

环境变量一律放进 MCP client 配置的 `env` 块里——GUI 类 client 不一定继承你 shell 里 `export` 的变量。完整清单：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `PG_DSN` | 是 | PostgreSQL 连接串；除 `get_fundamentals`/`mx_query` 外的落库/查询工具都依赖它，缺失报明确错误 |
| `MX_APIKEY` | 否 | 妙想开放平台 apikey，仅 `mx_query`/`mx_search` 需要 |
| `MX_DAILY_LIMIT` | 否 | 妙想工具通用每日配额上限（缺省 20） |
| `MX_DAILY_LIMIT_MX_DATA` / `MX_DAILY_LIMIT_MX_SEARCH` | 否 | 单工具配额覆盖，优先于 `MX_DAILY_LIMIT` |
| `MX_QUOTA_FILE` | 否 | 配额 ledger 路径（缺省 `~/.qstock-mcp/quota.json`） |

`env` 块示例（按需取舍可选项）：

```json
"env": {
  "PG_DSN": "postgresql://user:pass@host:5432/qstock",
  "MX_APIKEY": "your-mx-apikey",
  "MX_DAILY_LIMIT": "20"
}
```

首次使用调一次 `init_database` 即可完成建表与轻量初始化。轻量初始化不等于全市场历史数据已就绪；从空库或已有断档库建立最近 90 个交易日的历史分析数据，按 [`docs/first-run.md`](docs/first-run.md) 的顺序执行并验收。

## 启动方式

这是 stdio MCP server：**不需要手动启动**，由 MCP client（Claude Code / Kimi Code / 其他 agent）按上面的配置自动拉起——client 通过子进程的 stdin/stdout 与它通信，会话结束自动回收。配置后重启 client 即可使用 `qstock` 前缀的 14 个工具。

需要脱离 client 手动验证时（冒烟调试），直接运行入口命令：

```bash
qstock-mcp                    # 等价于 python -m qstock_mcp.server
```

进程启动后挂起等待 stdin 上的 JSON-RPC 消息，没有输出属正常（日志只走 stderr）。发一行 initialize 握手可验证服务可用（stdio 通道为换行分隔 JSON）：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' | qstock-mcp
```

Ctrl+C 退出。握手本身不触库；若要手动实测工具调用，先在当前 shell 里 `export PG_DSN`（以及可选的 `MX_APIKEY`）。注意：手动启动走的是前台 stdio 通道，不是常驻后台服务；要让 agent 长期使用，仍应配进 MCP client 由它管理生命周期。

## 定时采集与历史补录（CLI）

MCP server 只提供工具，**自己不会按点采集**。日常数据靠独立的调度 CLI `qstock-scheduler`（与 `qstock-mcp` 同一次 `pip install` 注册），它直调 tools_* 核心层、不经 MCP 协议层：

| 子命令 | 用途 | 建议执行时机 |
| --- | --- | --- |
| `close [--date YYYYMMDD]` | 收盘后批：盘面五 section（boards/zt_pool/strong_stocks/lhb/indices）快照采集（boards 行业段申万官网单源，issue #118） | 交易日 15:30 之后 |
| `morning [--date YYYYMMDD]` | 次晨批：baostock 一把抓前一交易日全市场日线 + 7 大指数尾行缺失即补 + 申万板块分析报表估值维度（sw_board_valuation part，T+1） | 交易日开盘前（baostock 当日 bar 发布较晚，目标日恒为前一交易日） |
| `backfill [--start D --end D \| --trading-days N] [--force]` | 申万板块历史回补：默认断点续跑；`--force` 整窗重抓并覆盖同键记录 | 手动/灾难恢复，非定时任务 |
| `sw-boards [--date D] [--check]` | 申万板块固定入口（issue #118）：双级实时快照落库（收盘后=当日收盘截面）；`--check` 官网 vs 库内 vs 自算三方位对拍不落库 | 收盘后；对拍按需 |
| `sw-dict [--url U]` | 申万成分字典在线刷新（issue #118）：官网分类 xls 清空重载 'sw' 体系（含北交所归属），幂等 | 每周一次 |
| `daily --codes A,B [--date D \| --start D --end D \| --trading-days N] [--force]` / `daily --all` | 个股日线固定入口：默认只补头尾缺口；`--force` 整窗重抓；`--check` 三源对拍不落库 | 收盘后 19:30 左右的当日行（T+0）补采；`--all` 建议配 `--throttle 1` |
| `limitpool [--date D]` / `limitpool --probe` | 涨停池固定入口（issue #120）：zt_pool 三池经同花顺涨停揭秘低频抓取（每日每池一次、封禁探测），strong_stocks 库内自算（60日新高 ∪ 近10日多次涨停）；`--probe` 只做封禁探测 | 交易日 19:30 之后（THS 盘后数据 + 当日个股日线已入库）；被封禁当日勿重试，等次日 |
| `snapshot [--date D] [--check]` | 全市场快照固定入口（issue #116）：腾讯批量报价 → 腾讯 rank → 新浪三层链抓取落库；`--check` 三源直取对拍不落库；主源 18 字段全覆盖、含北交所 | 收盘后（15:30 之后）；盘中勿高频 |
| `index [--codes A,B] [--date D \| --start D --end D \| --trading-days N] [--verify]` | 指数日线固定入口：历史回补 / T+0 补采；`--verify` 对照交易日历列缺口 | 收盘后 19:30 左右；`--verify` 复盘前巡检 |
| `lhb [--date D \| --start D --end D \| --trading-days N]` | 龙虎榜 basic 与滚动统计；历史逐日完整替换、60 日历日前置 staging，营业部累计榜不写历史 | 收盘后 18:30 或历史回补 |
| `coverage [--start D --end D \| --trading-days N]` | 个股、指数、申万板块、龙虎榜窗口覆盖核验；首末日期、行数、缺失日，纯读不补抓 | 回补前预检与回补后验收 |

耗时量级：`morning` 约 1 分钟（baostock 一把抓约 20 秒 + 指数尾行通常零请求 + 申万估值报表 2 次分页调用）；`close` 数分钟到十几分钟；`limitpool` 约 40 秒（THS 三池 × 池间 20s 长退避 + 库内自算秒级）；`backfill --source sw` 155 板块 × trend 全历史（1999 起，单请求/板块）约 2~3 分钟（0.4s/请求限速内）；`daily --codes` 每只约 1–2 秒（腾讯单请求），`daily --all` 约 5900 只 × 节流间隔（缺省 1 秒/只，约 1.5–2 小时）。

重复执行按业务键幂等；每次执行落采集台账（可用 MCP 工具 `query_collection_log` 查询）。退出码 0=全部成功，1=任一部分失败（明细在 stdout JSON），2=参数错误，退出码不单独证明窗口完整。长时间运行的批按双阈值向 **stderr** 打进度，stdout 只留最终结构化 JSON。backfill 默认跳过同窗口已 ok 板块，`--force` 时整窗重抓；连续 10 个板块全源失败仍熔断。全链路上游请求 60s 全局超时，同源重试 0.5/1/2s 退避，by-design 不支持的源不重试。

**漏采补录示例**：某天定时任务没跑、复盘发现缺数时，按数据类型选路径——

```bash
# 补 10-15、10-16 的全市场个股日线与指数日线（index_daily/stock_daily）
qstock-scheduler morning --date 20261015
qstock-scheduler morning --date 20261016

# 补 10-14~10-16 的板块行情（market_boards）；申万口径 155 板块全历史回补
qstock-scheduler backfill --source sw

# 收盘后当晚补个股日线当日行（T+0；沪/深/北代表股，北交所旧码自动归一化）
qstock-scheduler daily --codes 600519,000001,920001 --date 20261020
# 三源对拍（腾讯/新浪/baostock 逐源直取比对，不写库）
qstock-scheduler daily --codes 600519,000001,920001 --date 20261020 --check

# 指数日线断档回填（如科创50 2026-10-13 起缺口；幂等可重跑）
qstock-scheduler index --codes 000688 --start 20261013 --end 20261020
# 指数覆盖核验（7 大指数逐一对照交易日历，不写库）
qstock-scheduler index --start 20261013 --end 20261020 --verify
# 补当日涨停三池 + 自算强势股（THS 低频纪律：每日每池一次，当日已采集自动跳过）
qstock-scheduler limitpool --date 20261020
```

注意可补性边界：`zt_pool` 为 THS 源、回溯深度有限，`strong_stocks` 依赖日线与涨停池输入，不承诺完整 90 日；龙虎榜历史统一走 `lhb` CLI，只补 basic 与滚动统计，营业部累计榜无历史日期接口。`indices` 快照与 `boards` 实时快照属实时语义 section，**MCP 路径对历史日期硬拒绝**；板块历史走 `backfill`，指数日线走 `index`。`market_snapshot`（涨跌家数/两市成交额）没有历史 API，漏采即永久缺失。因此定时任务的当日告警比事后补录更重要。

launchd/cron 安装样例见 [`docs/scheduler-launchd.md`](docs/scheduler-launchd.md)（用户自行安装；非交易日触发产生 empty 台账行，属正常语义）。

### 各源当日数据就绪时间线

挂定时任务与决定复盘时点前，先了解各承载源当日数据何时就绪（issue #121 落档，spec #111）：

| 数据面 | 承载源 | 当日数据就绪时间 |
| --- | --- | --- |
| 指数快照 / 全市场快照 / 个股日线 / 指数日线当日行 | 腾讯（qt.gtimg.cn / K 线） | **T+0**——收盘后即可得当日行（个股日线收盘后当晚即含当日行，已实证） |
| boards 行业段实时快照 | 申万官网 | **盘中实时**——收盘后末次快照即当日收盘截面 |
| zt_pool 涨停三池 | THS 涨停揭秘 | **盘后**——建议 19:30 后采集（低频纪律：每日每池一次） |
| 个股日线（全市场）/ 复权序列 | baostock | 日 K 约 **17:30** 就绪、复权数据约 **18:00** 就绪（次晨批角色：权威复核兜底） |
| lhb 龙虎榜 | 交易所官方（深所/上所/北所） | 约 **16:30–18:00** 陆续发布（待 #122 对拍确认精确窗口） |
| 申万板块估值维度 | 申万分析报表 | **T+1**——次晨批 `morning` 落 `sw_board_valuation` |

**最佳复盘窗口：19:30 之后**——THS 涨停池、baostock 复权序列均已就绪，收盘后批（`close`）与各 T+0 补采都已完成；15:30–19:30 之间可出报告但涨停/连板章节可能缺 zt_pool 数据（核心 section 缺失会走故障中止分支，详见 daily-review skill）。

## 输出契约

所有工具与 skill 脚本输出自描述 JSON：成功 `status:"ok"` + 参数回显 + 实际数据源；数据不足 `status:"insufficient_data"`（非错误）；失败 `status:"error"` + 已尝试数据源与原因。抓取类工具的错误另带机器可读三态分类（`error_kind`：`upstream_unreachable` 上游连接级故障、可稍后重试 / `no_data_for_date` 当日无数据 / `empty_ok` 正常空结果，语义见 ADR 0007）。日志只走 stderr，不进数据通道。

## 测试

```bash
pytest tests/ skills/
```

两条测试接缝：MCP 工具函数层（注入 fake 适配器 + 真实 PG 测试库，PG 不可达自动 skip）与 skill 脚本 stdin/stdout 契约。网络抓取不进测试。

## 文档导航

- [`docs/first-run.md`](docs/first-run.md)：最近 90 个交易日数据准备快速引导 + 全部调度 CLI 命令手册
- [`docs/knowledge-graph.md`](docs/knowledge-graph.md)：项目知识图谱——总览图、实体清单、关键流程、「想改 X 先看哪里」
- CLI 手册：[`daily`](docs/daily-source-cli.md) / [`index`](docs/index-source-cli.md) / [`sw-boards`](docs/sw-boards-cli.md) / [`lhb`](docs/lhb-source-cli.md) / [`limitpool`](docs/limitpool-cli.md) / [`snapshot`](docs/snapshot-source-cli.md) / [`stock-list`](docs/stock-list-cli.md) / [`parity`](docs/parity-cli.md)
- [`CONTEXT.md`](CONTEXT.md)：领域词汇表
- [`docs/adr/`](docs/adr/)：设计决策（MCP 边界 / PG 选型 / 通用结论表 / init 设计 / 妙想接入 / 金额单位万元 / 抓取三态错误契约）
- [`docs/scheduler-launchd.md`](docs/scheduler-launchd.md)：`qstock-scheduler` 定时采集的 launchd 安装样例
- [`docs/agents/`](docs/agents/)：agent 协作约定（issue 流程、triage 标签）
