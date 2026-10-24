# 指数日线固定入口操作手册（`qstock-scheduler index`）

issue #117（spec #111）：指数快照与指数日线迁移腾讯承载——指数快照走腾讯
`qt.gtimg.cn` 批量报价（唯一覆盖**量比**字段的源，含科创50），fallback 新浪
批量报价；指数日线走腾讯 K 线薄封装（收盘后当晚含当日行 T+0，覆盖科创50），
baostock 保留为次晨权威复核。东财口径（efinance 沪深系列指数 / akshare 沪深
重要指数 / index_zh_a_hist）退出指数面。本手册是该能力的固定 CLI 入口说明——
断档回填、当日行补采、覆盖核验都走同一入口，可重复运行、可挂定时任务；
不使用一次性临时脚本。

## 适用场景

| 场景 | 命令要点 |
| --- | --- |
| 科创50（000688）断档回填（如 2026-10-13 起缺口） | `index --codes 000688 --start <缺口首日> --end <截止日>` |
| 收盘后当晚补指数当日行（T+0） | `index --date <当日>`（缺省代码 = 7 大指数） |
| 复盘前覆盖核验（主要指数清单逐一对照） | `index --verify [--start D --end D]`（不写库） |
| 换源后数据质量抽查 | `index --codes 000001 --start D --end D` 后 `query_index_daily` 读数与行情软件对照 |

## 前置条件

- 已完成 editable 安装：`.venv/bin/pip install -e ".[sources,dev]"`；
- 环境变量 `PG_DSN` 指向目标库（写库模式必需；`--verify` 也需连库读数）；
- 数据库表已初始化（`init_database` 或此前已运行过任一写库批）。

## 命令

```bash
# 科创50 断档回填（2026-10-13 起缺口，回填写例；幂等可重跑）
qstock-scheduler index --codes 000688 --start 20261013 --end 20261023

# 收盘后当晚补 7 大指数当日行（T+0）
qstock-scheduler index --date 20261023

# 覆盖核验：逐指数列库内缺口（对照 A 股交易日历），不写库
qstock-scheduler index --start 20261013 --end 20261023 --verify

# 自定义区间与代码（逗号分隔；缺省 = 7 大指数白名单）
qstock-scheduler index --codes 000001,399006 --start 20261001 --end 20261023
```

参数说明：

- `--codes`：逗号分隔指数代码；缺省 = `MAJOR_INDICES` 7 大指数白名单
  （000001 上证指数 / 399001 深证成指 / 399006 创业板指 / 000300 沪深300 /
  000016 上证50 / 000905 中证500 / 000688 科创50）。
- `--date` 与 `--start/--end` 互斥；都不传时缺省当日。
- `--verify`：覆盖核验模式——逐指数读库内区间行数/首末日期/来源分布，并对照
  `dates.is_trading_day` 交易日历列示窗口内缺失交易日；纯读不写库。
  窗口缺省 = 近 31 自然日。退出码 1 表示存在 gap（`missing_days` 即回填命令的
  `--start` 线索）。

## 输出与退出码

stdout 只输出一份 JSON（日志走 stderr）：

```json
{"status":"ok","job":"index","mode":"fetch","range":{"start":"20261013","end":"20261023"},
 "indices":[{"index_code":"000688","status":"ok","rows":9,"source":"tencent"}]}
```

- `mode="fetch"`：`status` 为 `ok` / `partial_error` / `error`（逐指数独立成败，
  单指数失败不拖垮其他）；每指数带实际命中源 `source`（`tencent`/`baostock`）。
  盘中（交易时段 09:30–15:00）执行时当日行不落库，`deferred` 计数进报告
  （issue #78 盘中禁写，未收盘数据非最终值）。
- `mode="verify"`：`status` 为 `ok` / `partial_error` / `error`；每指数带
  `rows`/`first_date`/`last_date`/`sources`/`missing_days`/`status`
  （`ok`=窗口内交易日历全覆盖，`gap`=有缺口）。
- 退出码：0 = 全部成功；1 = 有失败/有 gap/输入非法；2 = 参数错误（argparse）。

## 数据口径

- **T+0**：腾讯 K 线收盘后当晚返回当日行（#117 实测 10-23 行 16:15 已可取）；
  baostock 为 T+1，保留次晨批权威复核（`morning` 子命令指数尾行补漏经
  腾讯→baostock 链，两源经 `index_daily.source` 列并存对账）。
- **量比（volume_ratio）**：仅腾讯快照源提供（`market_indices` 列）；腾讯批量
  报价量比字段位为响应 [49]（2026-10-23 收盘数据复核；注意 [38] 是换手率，
  调研报告 2026-10-20-index-alternatives.md §2.1 曾误标为量比，已修正）。
  新浪快照无量比（置 NULL，不伪造）。
- **单位**：`index_daily.volume` 入库存股（腾讯指数成交量为手 ×100，ADR 0008）；
  `amount` 入库万元（腾讯原生万元，ADR 0006）；`turnover_rate` 为腾讯原生
  百分数列（指数换手率口径意义有限，baostock 路径同为原生 turn 列）。
- **科创50（000688）**：baostock 实测返回 0 行（error_code=0），链对 000688
  等于空转——腾讯主源是首个覆盖 000688 的日线源；断档回填必须走本入口。
- **推算字段**：腾讯无 change_percent/change_amount/amplitude 列，按前收推算；
  区间首行前收随分段回溯取得（上市首日/区间首日前无行则置 NULL，不伪造）。
- **落库**：按业务键 `(index_code, trade_date)` 幂等 upsert，每指数落一条
  `collection_log`（section=`index_daily`，detail 记区间与 deferred）。

## 回滚与还原

CLI 只写 `index_daily`（业务键 upsert）与 append-only 台账；无 schema 变更。
**upsert 会把被覆盖行的 `source` 改成本次命中源**，不能仅凭 `source` 判断
原写入方。写库前先备份：

```bash
pg_dump -t index_daily <db> > index_daily_before_$(date +%Y%m%d%H%M).sql
```

回滚分两类：

```sql
-- 1) 清理本次**新增**的行（未发生过 upsert 覆盖：created_at = updated_at）
DELETE FROM index_daily
 WHERE index_code = '000688'
   AND trade_date BETWEEN '20261013' AND '20261023'
   AND source = 'tencent'
   AND created_at = updated_at;

-- 单日复跑覆盖同理：trade_date = '<当日>'
```

- 本次**覆盖**的旧行无法从库内反推原值：从 `pg_dump` 备份恢复到临时表后按
  `(index_code, trade_date)` 回写，或用目标源重跑覆盖（upsert 幂等）。
- 台账 `collection_log` 为审计表，append-only，不回滚。
- 只想换回旧源数据时，按业务键删除后由 `query_index_daily` 自愈不触发
  （纯读不补抓）——须用目标源重跑本入口。

## 定时任务示例（launchd）

收盘后 19:30 补 7 大指数当日行（示例；路径按实际仓库替换）：

```xml
<key>ProgramArguments</key>
<array>
  <string>/path/to/q_stock_skills/.venv/bin/qstock-scheduler</string>
  <string>index</string>
</array>
<key>StartCalendarInterval</key>
<dict><key>Hour</key><integer>19</integer><key>Minute</key><integer>30</integer></dict>
```

不传 `--date` 时由 CLI 取当日（目标日恒为当日 yyyymmdd）。更完整的 launchd
安装步骤见 [`scheduler-launchd.md`](scheduler-launchd.md)。日常 T+1 复核仍优先
`morning` 批（baostock 权威口径）；`index` 入口用于 T+0 当晚与断档回填。

## 验证

- 单元/集成：`.venv/bin/pytest tests/test_tencent_index.py tests/test_sina_index.py tests/test_index_chain.py`
- 全量：`.venv/bin/pytest tests/`（PG 不可达自动 skip）
- 真实冒烟（测试库 `qstock_test`，不写业务库）：

  ```bash
  PG_DSN=postgresql://localhost/qstock_test qstock-scheduler index \
    --codes 000688 --start 20261013 --end 20261023
  PG_DSN=postgresql://localhost/qstock_test qstock-scheduler index \
    --start 20261013 --end 20261023 --verify
  # 期望：fetch 7 指数全部 status=ok、source=tencent（rows=窗口交易日数）；
  # verify 逐指数 missing_days=[]（000688 回填后无缺口）
  ```
