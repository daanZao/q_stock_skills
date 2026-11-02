# limitpool CLI 手册：THS 涨停池抓取 + 强势股库内自算（issue #120）

`qstock-scheduler limitpool` 是 zt_pool（涨停/跌停/炸板三池）与
strong_stocks 的固定采集入口（spec #111 交付形态：一次开发、可重复运行、
可挂定时任务）。实现与口径决策见 `docs/adr/0012`。

## 适用场景

- 每日盘后采集三池 + 自算强势股（收盘后批 `close` 已内联同一逻辑；本命令
  只跑这两个 section，适合单独补跑/巡检）。
- 迁移验收后的日常巡检：`--probe` 探测同花顺封禁状态（单次页面请求，
  不消耗 dataapi 配额）。
- **不适用于**：盘中刷新（THS 低频纪律每日每池一次，当日重复执行自动跳过）；
  历史回补深度超过数日的场景（THS 回溯深度未验证，实测 4 天可取）。

## 执行命令

```bash
export PG_DSN=postgresql://localhost/qstock        # 业务库连接串（ADR 0002）

# 每日盘后：三池抓取落库 + strong_stocks 库内自算（约 40s：3 请求 × 池间 20s 退避）
qstock-scheduler limitpool --date 20261023

# 封禁探测：不打 dataapi，返回 reachable/banned（banned 或不可达 exit 1）
qstock-scheduler limitpool --probe
```

- `--date` 缺省当日；非交易日执行产出 empty 台账（正常语义）。
- **建议执行时机**：19:30 之后——strong_stocks 自算需要 stock_daily 当日行
  （腾讯/新浪 T+0，见 README 时间线）；早于日线入口运行时 strong_stocks 按
  no_data_for_date 跳过（非故障），补完日线重跑即可。
- 退出码：0 = 全部成功（含 empty 无数据）；1 = 任一段失败（如 THS 封禁
  upstream_unreachable，当日勿重试，等次日）；2 = 参数错误。

## 低频纪律（THS 风控极敏感，违反即 IP 级封禁）

- 每日每池一次：当日台账已有 ok/empty 行即跳过上游请求；失败（error）不
  在守卫之列，允许修复后当日重跑。
- 池间/分页间默认 20s 长退避（环境变量 `THS_POOL_PAUSE_S` 可覆盖，仅运维
  调试用，勿调小）。
- 封禁命中（HTTP 403 / Nginx forbidden 页）：本轮剩余池放弃，按
  upstream_unreachable 上报并触发核心 section 中止线；**今日勿再重试**
  （实测恢复 >5 分钟，恢复点未测到），用 `--probe` 确认解封后再跑。

## 数据库变更脚本与回滚（AGENTS.md 数据库纪律）

迁移脚本 `src/qstock_mcp/sql/017_zt_pool_ths_columns.sql`（幂等，
ADD COLUMN IF NOT EXISTS）：zt_pool 加 `sealed_amount`（封单额，万元，
ADR 0006）与 `reason_type`（涨停原因题材）两列。随 `ensure_schema` 在下次
任意工具调用时自动应用于现库，无需手工执行；手工执行亦可重复运行：

```bash
psql "$PG_DSN" -f src/qstock_mcp/sql/017_zt_pool_ths_columns.sql
```

回滚（幂等，可重复执行）：

```sql
ALTER TABLE zt_pool DROP COLUMN IF EXISTS sealed_amount;
ALTER TABLE zt_pool DROP COLUMN IF EXISTS reason_type;
```

注意：回滚仅在新列无消费方（limit-up-analysis 龙头点评/归因）后执行；
`ZT_POOL_FIELDS` 含两列时旧代码写库会因列缺失报错，回滚须与代码版本配套。

## 定时任务示例（launchd/cron，用户自行安装）

```cron
# 每日 19:35（盘后：THS 数据就绪 + 个股日线 T+0 已入库）
35 19 * * 1-5  /path/to/venv/bin/qstock-scheduler limitpool >> /path/to/log/limitpool.log 2>&1
```

非交易日触发产生 empty 台账行（正常语义）。调度安装样例另见
`docs/scheduler-launchd.md`。
