# 复权回填 A：`qstock-scheduler adj-backfill`

Issue [#163](https://github.com/daanZao/q_stock_skills/issues/163)，spec [#158](https://github.com/daanZao/q_stock_skills/issues/158) §6.1/§6.2。实现：
`src/qstock_mcp/adjustment_backfill.py`；单票竖切仍由 `adjustment.rebuild_stock` 承担。

## 适用场景

- 建立 2023-01-01 以来（`QSTOCK_ADJ_WINDOW_START`）的全市场累计因子**事件集**。
- 只对窗口内**有事件**的股票重抓 baostock 不复权 raw，反推窗口基线 B0，重建
  `stock_daily_hfq` 与 `stock_factor_state`。
- 断点续跑、失败重试、失败/空日审计、待修复台账与事件票清单导出。

**不适用**：

- 无事件票 raw 回补（归 issue #164，`adj-backfill` 不会抓无事件票）。
- 北交所批量（默认排除；`query_daily_adjust_factor` 只处理 sh/sz 个股前缀）。
- 2023 年以前 legacy 数据的重算（旧 qfq 只读留档，不从 legacy 反推 raw/hfq）。
- 全市场替代源回填：baostock 整体不可用时保留缺口与 error 台账，不自动改走
  腾讯/新浪。

## 前置条件

1. `PG_DSN` 指向目标库；正式回填前**备份**四张复权影子表（或整库）：
   `stock_daily_raw`、`stock_adjust_factor`、`stock_daily_hfq`、`stock_factor_state`。
2. `.venv` 已安装 baostock（`pip install -e ".[sources]"`）。
3. 不与 pytest / `live-sample` 并发（共享测试库 runner 锁）。
4. 窗口起点可用 `QSTOCK_ADJ_WINDOW_START` 覆盖，默认 `20230101`。
5. 交易日历来自 `dates._MARKET_CLOSURES`（2023–2026 已列）；2027 起需按
   交易所公告补齐，否则休市日会被多请求并记 `empty`（只会多不会漏事件，
   但“完整交易日集合”会偏大）。

## 命令

```bash
# 全窗口（正式回填；end 缺省为最近已收盘交易日）
PG_DSN=postgresql://user:pass@host:5432/qstock \
  .venv/bin/qstock-scheduler adj-backfill \
  --start 20230101 --end 20261104 \
  --report-dir files/adj-backfill_20230101_20261104

# 小样本开发验收（最近 3 个交易日、最多 20 只有事件票）
PG_DSN=postgresql://localhost/qstock_test \
  .venv/bin/qstock-scheduler adj-backfill --trading-days 3 --limit 20 \
  --report-dir files/adj-backfill_smoke

# 断点续跑：与上次完全相同的窗口参数再执行即可；已完成且仍有效的因子日/raw
# 票零上游请求。summary.upstream_requests.total 应接近 0（待修复票会重建，
# 但已成功抓取的 raw 不重抓）。

# 整窗重抓：显式 --force（忽略台账断点；只在确认需要重新取权威事实时使用）
PG_DSN=... .venv/bin/qstock-scheduler adj-backfill \
  --start 20230101 --end 20261104 --force
```

参数：

| 参数 | 说明 |
| --- | --- |
| `--start` | 窗口起点 `yyyymmdd`；缺省 `QSTOCK_ADJ_WINDOW_START` 或 `20230101` |
| `--end` | 窗口终点；缺省最近已收盘交易日；盘中显式指定当日会被拒（零落库） |
| `--trading-days N` | 最近 N 个交易日；与 `--start` 互斥，可用 `--end` 指定截止日 |
| `--limit N` | 只处理前 N 只有事件票（冒烟）；结果标记 `limited`，不宣称完整 |
| `--force` | 忽略台账断点，整窗重抓事件与 raw |
| `--report-dir` | 审计产物目录；缺省 `files/adj-backfill_<start>_<end>` |
| `--throttle` | 相邻上游请求间隔秒；缺省 1.5 |

## 执行流程

1. **事件集**：窗口内每个历史交易日调用一次 `query_daily_adjust_factor(date)`。
   - 成功空结果 → `collection_log` 的 `stock_adjust_factor` 记 `empty`。
   - 成功非空 → 事务内整日替换 `stock_adjust_factor`（支持修订/撤销），记 `ok`。
   - 失败 → 按 0.5/1/2s 退避重试 3 次；仍失败记 `error`，保留旧事件，**绝不**
     当“当日无事件”，并进入 `failed_days`。
2. **事件票清单**：按 `stock_adjust_factor` 窗口内行导出；非事件票不写占位因子。
3. **raw**：只对事件票逐只 `fetch_daily(..., adj="none")`（`adjustflag=3`），区间
   `[窗口首交易日, end]`；首事件恰在窗口首日时起点提前到窗口外最后一个交易日
   作为 D-1 计算输入（该行保留在 raw 供审计，不写 hfq）。amount 元→万元由
   适配层完成，`tradestatus`/`turn`/`pctChg` 原样入库。
4. **重建**：`rebuild_stock(window_start, window_end)` 反推窗口基线
   `B0=B1×f1`，写事件 B、hfq F/F_latest 与 state；逐事件做 B 路径 vs 权威
   pctChg 价格路径对账（相对误差 ≤0.1% 为一致）。
5. **待修复台账**：见下节；存在任何未决项时 `stage=rebuilt`，不输出
   「数据可以使用」。
6. **导出**：逐日状态、事件集、事件票清单、raw 结果、待修复明细、summary。

## 断点续跑判据

| 对象 | 复用条件 |
| --- | --- |
| 因子日 | 该日 `collection_log(stock_adjust_factor)` 最近一条为 `ok/empty`，且事件表该日行数与台账 `rows` 一致 |
| raw 票 | 该票本窗口最近一条 `adj-backfill raw` 台账为 `ok`，窗口内 raw 行数 > 0，且窗口首日事件票存在 D-1 行 |
| 重建 | 每次运行都会重建入选事件票（幂等）；但不会重抓已有效 raw |

失败/空日会重新请求；失败票会重新请求。`--force` 跳过全部复用。

## 待修复台账与 state 语义

事件被权威结果撤销时，**与整日事件替换同一事务**写
`collection_log(section=stock_factor_retracted, status=ok, detail='adj-backfill
retracted <code> <ex_date>')`；崩溃后断点续跑据此把旧事件票按“无事件”重建，
不会留下旧 hfq/state。

未决项写 `collection_log` section `stock_factor_repair`，`status=error`，
`error_kind` 取值：

| error_kind | 含义 | state |
| --- | --- | --- |
| `baseline_unverified` | 首个窗口事件缺 D-1/pctChg，或 B0 非正/非有限 | `baseline_factor=NULL`、`factor_latest=NULL`、`rebuild_pending=true`、coverage=partial；不猜 B0 |
| `factor_mismatch` | 后续事件 B 路径与价格路径相对误差 >0.1% | 保留已建立基线，`rebuild_pending=true` |
| `price_path_unverified` | 后续事件缺 D-1 raw 或权威 pctChg，价格路径不可验证 | 保留已建立基线，`rebuild_pending=true` |
| `raw_gap` | 窗口内 raw 缺少交易日（含停牌占位行缺失） | `rebuild_pending=true` |
| `invalid_close` | close 为 NULL 或 ≤0；行保留在 raw、不进入 hfq | `rebuild_pending=true` |
| `raw_unavailable` | 事件票 raw 抓取失败或上游空返回 | 无 raw 时不造 state；已有 raw 时沿用重建 |
| `raw_refresh_failed` | 本次 raw 重抓失败，沿用库内已有 raw | 沿用重建结果并标记 pending |

§2.4 允许 pending 时保留上一组有效值；只有**基线本身**不可验证时才强制
NULL/NULL。待修复记录是 append-only 审计；重跑成功不会删除旧记录，但当前
state 会恢复 `rebuild_pending=false`。

查询示例：

```sql
SELECT trade_date, error_kind, detail
FROM collection_log
WHERE section = 'stock_factor_repair'
ORDER BY id DESC LIMIT 100;

SELECT stock_code, baseline_factor, factor_latest, latest_trade_date, rebuild_pending
FROM stock_factor_state
WHERE rebuild_pending;
```

## 产物

| 文件 | 内容 |
| --- | --- |
| `factor_days.csv` | 每个交易日 `ok/empty/error/skipped`、行数、尝试次数、失败明细 |
| `events.csv` | 权威事件集：`stock_code, ex_date, factor_cum, source` |
| `event_stocks.csv` | 事件票清单：事件数、首/末事件日 |
| `raw_results.csv` | 每票 raw 结果：成功/跳过/失败、行数、尝试次数、pending、修复原因 |
| `repairs.csv` | 待修复明细（股票、日期、原因、详情） |
| `summary.json` | 窗口、逐日计数、事件/票数、上游请求数、耗时、status/stage |

CLI stdout 的最终 JSON 含相同 `summary` 与 `artifacts` 路径。存在失败、待修复
或 `--limit` 时退出码为 1；只有 `status=ok` 且无未决项才输出「数据可以使用」。

## 正式回填后的全量检查（只读，不重新请求上游）

```sql
-- 0. 窗口内每个交易日的因子采集状态（失败日必须为 0）
SELECT status, count(*) FROM collection_log
WHERE section='stock_adjust_factor' AND trade_date BETWEEN '20230101' AND '20261104'
GROUP BY status;

-- 1. 事件 B 正且有限；state 形状与 pending 一致
SELECT count(*) FROM stock_adjust_factor
WHERE factor_cum <= 0 OR factor_cum >= 'Infinity'::numeric;
SELECT count(*) FROM stock_factor_state
WHERE (baseline_factor IS NULL) <> (factor_latest IS NULL)
   OR (NOT rebuild_pending AND baseline_factor IS NULL);

-- 2. 每个事件票有 state；state 最新日与 raw 最新日一致
SELECT count(*) FROM (
  SELECT DISTINCT stock_code FROM stock_adjust_factor
  WHERE ex_date BETWEEN '20230101' AND '20261104'
) e
LEFT JOIN stock_factor_state s USING (stock_code)
WHERE s.stock_code IS NULL;

SELECT count(*) FROM stock_factor_state s
WHERE s.latest_trade_date <> (
  SELECT max(trade_date) FROM stock_daily_raw r WHERE r.stock_code=s.stock_code
);

-- 3. hfq 只覆盖有效 raw；F 正且有限
SELECT count(*) FROM stock_daily_hfq h
LEFT JOIN stock_daily_raw r USING (stock_code, trade_date)
WHERE r.stock_code IS NULL OR r.close IS NULL OR r.close <= 0;
SELECT count(*) FROM stock_daily_hfq
WHERE factor_cum <= 0 OR factor_cum >= 'Infinity'::numeric;

-- 4. 未决异常必须为 0 才可切读
SELECT count(*) FROM stock_factor_state WHERE rebuild_pending;
SELECT error_kind, count(*) FROM collection_log
WHERE section='stock_factor_repair'
  AND trade_date BETWEEN '20230101' AND '20261104'
GROUP BY error_kind;
```

完整八项验收（事件不变量、Golden、哈希幂等）由 issue #165 的测试套件承担；
本回填只负责产出可审计的迁移数据集。

## 回滚

**首选**：回填前对四张影子表做备份（`pg_dump -t stock_daily_raw -t
stock_adjust_factor -t stock_daily_hfq -t stock_factor_state`），回滚时恢复。

**仅在隔离/测试库**中可用的事务回滚（会删除本轮事件票在窗口内的 raw；若
窗口内已有 morning 写入，先确认这些行可重建）：

```sql
BEGIN;
CREATE TEMP TABLE _adj_codes AS
  SELECT DISTINCT stock_code FROM stock_adjust_factor
  WHERE ex_date BETWEEN :start AND :end;
DELETE FROM stock_daily_hfq
  WHERE stock_code IN (SELECT stock_code FROM _adj_codes)
    AND trade_date BETWEEN :start AND :end;
DELETE FROM stock_factor_state
  WHERE stock_code IN (SELECT stock_code FROM _adj_codes);
DELETE FROM stock_adjust_factor
  WHERE ex_date BETWEEN :start AND :end;
DELETE FROM stock_daily_raw
  WHERE stock_code IN (SELECT stock_code FROM _adj_codes)
    AND trade_date BETWEEN :start AND :end;  -- D-1 行在 :start 之前，天然保留
DELETE FROM collection_log
  WHERE section IN ('stock_adjust_factor','stock_daily_raw',
                    'stock_factor_repair','stock_factor_retracted')
    AND detail LIKE 'adj-backfill %'
    AND trade_date BETWEEN :start AND :end;
COMMIT;
```

正式切读后若已产生新写入，回滚必须走 issue #166 的修复工件流程，不得只删
回填行。

## 数据源纪律与实测记录

- 串行、无并发；相邻上游请求默认 1.5s；单请求 120s 外部硬超时；会话被踢
  由适配器重登 ≤3 次。
- 2026-11-04/05 已验证：单会话串行、相邻 ≥1.5s 的少量样本可承载；长期
  TPS、并发上限、全历史完备率**未验证**，不得据小样本放量。
- 2026-11-05 开发验收（qstock_test，真实 baostock）：3 个连续交易日
  （20261102–20261104），3 次因子请求成功返回 14 行/14 票；14 次单票 raw
  请求成功，窗口 raw 41 行；3 票首事件在窗口首日，额外取 20261030 作 D-1
  且未进 hfq；001381 上市首日（20261103）事件无 D-1，按规则进入
  `baseline_unverified`，未猜 B0。首跑 25.9s（17 次真实请求、1.5s 间隔）；
  原样重跑 0 次上游请求、3 日/14 票全部命中台账，耗时 0.048s，四表业务
  哈希一致。该结果只代表当次样本，不构成全窗口成功率或耗时结论。
