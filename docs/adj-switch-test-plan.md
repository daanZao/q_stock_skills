# 复权全局切读抽样测试方案

Issue [#166](https://github.com/daanZao/q_stock_skills/issues/166)。本方案只使用小规模代表性样本，目标是 **100% 覆盖约定的公开接口、边界条件和状态转换**，不是复制全市场数据，也不代替 #183 的生产八项完整复验。

## 测试接缝

- `price_reader`：new/legacy 的区间行、覆盖日期、最新日期、横截面口径、窗口锚点、当日截面、窗口极值和申万聚合。
- `tools_*` 核心层：`query_daily`、`compute_strong_rows`、`query_sw_board_daily`、`verify_history_coverage`、`fetch_morning_batch` 的公开输出。
- `adjustment_switch.run_adjustment_switch` 与 `qstock-scheduler adj-switch`：结构化输出、表名状态、修复计数和零上游请求。
- 所有数据库案例仅使用 `qstock_test`，不触生产库；复权计算案例不请求上游。

## 样本与行为矩阵

| 风险/行为 | 小规模案例 | 公开断言 | 主要用例 |
| --- | --- | --- | --- |
| 普通送转与 qfq 重锚 | 两日 raw，事件日 B=2 | qfq 由 hfq/F_latest 派生，事件前后值确定 | `test_new_qfq_is_derived_from_hfq_and_latest_anchor` |
| 强势股窗口与当日截面 | 一股两日，含一次事件 | new 下窗口锚点、当日行、窗口最高值和 change/amount 正确 | `test_new_reader_supports_strong_stock_window_and_cross_section` |
| 申万自算与最新日期 | 两股同一行业，不同 pct/amount | 缺省取 new 最新日，成交额加权值与计数正确 | `test_new_reader_supports_sw_board_latest_date_and_aggregation` |
| coverage 与 pending | raw 日期完整、state pending | 不因日期齐全判 complete；返回 partial + gap + note | `test_new_coverage_rejects_pending_even_when_raw_dates_are_complete` |
| 事件修订/撤销 | 单票事件邻域 | 受影响区间重建，撤销后无陈旧事件/派生 | `test_authoritative_revision_and_retraction_rebuild_derived_rows`、回填对应用例 |
| 长停牌/恢复上市与反向事件 | 600094、600381 录制向量 | 事件仍登记，F 可下降，不设单调约束 | `test_600381_reverse_event_on_suspended_day`、`test_600094_resume_after_long_suspension` |
| 缺 D-1 / 缺 pctChg | 首事件缺基线证据 | 不猜 B0，state pending，coverage=partial | `test_window_backfill_missing_d1_keeps_state_null_and_pending`、`test_window_backfill_audit_flags_missing_pctchg_as_unverified` |
| invalid close | 非正 close raw | 保留 raw 审计，不派生 hfq/qfq | `test_invalid_close_is_quarantined_and_not_derived` |
| 无事件股票 | 两日 raw、空事件集 | B0=1、F=1，正常派生 | 全局 reader 与 morning 小样本 |
| 2023 legacy 边界 | 2022 legacy qfq + 2024 new raw | legacy-only 可读，跨边界拒绝，窗口外不反推 | `test_cutover_schema_idempotency_legacy_boundary_and_immediate_rollback`、`test_new_reader_rejects_mixed_qfq_and_legacy_backfill` |
| 旧表归档后的全局 legacy 读取 | 两日 legacy + 申万成分 | 改名后强势股和申万仍读归档表 | `test_archived_legacy_table_supports_global_legacy_reads` |
| 立即回滚 | 切换后事实水位未变 | 恢复原表名，修复数为 0，新表保留 | `test_cutover_schema_idempotency_legacy_boundary_and_immediate_rollback` |
| 运行后修复回滚 | 切换后新增事件 | 修复 none/hfq/qfq，历史 qfq 重锚，不只追加日期 | `test_rollback_repairs_reanchored_legacy_and_roll_forward_keeps_facts` |
| roll-forward 与持续推进 | 回滚后再切 new，再增一日 | 零上游请求，新事实保留，再次回滚包含新日 | 同上 |
| 正常期不双写 | new 模式 morning 两行 raw | raw/state 继续写入，legacy_rows=0，归档表无新增 | `test_new_mode_keeps_fact_ingestion_without_legacy_double_write` |
| CLI 与 schema 幂等 | status + cutover 后重复 ensure_schema | 结构化输出正确，不重建空 stock_daily | `test_adj_switch_cli_status`、立即回滚案例 |
| 缺锚 pending 的修复回滚 | 旧 qfq 存在，切换后新事件使基线不可用 | 删除新窗口陈旧 qfq，不伪造新值；完整性验证四项为 0 | `test_rollback_removes_stale_qfq_when_anchor_becomes_unavailable` |
| 强势股缺派生 | raw 存在但 hfq/基线不可用 | 不选 raw 新高，返回空集 | `test_new_strong_stock_never_falls_back_to_raw_when_hfq_is_unavailable` |
| 生产门禁 | 非 qstock_test 连接 | 未显式开启生产开关时零写拒绝 | `test_non_test_database_requires_explicit_production_gate` |
| 并发序列化 | 双连接分别持锁执行 rebuild/cutover | 共用事务咨询锁，水位与修复无并发空洞 | `test_fact_write_and_switch_wait_for_the_same_lock` |
| 工件失败 | 数据库已提交、报告写入失败 | 返回 partial_error + committed=true，不谎报回滚 | `test_report_failure_does_not_misreport_committed_cutover` |

既有 Golden 与回填测试继续提供事件、单位和异常样本；本方案不复制其全量数据。任何一个约定接口或状态转换缺失，都不能以“抽样通过”关闭 #166。

## 执行

```bash
.venv/bin/pytest \
  tests/test_price_reader_global.py \
  tests/test_adjustment_switch.py \
  tests/test_adjustment_lock.py \
  tests/test_query_daily_adjustment.py \
  tests/test_adjustment.py \
  tests/test_morning_batch.py \
  tests/test_sw_board_daily.py \
  tests/test_price_reader.py -q
```

生产八项硬验收仍由 `docs/adj-acceptance-cli.md` 和 #183 承担；真实生产切读由 #184 单独预检、执行和记录。
