# 复权全局切读与回滚：`qstock-scheduler adj-switch`

Issue [#166](https://github.com/daanZao/q_stock_skills/issues/166)，spec [#158](https://github.com/daanZao/q_stock_skills/issues/158) §2.5、§6.3、§7.4。实现位于 `src/qstock_mcp/adjustment_switch.py`；抽样测试方案见 [adj-switch-test-plan.md](adj-switch-test-plan.md)。

## 适用场景与门禁

固定入口用于：

- 查看复权读取模式；
- 将旧 `stock_daily` 改名为 `stock_daily_legacy` 并切换 new 读取；
- 立即回滚，或在新事实已推进时修复 legacy 后回滚；
- roll-forward 再次切回 new，保留并复用新事实，不重抓上游。

**#166 只允许在 `qstock_test` 做小规模演练。真实生产切读归 #184，必须等待 #166、#167、#183 全部完成，并单独记录生产预检、备份恢复与执行结果。** 本命令的可用性不代表生产数据已验收；八项硬验收未全绿时不得据此执行生产切换。

所有动作零上游请求、无需 baostock 租约。表改名、状态更新和修复在同一个数据库事务中完成；失败时事务回滚并返回 `status=error`。

## 前置检查

1. `PG_DSN` 明确指向目标库；开发演练只能是 `qstock_test`。
2. 确认四张复权表存在且已有代表性事实：`stock_daily_raw`、`stock_adjust_factor`、`stock_daily_hfq`、`stock_factor_state`。
3. 生产执行前另按 #184 记录目标库、窗口、最大日期、断点、写入表、耗时上限和备份/恢复方式；预检不符即停止。非 `qstock_test*` 写动作默认以 `production_not_enabled` 拒绝，只有 #184 预检完成后才可显式设置 `QSTOCK_ADJ_SWITCH_ENABLE_PRODUCTION=true`；该开关不替代任何验收。
4. cutover 后必须把读取环境设为 `QSTOCK_PRICE_SOURCE=new`；rollback 后设为 `legacy`。命令在 `required_env` 中回显所需值，但不会修改调用进程之外的持久环境配置。
5. new 模式下 morning 继续写 raw/因子并停止 legacy 双写。daily/T+0/init 的持续采集改造归 #167，未完成前不得生产切读。

## 命令

```bash
# 查看当前持久化模式（不写业务表、不生成工件）
PG_DSN=postgresql://localhost/qstock_test \
  .venv/bin/qstock-scheduler adj-switch status

# 开发演练：old -> stock_daily_legacy，目标模式 new
PG_DSN=postgresql://localhost/qstock_test \
  .venv/bin/qstock-scheduler adj-switch cutover \
  --report-dir files/adj-switch-smoke/cutover
export QSTOCK_PRICE_SOURCE=new

# 回滚：事实水位未变时直接还原；已推进时自动修复 legacy
PG_DSN=postgresql://localhost/qstock_test \
  .venv/bin/qstock-scheduler adj-switch rollback \
  --report-dir files/adj-switch-smoke/rollback
export QSTOCK_PRICE_SOURCE=legacy

# 再次切回 new：新事实表保留，零上游请求
PG_DSN=postgresql://localhost/qstock_test \
  .venv/bin/qstock-scheduler adj-switch roll-forward \
  --report-dir files/adj-switch-smoke/roll-forward
export QSTOCK_PRICE_SOURCE=new
```

参数：

| 参数 | 说明 |
| --- | --- |
| `action` | `status` / `cutover` / `rollback` / `roll-forward` |
| `--report-dir` | 写动作的执行工件目录；缺省 `files/adj-switch-<action>_<UTC 时间>`，status 不写工件 |

## 行为与产物

- `cutover` / `roll-forward`：要求当前为 legacy、`stock_daily` 存在且不存在同名归档冲突。记录当前四表事实水位，再改名并持久化 `mode=new`。切换与 `rebuild_stock` 共用事务咨询锁，并发写入会串行等待，不能绕过水位。
- `rollback`：要求当前为 new 且归档表存在。事实水位未变时 `repair_required=false`，修复计数均为 0；否则把归档表恢复为 `stock_daily`，从持续保留的 raw/hfq/state 幂等修复 `none`、`hfq`、`qfq`。
- 修复覆盖新事件造成的历史 qfq 重锚，不只补切换后的缺失日期。缺少有效锚点的 pending 股票会删除新窗口陈旧 qfq，不伪造新值；提交前验证 `missing_none/missing_hfq/missing_qfq/stale_qfq` 全为 0，否则整个事务回滚。
- roll-forward 不删除或重抓新事实；回滚期间仍可继续 raw/因子采集。
- `summary.json` 记录模式、所需环境、零上游请求、修复标记和各口径修复行数。退出码 0 表示动作成功，1 表示预检或执行失败，2 表示 CLI 参数错误。

## 验收清单

- 全局 new 读取覆盖 `query_daily`、强势股、申万、coverage、最新日期与窗口极值。
- legacy-only、跨边界和窗口外行为符合 spec；旧表归档后 legacy 全局读取仍可用。
- 重复 `ensure_schema` 不重建空 `stock_daily`。
- new 模式 morning 不双写 legacy，raw/因子持续。
- 分别演练立即回滚、运行后修复回滚和 roll-forward；修复计数与实际行数一致，所有动作 `upstream_requests=0`。

## 故障还原

数据库事务内失败会整体回滚；若数据库已提交但本地工件写入失败，返回 `partial_error`、`committed=true`，不得按“未切换”处理。两种情况都先执行 `adj-switch status` 并检查 `stock_daily` / `stock_daily_legacy` 的实际状态，不要手工删表或重跑全量回填。

若动作成功但进程环境尚未切换，按结果中的 `required_env` 修正后重试读取。需要还原 legacy 时使用 `rollback`；需要再次进入 new 时使用 `roll-forward`。新表在两个方向都保留，不能以删除新表作为回滚方式。工件失败时可在修复本地目录/权限后补记实际状态，但不要重复执行已提交的数据库动作。
