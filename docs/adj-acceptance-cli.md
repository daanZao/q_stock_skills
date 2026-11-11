# 复权迁移硬验收：`qstock-scheduler adj-acceptance`

Issue [#165](https://github.com/daanZao/q_stock_skills/issues/165)，spec [#158](https://github.com/daanZao/q_stock_skills/issues/158) §7。实现位于 `src/qstock_mcp/adjustment_acceptance.py`。

## 适用场景

在 #163/#164 回填与迁移审计之后、#166 切读之前，一条命令执行八项验收。命令复用迁移产物和库内快照，不重新请求全市场历史数据，不导出全量明细。

默认只读 PostgreSQL 并写本地报告，零上游请求、不申请 baostock 租约、不改四张复权表。只有显式 `--smoke-days 3|4|5` 才进行真实上游 smoke，由 CLI 命令级自动实例租约管理会话。

## 命令

```bash
# 本地七项 + smoke=not_run；整体不会因缺少 smoke 而判成功
PG_DSN=postgresql://localhost/qstock \
  .venv/bin/qstock-scheduler adj-acceptance \
  --migration-dir files/adj-backfill-164-production_20230101_20261104 \
  --report-dir files/adj-acceptance-165-production_20230101_20261104

# 一键覆盖八项：额外对最近 3 个连续交易日做真实上游只读 smoke
PG_DSN=postgresql://localhost/qstock \
  .venv/bin/qstock-scheduler adj-acceptance \
  --migration-dir files/adj-backfill-164-production_20230101_20261104 \
  --smoke-days 3 --smoke-end 20261104

# 隔离测试库
PG_DSN=postgresql://localhost/qstock_test \
  .venv/bin/qstock-scheduler adj-acceptance \
  --migration-dir files/adj-backfill_smoke_20261102_20261104
```

参数：

| 参数 | 说明 |
| --- | --- |
| `--migration-dir` | 必填；读取 `summary.json`、`events.csv`、`repairs.csv`、`stock_hashes.csv`、`raw_sample.csv`、`raw_days.csv`；可选 `legacy_diagnostic_review.csv` |
| `--smoke-days` | 仅允许 3/4/5；缺省不触网，smoke 状态为 `not_run` |
| `--smoke-end` | smoke 截止交易日；缺省最近已收盘交易日 |
| `--report-dir` | 报告目录；缺省 `<migration-dir>/acceptance` |
| `--throttle` | smoke 相邻请求间隔秒；缺省 1.5，串行 |

## 八项判据

1. `factor_baseline`：全事件检查 B、首事件 `B0=B1×f1`、D-1/raw/权威 pctChg，以及双路径相对误差 ≤0.1%。0.1% 不用于过滤事件。
2. `event_invariants`：按项目交易日历精确联查 D-1/D/D+1，检查 `F(D)×f(D)=F(D-1)`、`hfq=raw×F`，并调用真实 `NewPriceReader` 验证 qfq×F_latest；首窗口事件的窗外 D-1 以 B0/F=1 为计算输入。不检查 F 单调。
3. `structure_coverage`：集合 SQL 检查主键、行数、股票数、日期与逐日 raw 行数、事件—raw/hfq/state 对齐、F_latest 锚点、非 pending 股票派生覆盖和 state 空值矩阵。NULL/NULL 必须 pending，且用真实 reader 断言 coverage=partial；有效值+pending 也阻断。
4. `indicator_regression`：Python 独立滚动预期与 SQL 窗口结果互验 60 日新高和 52 周高低；688300 另有固定 Golden 指标值。全市场初始样本必须 100+100、每股 5 日、固定种子 164 并覆盖全集已有 strata；命令会按种子重算并比对样本成员，不只信摘要。小全集按实际类别数验收。任一普通样本或 legacy 诊断失败自动扩大为同类别全检，不换随机样本。
5. `upstream_smoke`：仅 3–5 个连续交易日；逐日 raw + 因子真实请求，检查日期、raw 口径、重复代码、正因子及量/额非负。它不能单独证明迁移或派生正确。
6. `event_reconciliation`：本地事件表与 `events.csv` 按股票、日期、B、来源全量对账；当前对账异常和事件集不完整均阻断。
7. `golden_set`：内置 20 个真实边界代码，必须含 688300、600381、600094、600519、301569、603786、603336，以及停牌、退市、北交所、新股、无效价格和对账异常。只查事件邻域与边界日期；2023 年前案例使用已录制的一手向量。`adjustFacto(r)` 只作审计值，相同 B 日期必须不作为新事件。
8. `hash_idempotency`：当前四表与迁移目录基线逐股逐表比较行数和业务列摘要哈希，并要求迁移摘要为 0 上游请求的续跑结果；另在 `pg_temp` 临时表中运行真实 raw 修订、B 修订、事件撤销与重复重建场景，结束即删除，不写业务表。

新旧 qfq 与 52 周高低只输出抽样诊断，不设硬 parity；相对差异 >1% 时必须在迁移目录提供 `legacy_diagnostic_review.csv`（stock_code、trade_date、event_date、reason）完成逐事件人工归因，否则阻断。Golden 不占普通行情 100 只有事件 + 100 只无事件股票的抽样名额。

## 产物与退出码

| 文件 | 内容 |
| --- | --- |
| `acceptance_summary.json` | 八项状态、指标、失败定位、上游计数、耗时与性能预算 |
| `acceptance_failures.csv` | 仅失败项，含 check、股票、日期、字段和事件日期 |
| `indicator_diagnostics.csv` | 仅显著新旧 qfq 指标诊断及最近事件归因 |

只有八项全部 `ok` 才返回 `status=ok`、`stage=verified`、「数据可以使用」和退出码 0。其余情况退出码 1；默认缺少 smoke、任何 pending、覆盖缺口、哈希差异或未解释显著差异都不能视为可切换。

命令与哈希检查都记录实际耗时。性能预算初设 600 秒，依据是 #164 同机全量本地审计实测 432.279 秒。2026-11-11 对生产窗口 20230101–20261104 的最终零上游验收（4,746,054 行窗口 raw、真实 reader 全检与固定种子重算）运行 344.296 秒，其中哈希与修订场景 9.760 秒；结果为 `partial_error`，只证明当次本机样本，不构成长期性能或上游容量承诺。后续应结合实测复核预算。

2026-11-11 真实上游 smoke：隔离 `qstock_test` 小样本，20261102–20261104 连续 3 日，逐日 `query_daily_adjust_factor` + `query_daily_history_k_AStock`，串行、相邻请求 1.5 秒、单请求 60 秒超时；6 次 query 全部成功，量股/额万元检查通过，smoke 耗时 255.287 秒，无空日/失败日，租约正常释放。可控失败与超时分支由 fake 测试覆盖；该样本不推断长期 TPS 或并发上限。

## 回滚

本命令不改业务表，无需数据库回滚。删除本次 `--report-dir` 即可移除本地验收报告；不要删除作为输入的迁移审计目录。
