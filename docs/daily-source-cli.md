# 个股日线固定入口操作手册（`qstock-scheduler daily`）

issue #112 提供三源对拍；issue #167 将写库模式改为 **raw + 累计 B + 派生重建**。
本手册是固定 CLI 入口说明；不使用一次性临时脚本。

## 适用场景

| 场景 | 命令要点 |
| --- | --- |
| 单票收盘后补当日行（T+0） | `daily --codes 600519 --date <当日>` |
| 单票修复历史窗口 | `daily --codes 600519 --start <起> --end <止>` |
| 显式全市场整窗重刷 | `daily --all --trading-days 90`；`--all` 本身即强制重刷 |
| 三源对拍 | `daily --codes ... --date <交易日> --check`（不写库） |
| 全量回溯 | `init_database(backfill_history=True)`，复用同一逐日全市场管线 |

## 前置条件

- 已完成 editable 安装：`.venv/bin/pip install -e ".[sources,dev]"`。
- 写库模式的 `PG_DSN` 指向目标库，且 schema 已初始化。
- baostock 遵守实例租约：CLI 首次实际使用时自动 acquire，命令结束释放；锁冲突返回
  `baostock_locked`，不静默排队或换源。MCP 仍须 agent 获得用户确认后显式 acquire。

## 命令

```bash
# 单票 raw + 因子 + 派生重建
qstock-scheduler daily --codes 600519,000001 --date 20261020
qstock-scheduler daily --codes 600519 --start 20261001 --end 20261020

# 显式全市场整窗重刷；逐日全市场 raw + 因子，不逐股请求
qstock-scheduler daily --all --date 20261020 --throttle 1.5

# 三源对拍：逐源直取、字段级比较，不写库
qstock-scheduler daily --codes 600519,000001 --date 20261020 --check
qstock-scheduler daily --codes 600519 --date 20261020 --adj hfq --check
```

参数说明：

- `--codes` 与 `--all` 互斥；`--all` 仍要求库内 `stock_list` 非空。
- `--date` 与 `--start/--end/--trading-days` 互斥；都不传时缺省当日。
- 单票默认只补 raw 头尾缺口，并补齐窗口内缺失的逐日因子证据；`--force` 整窗重抓。
  baostock 优先；其单票 raw 失败或暂未返回当日行时，可尝试腾讯/新浪补单票 raw。
- `--all` 是显式全量重刷：固定复用 `run_adjustment_backfill`，逐日调用全市场因子与
  raw 接口，并额外取得窗口外 D-1 输入。baostock 整体不可用时失败并落台账，
  **不自动执行全市场腾讯/新浪回填**。`--force` 对 `--all` 是冗余参数。
- `--all --limit N` 只限制事件票的本地重建，不减少按日全市场请求；有限样本会标记
  `limited` 并按 `partial_error` 报告，不能当作全市场验收通过。
- 写库模式的 `--adj` 仅为兼容保留并原样回显：无论传 `qfq/hfq/none`，都写 raw+B，
  不再直接写对应口径的 `stock_daily` 行。`--adj` 只在 `--check` 时选择对拍口径。
- `--throttle` 单票模式控制逐 code 的调用间隔（缺省 0），不控制单票内部的逐日因子请求；
  `--all` 由全市场管线固定串行，缺省相邻 baostock 请求至少 1.5 秒，显式值覆盖缺省。
  单票窗口越长，逐日因子请求数按交易日数增长，调用前须按数据源预算与最短间隔评估。
  单请求 60 秒硬超时、会话与预算由 baostock 公共层承载。
- `--check` 逐源直取结束日 bar，不落库。hfq 只比成交量/成交额/换手率；qfq/none
  按既有字段规则比对。

## 输出与退出码

单票模式保留逐股 `results`：关注 `status`、`source`、`rows_upserted`、`data_range`、
`segments` 与 `adjust.coverage`。无 raw 缺口且因子证据已成功时零上游请求，
`source=null`、`rows_upserted=0`。

`--all` 输出 `stage`、`summary` 和 `artifacts`；请求数、断点命中、失败日、pending、
修复项和窗口行数在 `summary` 中。只有 `status=ok` 才是完整成功；`partial_error` 和
`error` 均退出 1。输入非法退出 1，argparse 参数错误退出 2。

因子请求失败与成功空结果严格区分：失败会落 `stock_adjust_factor/error` 台账并令
state pending，不能据此判 complete。替代源 raw 缺少权威 pctChg 时同样保持 partial；
无可信基线时不生成 hfq/qfq。次晨 `morning` 的 baostock raw+因子可修订这些 T+0 行，
并重建受影响派生。

## 可信 B0 与重建权限

`QSTOCK_ADJ_WINDOW_START` 定义的采集窗口才能建立 B0：init 全量回溯、
`adj-backfill` 和 `daily --all` 复用该路径，但只有 `start <= QSTOCK_ADJ_WINDOW_START`
且事件集完整时才实际建锚；窄窗回填不获得建锚权限。morning 仅新股上市首日可初始化。
单票 daily 与 T+0 查询只消费 B0，不根据查询窗无事件记录假定 B0=1，也不从查询窗
首事件反推。无锚时仅写 raw 与因子证据，baseline/F_latest 为 NULL 且 pending，
不生成或改写 hfq/qfq。

带锚 daily 只从 `rebuild_from` 起做 0.1% 对账，mismatch/unverified 进入
`stock_factor_repair`。窄窗 daily 不清全量采集留下的 pending；腾讯/新浪 raw 可由后续
baostock 覆盖升级并修订派生。morning 始终消费已有锚，只有新股首日例外初始化。

## 数据与审计口径

- 写 `stock_daily_raw`、`stock_adjust_factor`、`stock_daily_hfq`、`stock_factor_state`；
  qfq 只按最新锚点在读取时派生，不物化。
- raw 与因子按业务键幂等 upsert；amount 由 baostock 适配层从元除以 10000 为万元，
  volume 为股。raw/因子重跑与派生重建幂等。
- `stock_daily_raw`、`stock_adjust_factor` 与 `stock_factor_repair` 台账保留采集、
  断点、异常和修复证据；采集台账 append-only。
- 本管线不写 legacy：正常期不双写；回滚期间仍持续写 raw+B，legacy 修复统一由
  `adj-switch rollback` 完成，不启动第二套采集器。

## 备份与还原

`--all` 会覆盖窗内事实并重建派生。运行前记录目标库、窗口、当前最大日期、预计请求数、
写入表和耗时上限，并备份四张表：

```bash
pg_dump "$PG_DSN" \
  -t stock_daily_raw -t stock_adjust_factor \
  -t stock_daily_hfq -t stock_factor_state \
  > adjustment_before_$(date +%Y%m%d%H%M).sql
```

还原先恢复备份，再按同一窗口重跑并检查 summary/artifacts；不要用旧 qfq 反推 raw。
详细断点、修复和产物说明见 [`adj-backfill-cli.md`](adj-backfill-cli.md)，全局回滚见
[`adj-switch-cli.md`](adj-switch-cli.md)。生产切读仍归 #184，本命令完成不等于已切读。

## 验证

```bash
.venv/bin/pytest tests/test_fetch_daily.py tests/test_query_daily_adjustment.py \
  tests/test_scheduler.py tests/test_init_full.py tests/test_morning_batch.py
.venv/bin/pytest tests/
```

`--check` 仍是独立的上游对拍入口，不替代 raw+B 写入、派生重建或迁移硬验收。
