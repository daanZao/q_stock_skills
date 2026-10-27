# 首次使用与调度 CLI 指南

本文分两部分：

- **第一章：快速引导**——从空库开始，准备最近 90 个交易日的历史分析数据。
- **第二章：调度 CLI 命令手册**——按任务查找全部 `qstock-scheduler` 子命令。

完成快速引导后，库内应有最近 90 个交易日的全市场个股日线、7 大指数日线、申万一级/二级板块，以及龙虎榜 basic 与滚动统计。这里的目标是**历史分析就绪**，不表示过去 90 个交易日的每日复盘都能完整重做：全市场快照、实时板块快照和营业部累计榜等数据无法全部事后恢复。

> 整窗刷新会重新抓取并覆盖窗口内同业务键数据，窗口外业务数据不动。已有库执行前请备份；各写库命令的还原方法见对应专项手册。

## 第一章：快速引导

### 1. 准备环境与数据库

准备 Python ≥3.11、PostgreSQL，以及一个已创建的目标数据库。在仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[sources,dev]"
export PG_DSN='postgresql://user:pass@host:5432/qstock'
```

把 `PG_DSN` 中的用户、密码、主机和库名换成实际值。后续命令都在仓库根目录、同一个 shell 中执行；如果另开终端，需要重新设置 `PG_DSN`。

**成功标志**：`.venv/bin/qstock-scheduler --help` 能显示命令列表。

### 2. 初始化数据表

按 [README 的 MCP 配置示例](../README.md#安装与接入) 配置并重启 MCP client。client 配置的 `env` 中也要设置同一个 `PG_DSN`，不能只依赖 shell 中的 `export`。

在 MCP client 中调用一次 `init_database`，参数为：

```json
{"backfill_history": false}
```

不要开启 `backfill_history`：它会触发全市场全历史回溯，不是本指南的 90 个交易日刷新。

**成功标志**：返回 `status="ok"`，且 `parts.stock_list.status="ok"`。如果返回 `partial_error`，查看 `failed_parts` 和各 part 的错误，修正后重试；不要继续跑全量任务。

### 3. 已有库先预检，空库直接继续

如果目标库已有数据，先执行只读预检：

```bash
.venv/bin/qstock-scheduler coverage --trading-days 90
```

查看各数据面的 `missing_days` 和 `status`，再确认是否按本指南覆盖窗内数据。预检发现缺口时返回退出码 1，表示“覆盖不完整”，不是命令自身故障。空库可跳过本步。

### 4. 刷新股票清单

全市场日线以股票清单为标的来源。无论是空库还是已有库，都先刷新：

```bash
.venv/bin/qstock-scheduler stock-list
```

**成功标志**：`status="ok"`，`rows_upserted` 大于 0。若为 `empty` 或 `error`，不要继续；根据输出中的 `attempted_sources` 排查数据源后重跑。

### 5. 用 20 只个股做日线冒烟

先验证数据源、复权口径和写库链路：

```bash
.venv/bin/qstock-scheduler daily --all --limit 20 --trading-days 90 --force
```

**成功标志**：`status="ok"`、`failed=0`，20 只股票均有结果。若为 `partial_error` 或 `error`，先查看 `results` 中失败股票的原因，修正后重跑本命令，不要直接开始全量任务。

### 6. 刷新全市场个股日线

```bash
.venv/bin/qstock-scheduler daily --all --trading-days 90 --force --throttle 1
```

全量通常需要约 1.5–2 小时。进度日志输出到 stderr，最终 JSON 输出到 stdout；长时间运行不等于卡死，请保留 1 秒/只的节流。

**成功标志**：`status="ok"`、`failed=0`。失败时根据 `results` 定位问题，修正后重跑同一命令。

### 7. 刷新指数、申万板块和龙虎榜

依次执行：

```bash
# 7 大指数日线
.venv/bin/qstock-scheduler index --trading-days 90

# 申万一级/二级板块历史
.venv/bin/qstock-scheduler backfill --source sw --trading-days 90 --force

# 龙虎榜 basic 与滚动统计
.venv/bin/qstock-scheduler lhb --trading-days 90
```

每条命令成功后再执行下一条。`status="ok"` 表示执行成功；龙虎榜正常无数据的日期会记入 `empty_days`，不属于失败。申万 `--force` 会忽略同窗口成功台账并重抓；个股与申万不加 `--force` 时仍保留各自的默认补缺口/断点续跑行为。

上述命令省略 `--end` 时，都截止最近一个已收盘交易日。若要回补到指定截止日，给快速引导中的 `daily`、`index`、`backfill`、`lhb` 以及下一步的 `coverage` **全部添加同一个** `--end YYYYMMDD`，不要混用窗口。

### 8. 验收覆盖结果

```bash
.venv/bin/qstock-scheduler coverage --trading-days 90
```

**完成标准**：返回 `status="ok"`，且 `summary.gaps=0`。

如果有缺口，`coverage` 返回 `status="error"`、退出码 1。查看 `sections` 中对应标的或日期的 `missing_days`、`status` 和采集台账状态，重跑对应刷新命令后再次验收。`coverage` 只读数据库，不会自动补数；其他命令退出码 0 也不能替代这一步。

首次回补完成后，日常采集使用 `morning`、`close`、`snapshot`、`index` 和 `limitpool` 等命令，见第二章及 [launchd 配置说明](scheduler-launchd.md)。

## 第二章：调度 CLI 命令手册

### 通用规则

- 命令格式为 `qstock-scheduler <子命令> [参数]`；本章示例使用仓库内的 `.venv/bin/qstock-scheduler`。
- 写库命令依赖 `PG_DSN`。日期使用 `YYYYMMDD`；`--trading-days N` 按 A 股交易日历计算，不是 N 个自然日。
- stdout 输出最终结构化 JSON，进度和日志输出到 stderr。通常退出码 0 表示成功，1 表示执行失败或验收有缺口，2 表示参数解析错误；对拍命令的差异语义见对应专项手册。
- 写库操作按业务键幂等，可重复执行；采集台账为 append-only 审计记录。幂等不等于可撤销，覆盖旧值前仍需备份。
- `--check`、`--verify`、`--probe`、`parity` 和 `coverage` 不执行写库抓取，但部分命令会读数据库或直接请求上游。

### 初始化与历史数据

#### `stock-list`：刷新或互证股票清单

- **写库**：默认模式写 `stock_list` 并落台账；`--check` 不写库。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler stock-list
  .venv/bin/qstock-scheduler stock-list --check
  ```

- **关键参数**：`--check` 逐源比较清单行数、代码集合和北交所覆盖，不落库。
- **结果判断**：默认模式关注 `status`、`rows_upserted`、`bse_rows`；用于 `daily --all` 前，清单不能为空。
- **详细手册**：[股票清单 CLI](stock-list-cli.md)。

#### `daily`：个股日线抓取、区间刷新与三源对拍

- **写库**：抓取模式写 `stock_daily` 并落台账；`--check` 不写库。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler daily --codes 600519,000001 --date 20261023
  .venv/bin/qstock-scheduler daily --all --trading-days 90 --force --throttle 1
  .venv/bin/qstock-scheduler daily --codes 600519 --date 20261023 --check
  ```

- **关键参数**：`--codes` 与 `--all` 二选一；`--limit` 配合 `--all` 冒烟；`--adj` 可选 `qfq/hfq/none`；`--force` 整窗重抓；`--throttle` 控制逐股间隔；`--check` 对拍结束日数据。
- **结果判断**：抓取模式应为 `status="ok"`、`failed=0`；`partial_error` 时查看逐股 `results`。对拍差异见 `diffs`。
- **详细手册**：[个股日线 CLI](daily-source-cli.md)。

#### `index`：指数日线回补、当日补采与覆盖核验

- **写库**：默认写 `index_daily` 并落台账；`--verify` 只读。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler index --trading-days 90
  .venv/bin/qstock-scheduler index --codes 000688 --date 20261023
  .venv/bin/qstock-scheduler index --trading-days 90 --verify
  ```

- **关键参数**：`--codes` 缺省为 7 大指数；可用 `--date`、`--start/--end` 或 `--trading-days` 选窗口；`--verify` 对照交易日历列缺口。
- **结果判断**：抓取模式为 `status="ok"`；核验模式逐项检查 `missing_days`，空列表才表示该指数窗口完整。
- **详细手册**：[指数日线 CLI](index-source-cli.md)。

#### `backfill`：申万板块历史回补

- **写库**：写 `market_boards` 并落台账。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler backfill --source sw --trading-days 90 --force
  .venv/bin/qstock-scheduler backfill --source sw
  ```

- **关键参数**：`--source sw` 为申万官网历史源；缺省从 1999 年起回补；`--trading-days` 限定交易日窗口；`--force` 忽略同窗口成功台账并整窗重抓，默认按板块断点续跑。
- **结果判断**：关注 `status`、逐板块结果和是否出现熔断；完成后用 `coverage` 验收窗口。
- **详细手册**：[申万板块 CLI](sw-boards-cli.md)。

#### `lhb`：龙虎榜单日抓取、历史回溯与对拍

- **写库**：单日及历史模式写库并落台账；`--check` 不写库。历史模式只写 basic 与滚动统计，不写两个营业部累计榜。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler lhb --date 20261023
  .venv/bin/qstock-scheduler lhb --trading-days 90
  .venv/bin/qstock-scheduler lhb --date 20261023 --check
  ```

- **关键参数**：`--date` 用于单日；`--trading-days` 用于历史窗口；`--check` 使用新浪数据做互补校验。官方单市场失败时可在历史命令显式加 `--fallback ths` 启用同花顺市场级后备；默认关闭，单日与对拍不适用，真实官方对拍通过前不会进入默认链。
- **结果判断**：关注 `status`、逐日 `results` 和 `empty_days`；正常空日不是失败，部分失败不能当作窗口已完成。启用后备时还要核对 `source` 与 `source_detail`，确认哪些市场实际由同花顺补齐。
- **详细手册**：[龙虎榜 CLI](lhb-source-cli.md)。

#### `coverage`：历史分析窗口统一覆盖核验

- **写库**：否，只读数据库，不触发抓取或查询自愈。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler coverage --trading-days 90
  .venv/bin/qstock-scheduler coverage --start 20260601 --end 20261023
  ```

- **关键参数**：使用 `--trading-days`，或成对使用 `--start/--end`；可用 `--end` 配合 `--trading-days` 指定截止日。
- **结果判断**：`status="ok"` 且 `summary.gaps=0` 才通过；`missing_days` 和 `status="gap"` 用于定位缺口。发现缺口时退出码 1。
- **详细说明**：核验个股、7 大指数、申万板块和龙虎榜；快速引导第 8 步给出完整使用顺序。

### 日常采集

#### `morning`：次晨全市场日线与指数补采

- **写库**：是，写个股日线、指数日线、申万板块估值数据并落台账。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler morning
  .venv/bin/qstock-scheduler morning --date 20261023
  ```

- **关键参数**：`--date` 指定目标交易日；缺省为前一交易日，不要用它补当日尚未发布的数据。
- **结果判断**：检查 `status` 和 `failed_parts`；任一部分失败时按输出定位后重跑。
- **详细手册**：[调度配置说明](scheduler-launchd.md)。

#### `close`：收盘后盘面五 section 采集

- **写库**：是，采集指数、板块、涨停池、强势股和龙虎榜盘面数据并落台账。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler close
  .venv/bin/qstock-scheduler close --date 20261023
  ```

- **关键参数**：`--date` 缺省当日；建议在交易日收盘数据就绪后执行。实时 section 不能靠指定历史日期恢复。
- **结果判断**：检查 `status`、`failed_sections` 和各 section 明细；正常 `empty` 与上游故障的含义不同。
- **详细手册**：[调度配置说明](scheduler-launchd.md)。

#### `snapshot`：全市场快照抓取与三源对拍

- **写库**：默认写 `market_snapshot` 并落台账；`--check` 不写库。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler snapshot --date 20261023
  .venv/bin/qstock-scheduler snapshot --date 20261023 --check --limit 20
  ```

- **关键参数**：`--date` 缺省由上游报告日期或当日回退；`--check` 对拍三源；`--limit` 只限制对拍抽样数。
- **结果判断**：抓取模式关注 `status`、`rows_upserted` 和实际 `trade_date`；对拍差异见 `diffs`。历史快照无法事后补抓。
- **详细手册**：[全市场快照 CLI](snapshot-source-cli.md)。

#### `sw-boards`：申万板块实时快照与三方位对拍

- **写库**：默认写 `market_boards` 并落台账；`--check` 不写库。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler sw-boards --date 20261023
  .venv/bin/qstock-scheduler sw-boards --date 20261023 --check --level all
  ```

- **关键参数**：`--check` 比较官网、库内快照与库内自算结果；`--level` 可选 `1/2/all`；`--limit` 只限制对拍数量。
- **结果判断**：抓取模式检查 `status` 和 boards section；对拍差异是核验结果，具体语义见专项手册。
- **详细手册**：[申万板块 CLI](sw-boards-cli.md)。

#### `sw-dict`：在线刷新申万成分字典

- **写库**：是，以申万官网分类文件为准清空重载 `sw` 体系。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler sw-dict
  .venv/bin/qstock-scheduler sw-dict --url '<分类 xls 直链>'
  ```

- **关键参数**：`--url` 指定分类 xls 直链，缺省使用申万官网文件。这是成分字典维护命令，不是板块行情回补命令。
- **结果判断**：检查 `status` 和导入结果；失败时不要继续依赖新字典做自算板块验收。
- **详细手册**：[申万板块 CLI](sw-boards-cli.md)。

#### `limitpool`：涨停池采集与强势股自算

- **写库**：默认写涨停池、强势股并落台账；`--probe` 不写库。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler limitpool --date 20261023
  .venv/bin/qstock-scheduler limitpool --probe
  ```

- **关键参数**：`--date` 缺省当日；`--probe` 只做同花顺封禁探测。遵守每日每池一次的低频纪律，被封禁时不要反复重试。
- **结果判断**：检查 `status`、各池结果和 `failed_sections`；历史深度有限，不能用本命令承诺补齐 90 日涨停池。
- **详细手册**：[涨停池 CLI](limitpool-cli.md)。

### 巡检与质量核验

#### `fundamentals`：基本面透传链路巡检

- **写库**：否，不规格化、不落库，也不需要 `PG_DSN`。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler fundamentals --codes 600519,000001
  ```

- **关键参数**：`--codes` 必填，逗号分隔股票代码。
- **结果判断**：检查整体 `status`、各 section 是否可得，以及逐源 `errors`；单 section 失败应按输出定位，不把透传结果当作落库结果。
- **详细说明**：该命令用于代表股真实拉取和链路健康巡检，不是首次历史回补步骤。

#### `parity`：五核心面总验收对拍与历史连续性检查

- **写库**：否，只读数据库、直取上游并做 THS 页面探测。
- **常用命令**：

  ```bash
  .venv/bin/qstock-scheduler parity --date 20261023
  .venv/bin/qstock-scheduler parity --faces snapshot,indices --limit 20 --date 20261023
  .venv/bin/qstock-scheduler parity --no-probe --date 20261023
  ```

- **关键参数**：`--faces` 选择数据面；`--window` 设置连续性检查的自然日窗口；`--limit` 控制快照/板块抽样；`--no-probe` 跳过 THS 探测。
- **结果判断**：分别查看 `parity_summary` 与 `continuity_summary`。对拍差异、不可对拍和历史断档是验收结果，不都等同于命令执行失败；退出码语义以专项手册为准。
- **详细手册**：[总验收对拍 CLI](parity-cli.md)。
