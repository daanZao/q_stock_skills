# 个股日线固定入口操作手册（`qstock-scheduler daily`）

issue #112（spec #111）：个股日线 fallback 链重排为 **腾讯 → 新浪 → baostock**，
东财日线退出。本手册是该能力的固定 CLI 入口说明——抓取、区间更新、三源对拍
都走同一入口，可重复运行、可挂定时任务；不使用一次性临时脚本。

## 适用场景

| 场景 | 命令要点 |
| --- | --- |
| 收盘后当晚补当日行（T+0） | `daily --codes ... --date <当日>` |
| 补某段历史缺口 | `daily --codes ... --start <起> --end <止>` |
| 迁移/换源后验证数据质量 | `daily --codes ... --date <交易日> --check`（不写库） |
| 全市场刷新（重操作） | `daily --all --limit N` 冒烟后再全量；缺省 1 秒/只节流 |
| 北交所个股 | 新码 `920xxx` 直接传；旧码 `43x/83x/87x` 自动归一化到 920 新码 |
| 三复权口径**取数** | `--adj qfq` / `--adj hfq` / `--adj none` 分别执行（业务键含 adj，互不覆盖）；三源**对拍**时 hfq 只比量/额/换手率（见下） |

## 前置条件

- 已完成 editable 安装：`.venv/bin/pip install -e ".[sources,dev]"`；
- 环境变量 `PG_DSN` 指向业务库（写库模式必需；`--check` 不连库，仅 `--all`
  取股票清单时需要）；
- 数据库表已初始化（`init_database` 或此前已运行过任一写库批）。

## 命令

```bash
# 单日抓取落库（沪/深/北代表股）
qstock-scheduler daily --codes 600519,000001,920001 --date 20261020

# 区间补采（按业务键 upsert，可重复执行）
qstock-scheduler daily --codes 600519 --start 20261001 --end 20261020

# 三源对拍：逐源直取、字段级比较，不写库
qstock-scheduler daily --codes 600519,000001,920001,833171 --date 20261020 --check

# 北交所旧码（833171）经官方对照表归一化到 920571 后抓取
qstock-scheduler daily --codes 833171 --date 20261020

# 全市场（先冒烟 20 只；全量约 1.5–2 小时）
qstock-scheduler daily --all --limit 20 --date 20261020
qstock-scheduler daily --all --date 20261020 --throttle 1
```

参数说明：

- `--codes`：逗号分隔，支持 `sh/sz/bj` 前缀；与 `--all` 互斥。
- `--all`：从库内 `stock_list` 取全部代码；`--limit` 只取前 N 只（冒烟）。
- `--date` 与 `--start/--end` 互斥；都不传时缺省当日。区间跨多个交易日时
  逐段补齐，空结果（停牌/非交易日）不是失败。
- `--adj`：`qfq`（缺省）/ `hfq` / `none`，写入 `stock_daily.adj`。
- `--check`：逐源直取**结束日**的 bar 比对（区间模式取 `--end`），不落库；
  沪/深/北各源可比值见下。**`--adj hfq` 时只比成交量/成交额/换手率**——各源
  后复权基准不同（实测 600519 腾讯 9298.83 / 新浪 11830.44 / baostock
  10214.53），价格类字段直接比会误报；输出带 `note` 与每只股票的
  `compared_fields`。`qfq`/`none` 全字段比对。
- `--throttle`：逐 code 间隔秒；`--all` 缺省 1.0，显式 `--codes` 缺省 0。
  腾讯公开端点实测连发 5–6 个请求会触发 TLS 层断连（间隔 8s 恢复），批量
  抓取务必保留节流。
- 全量回溯（`init_database(backfill_history=True)`）也走本链：腾讯单请求
  640 根，逐股约 19 次请求；适配器在分段回溯之间留 0.3s 间隔，但整批仍建议
  放在非交易时段执行。日常全市场 T+1 补数优先用 `morning`（baostock 一把抓）。

## 输出与退出码

stdout 只输出一份 JSON（日志走 stderr）：

```json
{"status":"ok","job":"daily","mode":"fetch","range":{"start":"20261020","end":"20261020"},
 "adj":"qfq","total":3,"succeeded":3,"failed":0,
 "results":[{"stock_code":"600519","status":"ok","source":"tencent",
             "rows_upserted":1,"data_range":{"start":"20261020","end":"20261020"}}]}
```

- `mode="fetch"`：`status` 为 `ok` / `partial_error` / `error`（全失败）；
  每只股票带实际命中源 `source`（`tencent`/`sina`/`baostock`）。已补过的区间
  重跑为零抓取：`source` 为 null、`rows_upserted` 0、`note` 为「区间内无缺口，
  零抓取」，状态仍为 ok（幂等）。腾讯复权键缺失按 day 回落时（无复权事件或
  上游不提供调整序列），`note` 标注「上游缺复权键，已按不复权（day）数据落库」，
  采集台账 detail 同步留「复权回落=day」。
- `mode="check"`：`status` 为 `ok` / `mismatch` / `error`；每只股票带
  `sources.{tencent,sina,baostock}` 的逐源状态（`ok`/`no_data`/`error`/
  `unsupported`）与 `diffs` 字段级差异；`single_source` 只算警告（如北交所
  baostock by-design 不支持），不改变退出码。某源复权回落时该源条目带
  `note`（「上游缺复权键，本次取数为不复权（day）回落」）。
- 退出码：0 = 无失败/无差异；1 = 有失败、有对拍差异或输入非法；2 = 参数错误
  （argparse）。

## 数据口径

- **T+0**：腾讯/新浪收盘后当晚返回当日行（#104 实测 15:37 已可取）；baostock
  仍为 T+1，作兜底与停牌权威列来源。
- **单位**：volume 入库存股（腾讯手 ×100；科创板 sh688/sh689 原生为股；新浪
  原生为股）；amount 入库万元（腾讯原生万元；新浪元 ÷1e4）；turnover_rate
  为百分数（腾讯原生；新浪 fraction ×100）。
- **推算字段**：腾讯/新浪无 change_percent/change_amount/amplitude，按前收
  推算；区间首行的前收随回溯取得，上市首日无前收则置 null（不伪造）。
- **北交所**：`data/bse_code_map.csv` 为北交所官方新旧代码对照表（248 只，
  2025-10-09 全面切换）。腾讯旧码数据冻结在迁码前，因此旧码一律先归一化到
  920 新码再请求；对照表未覆盖的旧码原样请求（保留冻结历史可取）。注意：
  归一化只作用于**请求**，`stock_daily.stock_code` 仍记录你传入的代码——若
  同一只票既要按旧码又要按新码取数，会形成两条序列，迁移时需统一代码口径。
- **落库**：每只股票调用一次 `tools_daily.fetch_daily`，只补头尾缺口、按
  `(stock_code, trade_date, adj)` 幂等 upsert，并落一条 `collection_log`
  （section=`stock_daily`）。`--all` 会为每只股票各落一条台账行，与逐股调用
  `fetch_daily` 一致。

## 回滚与还原

CLI 只写 `stock_daily`（业务键 upsert）与 append-only 台账；无 schema 变更。
**upsert 会把被覆盖行的 `source` 改成本次命中源**，所以不能仅凭 `source`
判断"这行原来是谁写的"。写库前（尤其 `--all` 覆盖既有行时）先备份：

```bash
pg_dump -t stock_daily <db> > stock_daily_before_$(date +%Y%m%d%H%M).sql
```

回滚分两类：

```sql
-- 1) 清理本次**新增**的行（未发生过 upsert 覆盖：created_at = updated_at）
DELETE FROM stock_daily
 WHERE trade_date = '20261020' AND adj = 'qfq'
   AND source IN ('tencent', 'sina')
   AND created_at = updated_at
   AND stock_code IN ('600519', '000001', '920001');

-- 区间版同理：trade_date BETWEEN '20261001' AND '20261020'
```

- 本次**覆盖**的旧行无法从库内反推原值：从 `pg_dump` 备份恢复到临时表后按
  `(stock_code, trade_date, adj)` 回写，或用目标源重跑覆盖（upsert 幂等）。
- 台账 `collection_log` 为审计表，append-only，不回滚。
- 只想换回旧源数据时，按业务键删除后由 `query_daily` 自愈补回，或用目标源
  重跑。

## 定时任务示例（launchd）

收盘后 19:30 补当日行（示例；路径按实际仓库替换）：

```xml
<key>ProgramArguments</key>
<array>
  <string>/path/to/q_stock_skills/.venv/bin/qstock-scheduler</string>
  <string>daily</string>
  <string>--all</string>
  <string>--throttle</string>
  <string>1</string>
</array>
<key>StartCalendarInterval</key>
<dict><key>Hour</key><integer>19</integer><key>Minute</key><integer>30</integer></dict>
```

不传 `--date` 时由 CLI 取当日（目标日恒为当日 yyyymmdd）。

更完整的 launchd 安装步骤见 [`scheduler-launchd.md`](scheduler-launchd.md)。
注意：全市场 `--all` 是重操作（约 5900 次腾讯请求），日常全市场 T+1 补数仍
优先用 `morning` 批；`daily --all` 仅用于确需当晚 T+0 全量的场景。

## 验证

- 单元/集成：`.venv/bin/pytest tests/test_tencent_adapter.py tests/test_sina_adapter.py tests/test_daily_chain.py tests/test_scheduler.py`
- 全量：`.venv/bin/pytest tests/`（PG 不可达自动 skip）
- 真实冒烟（不写库）：

  ```bash
  qstock-scheduler daily --codes 600519,000001,920001,833171 --date 20261020 --check
  # 期望：status=ok、mismatch_count=0；北交所 baostock=unsupported、
  # tencent/sina 数值一致；833171 经映射返回 2026-10-20 行
  ```
