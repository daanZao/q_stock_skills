# 首次使用与最近 90 个交易日回补

本指南面向第一次准备历史分析数据的用户，目标是建立**历史分析就绪**的数据基础，不承诺过去 90 个交易日的每日复盘都能完整重做。

默认主路径刷新最近 90 个交易日的：股票清单、全市场个股日线、7 大指数日线、申万一级/二级板块、龙虎榜 basic 与滚动统计。窗口内已有数据按当前源和算法刷新，窗口外业务数据不动。

## 1. 安装并准备数据库

要求 Python ≥3.11 和 PostgreSQL。仓库根目录执行：

```bash
.venv/bin/pip install -e ".[sources,dev]"
export PG_DSN='postgresql://user:pass@host:5432/qstock'
```

GUI MCP client 不一定会继承 shell 的环境变量，应把 `PG_DSN` 同时写进 client 配置的 `env`。业务库操作前先按各 CLI 手册的“回滚与还原”章节备份。

## 2. 区分空库与已有库

### 空库

通过 MCP 调用一次 `init_database`（不开启 `backfill_history`），完成幂等建表和轻量初始化。检查返回的 `parts` 与 `failed_parts`；轻量初始化不等于全市场历史数据已就绪。

随后刷新股票清单：

```bash
qstock-scheduler stock-list
```

`daily --all` 以此清单为标的来源。清单代表当前标的集合，不是每个历史交易日的当时上市股票全集。

### 已有少量或断档数据

先执行只读覆盖预检，确认现有最早/最晚日期、行数和缺失交易日，再决定是否执行完整 90 日刷新：

```bash
qstock-scheduler coverage --trading-days 90
# 也可用 --start YYYYMMDD --end YYYYMMDD 指定窗口
```

`coverage` 不触发抓取；有缺口时退出码 1，逐标的/逐日 `missing_days` 与 `status=gap` 是决策依据。选择完整窗口后，不要只从库内最新日期向后补，也不要把 `init_database` 当作窗口刷新命令：它还会写当前快照和主要指数历史，不属于严格的 90 日窗口操作。

以下命令对窗内已有数据同样生效：个股日线与申万板块使用 `--force` 整窗重抓；指数逐区间重抓；龙虎榜逐日完整替换并重算。窗口外数据不删除、不改写。

## 3. 执行 90 个交易日刷新

四个命令使用同一窗口。`--trading-days 90` 按 A 股交易日历计算，不是 90 个自然日；省略 `--end` 时截止最近一个已收盘交易日。需要回补其他截止日时显式指定，例如：

```bash
END=20261023

# 全市场个股日线：重操作，先用 --limit 20 冒烟，再去掉 limit 全量执行
qstock-scheduler daily --all --limit 20 --trading-days 90 --end "$END" --force
qstock-scheduler daily --all --trading-days 90 --end "$END" --force --throttle 1

# 7 大指数日线
qstock-scheduler index --trading-days 90 --end "$END"

# 申万一级/二级板块历史
qstock-scheduler backfill --source sw --trading-days 90 --end "$END" --force

# 龙虎榜 basic + 近 60 日历日滚动统计
qstock-scheduler lhb --trading-days 90 --end "$END"
```

`daily --all` 全量通常约 1.5–2 小时，应保留节流。个股默认只刷新 `adj=qfq`；需要其他复权口径时分别显式执行，业务键不同，互不覆盖。

申万 `--force` 会忽略相同窗口的成功台账并重抓；不加时仍按原断点续跑规则跳过已完成板块。个股不加 `--force` 时仍只补头尾缺口。

## 4. 龙虎榜一致性保证

历史回溯与单日实时抓取的写入语义不同：

- 历史回溯只写 `lhb_basic` 和 `lhb_stock_statistic`，不写 `lhb_yyb_capital` / `lhb_yyb_most`。营业部累计榜没有历史日期参数，只在真实执行日刷新。
- basic 按交易日完整替换，避免旧来源、旧原因文本与新行同时残留并重复计数。
- 单所缺失、`partial_error` 或连接失败时，该日旧 basic 与 statistic 保留，不以残缺结果替换；失败原因进入结构化输出和采集台账。
- statistic 使用当前近 60 日历日公式逐日重算。窗口前额外 60 个日历日的 basic 只进入连接级临时 staging，业务库不写入这些窗外行。
- basic 替换与 statistic 重算在同一事务提交，避免中途失败留下半套结果。

## 5. 验收，不只看退出码

用刷新时的同一窗口执行统一覆盖核验：

```bash
qstock-scheduler coverage --trading-days 90 --end "$END"
# 可选的指数专项复核
qstock-scheduler index --trading-days 90 --end "$END" --verify
```

`coverage` 分别返回个股、7 大指数、申万板块的首末日期、行数与 `missing_days`，以及龙虎榜逐日 basic/statistic 行数和台账状态；`summary.gaps=0` 才表示覆盖验收通过。它只读数据库，不会借查询自愈补数。

再通过 MCP `query_collection_log` 检查目标窗口各 section 是否仍有 `error`，并对照刷新前记录确认窗口外首末行未修改。命令退出码 0 只表示执行层面没有失败，不单独证明窗口完整。`--check` / `parity` 的逐源对拍可作为额外质量验收，不是首次主路径的必需步骤。

## 6. 可补范围与历史复盘边界

| 数据面 | 历史入口与范围 | 关键限制 |
| --- | --- | --- |
| 个股日线 | `daily --trading-days/--start/--end`；全历史可另用 `init_database(backfill_history=True)` | 全量回溯是重操作；默认只补头尾，整窗刷新须 `--force` |
| 指数日线 | `index`，支持历史区间 | 补的是 `index_daily`，不是实时 `market_indices` |
| 申万板块 | `backfill --source sw`，历史深至 1999 | 只使用 `industry_l1/l2`；东财旧口径保留只读、不混算 |
| 龙虎榜 basic / statistic | `lhb`；沪深约 2015 起、北交所约 2021-11 起 | statistic 需要额外 60 日历日 staging |
| 营业部累计榜 | 仅真实执行日 | 无历史日期接口，禁止盖历史日期 |
| 涨停池 / 强势股 | `limitpool`；THS 历史深度有限，强势股由日线与涨停池派生 | 不承诺 90 日完整；输入不足不能当成完整结果 |
| 全市场快照、指数/板块实时快照 | 只能在真实交易日采集 | 无历史 API，漏采不能事后恢复 |

历史分析就绪不等于历史每日复盘就绪。全市场快照、涨停池等缺口仍可能使过去日期的复盘降级或无法出报。

## 7. 转入日常采集

首次回补不能替代后续例行采集。交易日收盘后执行 `close`、`snapshot`、`index`，数据就绪后执行 `limitpool`；次晨执行 `morning`。安装样例见 [`scheduler-launchd.md`](scheduler-launchd.md)，各命令细节见对应 CLI 手册。
