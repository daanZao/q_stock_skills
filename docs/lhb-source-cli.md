# 龙虎榜固定入口操作手册（`qstock-scheduler lhb`）

issue #114（spec #111）：龙虎榜迁移交易所官方源——深交所 ShowReport API（零鉴权 JSON，
汇总+席位明细）、上交所 showTradePublicFile.do（自写定宽文本解析器，需 Referer）、
北交所 bseinfo.net callAuctionController（curl_cffi TLS 指纹；**不走 bse.cn** 的 JS
挑战）。新浪 akshare 五函数留作互补校验（`--check` 手动触发对拍）。本手册是该能力的
固定 CLI 入口说明——抓取、历史回溯、对拍都走同一入口，可重复运行、可挂定时任务；
不使用一次性临时脚本。

## 适用场景

| 场景 | 命令要点 |
| --- | --- |
| 收盘后当日龙虎榜落库 | `lhb --date <当日>`（T 日盘后 16:30–18:00 发布） |
| 历史回补/回溯 | `lhb --start <起> --end <止>`（逐交易日，单日失败不中断） |
| 最近 90 个交易日整窗刷新 | `lhb --trading-days 90`（basic 按日替换 + statistic 重算） |
| 迁移后数据质量验证 | `lhb --date <交易日> --check`（新浪对拍，不写库） |
| 收盘后批（五 section 同跑） | `close [--date]`（lhb 段自动走本专用链） |

历史回溯深度（调研实测）：深交所/上交所回溯到 **2015 年**（2021-10-08 前上交所
为"前三只"口径，解析器天然兼容）；北交所回溯到 **2021-11 开市当周**（注意历史
数据保留旧证券代码，入库前自动经官方对照表归一化到 920 新码）。

## 前置条件

- 已完成 editable 安装：`.venv/bin/pip install -e ".[sources,dev]"`（`sources`
  extras 含 curl_cffi——北交所 bseinfo.net WAF 需 Chrome TLS 指纹，未装时北交所
  子项报明确错误、沪深不受影响）；
- 环境变量 `PG_DSN` 指向业务库（写库模式必需；`--check` 只需只读连库——
  读取 statistic 自算快照与新浪 ggtj 对照，不落库不落台账）；
- 数据库表已初始化（`init_database` 或此前已运行过任一写库批）。

## 命令

```bash
# 单日抓取落库（缺省当日；幂等，可重跑）
qstock-scheduler lhb --date 20261020

# 历史回溯（深圳默认同花顺主采；周末自动跳过；单日失败不中断）
qstock-scheduler lhb --start 20261001 --end 20261020

# 沪市/北交所官方失败时，显式启用同花顺市场级后备
qstock-scheduler lhb --trading-days 90 --fallback ths

# 整窗重抓，包括替换默认保留的完整官方日
qstock-scheduler lhb --trading-days 90 --force

# 新浪互补对拍：交易所官方 vs 新浪转载面，不落库
qstock-scheduler lhb --date 20261020 --check
```

参数说明：

- `--date`：单日 yyyymmdd；与 `--start/--end/--trading-days` 互斥。盘中/非交易日三所全空是
  **正常语义**（status ok + `empty_kind=no_data_for_date` + note，非失败）。
- `--start/--end`：回溯闭区间（自然日），区间内逐交易日抓取；缺省无（不传即
  单日模式）。进度经 stderr 双阈值报（每 20 日或 ≥60s，issue #89）。
- `--trading-days N`：含截止日在内 N 个 A 股交易日，可用 `--end` 改截止日，
  缺省截止最近已收盘交易日；与 `--start` 互斥。
- `--fallback ths`：仅历史回溯及统计前置 staging 可用，单日与 `--check` 拒绝。
  用于沪市/北交所官方失败时补缺；深圳默认同花顺主采，无需此参数。
  同花顺每日期最多一次请求、日期间 20 秒退避，封禁后停止本轮后续日期。
- `--force`：历史整窗重抓；缺省保留有成功台账佐证的完整官方日，显式 force
  才按当前主源链替换。staging 始终优先复用库内完整输入，不写窗外业务表。
- `--check`：对拍模式（不落库、不落台账；只读连库读取 statistic 自算快照）。
  逐股比对：
  - 股票集合双向差集（`stock_set_diff`，信息级——两源归类口径不同，差集供
    人工核对，不算失败）；
  - 全日成交额（交易所明细精确值 vs 新浪每日详情，rel 容差 0.005）；
  - 席位净买额（交易所席位明细自算 vs 新浪 getLHBComBSData 去重后 Σnet，
    万元，rel 容差 0.005；新浪席位缺失的只标注 note 不判差异）。
  - statistic 校验位（`statistic` 段）：库内 lhb_basic 自算快照 vs 新浪 ggtj
    近 60 日窗口，逐股比对上榜次数（精确）与买/卖/净额（rel 0.005）；
    `buy/sell_seat_count` 自算恒 NULL（官方汇总无席位计数来源，不伪造）
    不参与比对；ggtj 窗口锚定采集执行日，与 `--date` 非同日时差异含窗口
    滑移成分，供人工核对。
  - 指标（上榜原因）文案两源口径不同（交易所披露原文 vs 新浪归类），
    **不逐条比对**（输出 note 说明）。

## 输出与退出码

stdout 只输出一份 JSON（日志走 stderr）。fetch/backfill 模式示例：

```json
{"job":"lhb","mode":"fetch","status":"ok","trade_date":"20261020",
 "lhb":{"rows":45,"source":"exchange","status":"ok"},"failed_sections":[]}
```

- fetch：`lhb.status` ok / error（三态结构化：`error_kind` +
  `attempted_sources`，lhb 永不进核心集、不触发整批中止）；子项部分失败
  （同花顺 yyb 子表单挂）时 status 仍 ok 但带 `partial_error`。statistic
  由库内自算（见数据口径），无子项失败语义。
- backfill：`status` ok / partial_error / error（`aggregate_batch_status`
  批次口径）；`results` 逐日成败/行数/来源；`empty_days` 统计上游确认无
  数据日（非故障）。深圳主采时 `source_detail.szse_primary` 记录请求源、实际源
  和行数，其他市场后备由 `source_detail.fallback` 记录；保留官方日时
  `preserved=true`，不伪造一次新的上游采集。采集台账 `source` 与 `detail`
  同步记录真实来源，不把同花顺或混合来源记成纯官方。
- check：`status` ok / mismatch / error；`mismatch_count` 为差异股数
  （basic 对拍 + statistic 校验位差异都计入整体 status）；
  `results` 携带双方原值供人工核对。
- 退出码：0 = ok（含 empty 无数据）；1 = 失败/差异/输入非法；2 = 参数错误。

## 数据口径

- **金额**：全库万元（ADR 0006）。交易所席位明细为原始元，适配器映射层
  ÷1e4 收敛。lhb_basic 的 buy/sell/net 为**买前五/卖前五席位合计**（东财
  同口径），`total_amount = buy + sell`（与存量行一致）；全日成交金额随行
  携带 `day_amount` 元数据键仅供对拍（非落库列，repository 忽略）。
- **close_price / change_percent**：交易所官方汇总不提供（披露文本中的
  "偏离值"是偏离基准的数值，≠ 个股涨跌幅，**不伪造**置 null）。需要时可与
  `stock_daily` 按 (stock_code, trade_date) join 补齐。
- **turnover_rate**：仅披露原因为"日/当日换手率达到X%"时可从文本提取实际
  值；其余情形（振幅/偏离值等）置 null。
- **lhb_stock_statistic**：**库内自算**（issue #114 定稿，spec 冲突修正）——
  `fetch_board_snapshot(lhb 段)` 落 lhb_basic 后由
  `repository.recompute_lhb_statistic` 以**近 60 日历日窗口**（含当日，
  承接原新浪 ggtj「近 60 日」窗口语义）按股聚合：appear=窗口行数（业务键含
  lhb_reason，同股当日多榜单各计一次）、买/卖/净额=窗口合计（万元）。
  DELETE+INSERT 幂等可重跑，窗口滑出/上游修订自愈（旧快照股票不再入窗即
  删除）。`buy_seat_count`/`sell_seat_count` 恒 NULL：交易所官方汇总无席位
  计数来源、不伪造（lhb_stock_detail 接线后才可派生）。新浪 ggtj 回归
  `--check` 对拍校验位（不落库）。`trade_date` 为快照交易日，业务键
  (trade_date, stock_code)。历史回溯使用窗口前 60 个日历日 basic 作为连接级
  临时 staging，优先复用有成功台账佐证的库内完整日期，只抓缺失或来源不明日；
  staging 不写入窗外业务表。
- **历史回溯一致性**：只写 `lhb_basic` 与 statistic。已确认的完整官方日缺省
  保留；其余实际抓取日按日完整替换并在同一事务重算 statistic，`--force`
  才替换保留官方日。单所缺失、`partial_error` 或双源失败时保留旧数据并报告
  失败，不以残缺结果替换。两个营业部累计榜没有历史日期参数，历史任务不写。
- **lhb_stock_detail（席位级表）**：本期未接线（schema 在、写路径未挂；研究
  报告定位"能力增量非恢复"）。席位数据已用于金额自算与对拍。
- **北交所**：历史行旧码自动归一化 920 新码（官方 248 条对照表，未覆盖的
  极少数旧码原样保留）；北交所特有的严重异常波动分类资金（yzycbdList）本期
  不接线。
- **链序**：单日 `default_lhb_adapters()` 保持交易所官方单源；历史
  `default_lhb_history_adapters()` 在深圳组合同花顺主采，成功时不请求深交所，
  失败才由官方补缺，沪市/北交所仍走官方。显式 `--fallback ths` 只启用其他
  市场后备，同花顺同一日期页面缓存复用，不重复请求。深交所官方汇总单页仍有
  最多 3 次（0.5/1 秒退避）的防御性重试。同花顺 buy/sell/net 与普通行
  `day_amount` 统一万元；累计行 `day_amount` 置 null，`turnover_rate` 无独立
  实际值时恒 null。异常 HTML、业务键重复、日期错位或双源失败时当日失败并
  保留旧数据，staging 不继续也不写窗外表。

## 落库与回滚

单日实时 CLI 写 basic、两个营业部榜与 statistic；历史回溯只写 basic 与
statistic，并写 append-only 台账 `collection_log`。实际重抓日的 basic 按日替换
且与统计重算同事务；保留官方日不重写也不新增采集台账。恢复旧值须从事先备份
回写，不能假设 upsert 可反推旧行。台账不回滚。
按日期清理后可重跑：

```sql
DELETE FROM lhb_basic WHERE trade_date = '20261020';
DELETE FROM lhb_stock_statistic WHERE trade_date = '20261020';
-- 台账 collection_log 为审计表，append-only，不回滚
```

注意：statistic 为逐日快照语义，删除某日的 basic 行后重跑当日抓取即可重建
该日快照（窗口聚合只读历史）；删除某执行日的快照不影响其他日期。

## 定时任务示例（launchd）

收盘后 18:30 抓当日龙虎榜（示例；路径按实际仓库替换）：

```xml
<key>ProgramArguments</key>
<array>
  <string>/path/to/q_stock_skills/.venv/bin/qstock-scheduler</string>
  <string>lhb</string>
</array>
<key>StartCalendarInterval</key>
<dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>30</integer></dict>
```

不传 `--date` 时由 CLI 取当日。更完整的 launchd 安装步骤见
[`scheduler-launchd.md`](scheduler-launchd.md)。

## 验证

- 单元/集成：`.venv/bin/pytest tests/test_lhb_parsers.py tests/test_lhb_exchange_adapter.py tests/test_lhb_tools.py`
- 全量：`.venv/bin/pytest tests/`（PG 不可达自动 skip）
- 真实冒烟（写测试库，不写业务库）：

  ```bash
  PG_DSN=postgresql://localhost/qstock_test qstock-scheduler lhb --date 20261020
  # 期望：status ok、source=exchange、rows>0（当日有上榜股票时）
  PG_DSN=postgresql://localhost/qstock_test qstock-scheduler lhb --date 20261020 --check
  # 期望：status ok（或少量 single_source/mismatch 供人工核对）
  ```
