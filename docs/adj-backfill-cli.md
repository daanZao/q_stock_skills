# 复权回填：`qstock-scheduler adj-backfill`

Issue [#163](https://github.com/daanZao/q_stock_skills/issues/163)，spec [#158](https://github.com/daanZao/q_stock_skills/issues/158) §6.1/§6.2。实现：
`src/qstock_mcp/adjustment_backfill.py`；本地单票竖切由 `adjustment.rebuild_stock` 承担。

## 适用场景

- 建立 2023-01-01 以来（`QSTOCK_ADJ_WINDOW_START`）的全市场累计因子**事件集**。
- D-1 与窗口内每个交易日通过 `query_daily_history_k_AStock(date)` 获取全市场
  baostock 不复权 raw；本地按票重建，事件票反推窗口基线 B0，无事件票 B0=1。
- 断点续跑、失败重试、失败/空日审计、待修复台账与事件票清单导出。

**不适用**：

- #164 已在同一入口接入无事件分类、B0=1、非正价隔离、legacy `adj=none`
  只读盘点与迁移审计；raw 仍由本命令采集，不再逐票重抓。
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
6. `--end` 必须覆盖库内最新复权事实（`max(stock_daily_raw.trade_date)` 与
   `max(stock_adjust_factor.ex_date)`）。回填会重建 state/hfq；历史 end 若
   早于已有数据会回退窗口外派生，程序直接拒绝并返回
   `error_kind="window_behind_data"`，零写入。历史窗口请用隔离库。
7. **使用 CLI 命令级自动租约**（ADR 0019/#173）。直接运行 `adj-backfill`；首次实际抓取时自动
   acquire，整个命令复用同一实例租约与会话，正常或异常结束均 finally release。不要执行旧的独立
   `acquire → adj-backfill → release`。

   锁冲突返回 `baostock_locked` 和真实持有者，不重试、不静默换源；持有进程仍活跃时等待或终止
   持有命令。确认持有进程已终止后，才用 `qstock-scheduler baostock-lock release --force` 清理
   stale。数据成功但清理失败返回 `baostock_cleanup_error`。详见
   [baostock-lease.md](baostock-lease.md)。

## 命令

```bash
# 全窗口（正式回填；end 缺省为最近已收盘交易日）
PG_DSN=postgresql://user:pass@host:5432/qstock \
  .venv/bin/qstock-scheduler adj-backfill \
  --start 20230101 --end 20261104 \
  --report-dir files/adj-backfill_20230101_20261104

# 小样本开发验收（D-1 + 最近 3 个交易日全市场 raw，最多本地重建 20 只事件票）
PG_DSN=postgresql://localhost/qstock_test \
  .venv/bin/qstock-scheduler adj-backfill --trading-days 3 --limit 20 \
  --report-dir files/adj-backfill_smoke

# 断点续跑：与上次完全相同的窗口参数再执行即可；已完成且仍有效的因子日/raw
# 日零上游请求。summary.upstream_requests.total 应为 0；本地重建仍会执行。

# 整窗重抓：显式 --force（忽略台账断点；只在确认需要重新取权威事实时使用）
PG_DSN=... .venv/bin/qstock-scheduler adj-backfill \
  --start 20230101 --end 20261104 --force
```

参数：

| 参数 | 说明 |
| --- | --- |
| `--start` | 窗口起点 `yyyymmdd`；缺省 `QSTOCK_ADJ_WINDOW_START` 或 `20230101` |
| `--end` | 窗口终点；缺省最近已收盘交易日；必须覆盖库内最新复权事实；盘中显式指定当日会被拒（零落库） |
| `--trading-days N` | 最近 N 个交易日；与 `--start` 互斥，可用 `--end` 指定截止日 |
| `--limit N` | 只本地重建前 N 只事件票；不减少按日 raw 请求；结果标记 `limited`，不宣称完整 |
| `--force` | 忽略台账断点，整窗重抓事件与 raw |
| `--report-dir` | 审计产物目录；缺省 `files/adj-backfill_<start>_<end>` |
| `--throttle` | 相邻上游请求间隔秒；缺省 1.5 |

## 执行流程

1. **事件集**：窗口内每个历史交易日调用一次 `query_daily_adjust_factor(date)`。
   - 成功空结果 → `collection_log` 的 `stock_adjust_factor` 记 `empty`。
   - 成功非空 → 事务内整日替换 `stock_adjust_factor`（支持修订/撤销），记 `ok`；
     同一事务把受影响 state 置 `rebuild_pending=true`，并把被撤销票写
     `stock_factor_retracted` 标记（崩溃续跑可补齐）。
   - **同 B 非阶跃行不写入事件表**：若某票当日 B 与它上一个事件 B 相等，说明
     上游返回行没有价格阶跃，跳过（不写 `f=1` 占位行）；台账 detail 记
     `equal_b_skipped=N`，当日若只剩这类行记 `ok, rows=0`。
   - 失败 → 最多 3 次尝试，重试前 0.5s/1.0s 退避；仍失败记 `error`，保留旧
     事件，**绝不**当“当日无事件”，并进入 `failed_days`。
2. **事件票清单**：按 `stock_adjust_factor` 窗口内行导出；非事件票不写占位因子。
3. **raw**：对 D-1 与窗口内每个交易日调用 `fetch_astock_daily(date)`，即
   `query_daily_history_k_AStock(date)` 全市场 `adjustflag=3` raw。响应通过代码唯一、
   日期一致、raw 口径校验后整日替换 `stock_daily_raw`；失败保留旧事实并落 error。
   D-1 只作窗口首日事件的基线输入，不进入 hfq、事件集或窗口查询交付；没有首日
   事件需要它时，空 D-1 不单独构成窗口失败，需要但缺失时由基线校验置 pending。amount
   元→万元由适配层完成，`tradestatus`/`turn`/`pctChg` 原样入库。
4. **重建**：本地逐票调用 `rebuild_stock(window_start, window_end)`，不再发逐票
   上游请求。事件票反推 `B0=B1×f1`，无事件票仅在事件集完整时以 B0=1 重建；
   逐事件做 B 路径 vs 权威 pctChg 价格路径对账（相对误差 ≤0.1% 为一致）。
   窗口外已有 hfq/事件时拒绝重建，绝不回退窗口外派生。权威事件补录或撤销会触发
   重新分类与重建。
5. **待修复台账**：见下节；存在任何未决项时 `stage=rebuilt`，不输出
   「数据可以使用」。
6. **迁移审计（#164）**：以集合 SQL 汇总 raw/hfq/state 覆盖与无事件分类不变量，
   导出每股四表业务摘要哈希（排除运行时间戳）和固定种子 `164` 的分层普通 raw
   样本；同时只读盘点 legacy `adj=none`，所有旧行保持隔离，窗口内未被 raw 覆盖的
   行列为重抓候选，默认合并数恒为 0。
7. **导出**：因子日、raw 日、分类、逐票重建、隔离、旧数据盘点、哈希、样本与 summary。

## 断点续跑判据

| 对象 | 复用条件 |
| --- | --- |
| 因子日 | 该日**本回填** `adj-backfill event <start>~<end>` 台账最近一条为 `ok/empty`，且事件表该日行数与台账 `rows` 一致；morning 的 pctChg 降级 ok 行不算已调权威接口 |
| raw 日 | 该日**本回填** `adj-backfill raw-day <start>~<end>` 台账最近一条为 `ok`，且 raw 表该日行数与台账 `rows` 一致；`empty/error` 均重新请求 |
| 重建 | 每次运行都对入选股票做本地幂等重建；不会重抓已有效 raw 日 |

失败日/空日均重新请求。`--force` 跳过全部复用。2026-11-08 前的逐票 raw 台账只作历史审计，不能充当逐日全市场断点。

## 待修复台账与 state 语义

事件被权威结果撤销时，**与整日事件替换同一事务**写
`collection_log(section=stock_factor_retracted, status=ok, detail='adj-backfill
retracted <code> <ex_date>')`，并把受影响 `stock_factor_state.rebuild_pending`
置 true；崩溃后断点续跑据此把旧事件票按“无事件”重建，读者在重建完成前只能
看到 pending，不会把新事件分母配旧 hfq。

未决项写 `collection_log` section `stock_factor_repair`，`status=error`，
`error_kind` 取值（这些是复权审计原因，不是 ADR 0007 的抓取三态；抓取失败
仍用 `upstream_unreachable`/`fetch_failed`/`no_data_for_date`）：

| error_kind | 含义 | state |
| --- | --- | --- |
| `baseline_unverified` | 首个窗口事件缺 D-1/pctChg，或 B0 非正/非有限 | `baseline_factor=NULL`、`factor_latest=NULL`、`rebuild_pending=true`、coverage=partial；不猜 B0 |
| `factor_mismatch` | 后续事件 B 路径与价格路径相对误差 >0.1% | 保留已建立基线，`rebuild_pending=true` |
| `price_path_unverified` | 后续事件缺 D-1 raw 或权威 pctChg，价格路径不可验证 | 保留已建立基线，`rebuild_pending=true` |
| `raw_gap` | 生命周期内 raw 断档、本轮明确失败的 raw 日（含窗口首尾），或首/末事件日不在 raw（detail 带 `first_event=` / `last_event=`） | `rebuild_pending=true` |
| `invalid_close` | close 为 NULL 或 ≤0；行保留在 raw、不进入 hfq | `rebuild_pending=true` |
| `raw_unavailable` | 事件票在库内没有有效 raw（通常由 raw 日失败导致） | 无 raw 时不造 state |
| `raw_refresh_failed` | 2026-11-08 前逐票方案的历史审计原因；新按日方案不再产生 | 仅供历史审计 |
| `rebuild_failed` | 本地复权重建异常 | 本次重建失败，保留审计并返回 partial_error |

§2.4 允许 pending 时保留上一组有效值；只有**基线本身**不可验证时才强制
NULL/NULL。待修复记录是 append-only 审计；重跑成功不会删除旧记录，但当前
state 会恢复 `rebuild_pending=false`。因此切读门禁看
`stock_factor_state.rebuild_pending`，不要把历史修复行数当作当前未决数。

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
| `factor_days.csv` | 每个交易日因子 `ok/empty/error/skipped`、行数、尝试次数、失败明细 |
| `raw_days.csv` | D-1 与每个窗口交易日 raw `ok/empty/error/skipped`、行数、尝试次数、失败明细 |
| `events.csv` | 权威事件集：`stock_code, ex_date, factor_cum, source` |
| `event_stocks.csv` | 事件票清单：事件数、首/末事件日 |
| `raw_results.csv` | 每票本地重建结果：成功/失败、窗口 raw 行数、pending、修复原因 |
| `repairs.csv` | 待修复明细（股票、日期、原因、详情） |
| `stock_classification.csv` | 窗口内事件/无事件/撤销/未分类/因 limit 跳过的股票及原因 |
| `quarantined.csv` | close NULL/≤0 的 raw 隔离明细；原行仍保留在 raw |
| `legacy_none_audit.json` | 旧 `adj=none` 的来源、日期、非正价、raw 覆盖、隔离与重抓候选聚合计数 |
| `stock_hashes.csv` | 每股 raw/事件/hfq/state 行数与业务列摘要哈希，不含运行时间戳 |
| `raw_sample.csv` | 固定种子 `164`，每类最多 100 股、每股最多 5 行，优先覆盖交易所/年份/价格区间/来源 strata；不做全量逐值对拍 |
| `summary.json` | 窗口、完整性证明、分类/隔离/跳过分布、集合校验、抽样与上游计数、耗时、status/stage |

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

-- 2b. 首事件基线 B0=B1×f1（D-1 取首事件日前最近 raw 行；raw 缺口另查）
WITH first_ev AS (
  SELECT DISTINCT ON (stock_code) stock_code, ex_date, factor_cum
  FROM stock_adjust_factor
  WHERE ex_date BETWEEN '20230101' AND '20261104'
  ORDER BY stock_code, ex_date
), prev_raw AS (
  SELECT f.stock_code, f.factor_cum, r.close, r.change_percent, p.close AS prev_close
  FROM first_ev f
  JOIN stock_daily_raw r ON r.stock_code=f.stock_code AND r.trade_date=f.ex_date
  LEFT JOIN LATERAL (
    SELECT close FROM stock_daily_raw p
    WHERE p.stock_code=f.stock_code AND p.trade_date < f.ex_date
    ORDER BY p.trade_date DESC LIMIT 1
  ) p ON true
)
SELECT count(*) FROM prev_raw pr
JOIN stock_factor_state s USING (stock_code)
WHERE s.baseline_factor IS NOT NULL
  AND pr.prev_close IS NOT NULL AND pr.change_percent IS NOT NULL
  AND pr.prev_close > 0 AND pr.close > 0
  AND abs(s.baseline_factor - pr.factor_cum * (
        pr.close / (pr.prev_close * (1 + pr.change_percent / 100)))) >
      1e-6 * s.baseline_factor;

-- 3. hfq 只覆盖有效 raw；F 正且有限
SELECT count(*) FROM stock_daily_hfq h
LEFT JOIN stock_daily_raw r USING (stock_code, trade_date)
WHERE r.stock_code IS NULL OR r.close IS NULL OR r.close <= 0;
SELECT count(*) FROM stock_daily_hfq
WHERE factor_cum <= 0 OR factor_cum >= 'Infinity'::numeric;

-- 3b. hfq=raw×F（容差）
SELECT count(*) FROM stock_daily_hfq h
JOIN stock_daily_raw r USING (stock_code, trade_date)
WHERE r.close IS NULL OR h.close IS NULL
   OR abs(h.close - r.close * h.factor_cum) > 1e-6 * greatest(abs(h.close), 1);

-- 3c. 事件日 F(D)×f(D)=F(D-1)（D-1 取 hfq 前一交易行；raw 缺口另查）
WITH ev AS (
  SELECT stock_code, ex_date, factor_cum,
         lag(factor_cum) OVER (PARTITION BY stock_code ORDER BY ex_date) AS prev_b
  FROM stock_adjust_factor
  WHERE ex_date BETWEEN '20230101' AND '20261104'
), hfq AS (
  SELECT stock_code, trade_date, factor_cum,
         lag(factor_cum) OVER (PARTITION BY stock_code ORDER BY trade_date) AS prev_f
  FROM stock_daily_hfq
  WHERE trade_date BETWEEN '20230101' AND '20261104'
)
SELECT count(*) FROM ev
JOIN hfq h ON h.stock_code=ev.stock_code AND h.trade_date=ev.ex_date
WHERE ev.prev_b IS NOT NULL
  AND (h.prev_f IS NULL
       OR abs(h.factor_cum * (ev.prev_b / ev.factor_cum) - h.prev_f)
          > 1e-6 * greatest(abs(h.factor_cum), 1));

-- 4. 切读门禁：当前 state 无 pending；修复台账只作审计（历史行可保留）
SELECT count(*) FROM stock_factor_state WHERE rebuild_pending;
SELECT error_kind, count(*) FROM collection_log
WHERE section='stock_factor_repair'
  AND trade_date BETWEEN '20230101' AND '20261104'
GROUP BY error_kind;
```

上述 SQL 与随回填生成的 `summary.validation`、`stock_hashes.csv` 覆盖结构、首事件
B0、事件不变量、hfq 派生、覆盖关系和幂等证据。正式回填后用 issue #165 的固定入口
`qstock-scheduler adj-acceptance --migration-dir <本回填产物目录>` 执行 Golden、100+100
分层样本、指标回归和完整八项硬验收（手册：[复权迁移硬验收](adj-acceptance-cli.md)），
八项全绿后再按 #166 切读。

## 回滚

**首选**：回填前对四张影子表及断点台账做备份（`pg_dump -t stock_daily_raw -t
stock_adjust_factor -t stock_daily_hfq -t stock_factor_state -t collection_log`），回滚时恢复；
生产回滚只用备份，不执行下面的删除 SQL。

**仅在隔离/测试库**中可用的事务回滚（会删除本轮全部事件/无事件票在 `d1~end`
的 raw 与派生；若窗口内已有 morning 写入，先确认这些行可重建。`d1` 为窗口首日前一
交易日）：

```sql
BEGIN;
CREATE TEMP TABLE _adj_codes AS
  SELECT DISTINCT stock_code FROM stock_adjust_factor
  WHERE ex_date BETWEEN :start AND :end
  UNION
  SELECT DISTINCT stock_code FROM stock_daily_raw
  WHERE trade_date BETWEEN :d1 AND :end;
DELETE FROM stock_daily_hfq
  WHERE stock_code IN (SELECT stock_code FROM _adj_codes)
    AND trade_date BETWEEN :start AND :end;
DELETE FROM stock_factor_state
  WHERE stock_code IN (SELECT stock_code FROM _adj_codes);
DELETE FROM stock_adjust_factor
  WHERE ex_date BETWEEN :start AND :end;
DELETE FROM stock_daily_raw
  WHERE stock_code IN (SELECT stock_code FROM _adj_codes)
    AND trade_date BETWEEN :d1 AND :end;
DELETE FROM collection_log
  WHERE section IN ('stock_adjust_factor','stock_daily_raw',
                    'stock_factor_repair','stock_factor_retracted')
    AND detail LIKE 'adj-backfill %'
    AND trade_date BETWEEN :d1 AND :end;
COMMIT;
```

正式切读后若已产生新写入，回滚必须走 issue #166 的修复工件流程，不得只删
回填行。

## 数据源纪律与实测记录

- 串行、无并发；相邻上游请求默认 1.5s。单请求 60s 超时、`10001001` 重登一次
  重试同一请求、瞬时错误最多重试一次，均由 `BaostockApi` 承载（ADR 0019）。
- 正式回填使用 **CLI 命令级自动实例租约**（ADR 0019/#173）：首次实际抓取自动 acquire，
  整个回填进程复用一个 `BaostockApi` 会话，正常或异常结束先关会话再 finally release；
  禁止每请求 login/logout，`10001011` 致命零重试。
- 租约归属具体进程实例；另一个 CLI/MCP 进程即使同入口也不能共享或正常 release。锁冲突返回
  `baostock_locked`，不重试、不静默换源。
- 日预算按协议动作计，45,000 警告、50,000 停止；大回填不得与 morning/close/daily 并发。
  崩溃不自动删除文件租约；确认持有进程终止后才可 `release --force`。
- 数据成功但会话关闭或 release 失败返回 `baostock_cleanup_error`；数据失败时保留原错误并附
  cleanup 警告。首个实际请求失败保留断点，不逐日放大登录限速。
- #172/#173 已按进程实例租约新契约验收关闭；定向、双进程、stdio 与全量回归证据见
  [验收报告](research/2026-11-06-baostock-lease-acceptance.md)。
- 2026-11-04/05 已验证：单会话串行、相邻 ≥1.5s 的少量样本可承载；长期
  TPS、并发上限、全历史完备率**未验证**，不得据小样本放量。
- 2026-11-05 正式回填事故记录：每请求登录/登出跑到约 508 个因子日后，
  baostock 登录/查询退化到 20s 无响应（`10002007 网络接收错误`，TCP 连接
  仍 0.03s 可达），随后改为单登录会话。该次事故与恢复验证见
  `docs/research/2026-11-04-adjustment-core-backfill.md` §11。
- 2026-11-05 开发验收（qstock_test，真实 baostock）：3 个连续交易日
  （20261102–20261104），3 次因子请求成功返回 14 行/14 票；14 次单票 raw
  请求成功，窗口 raw 41 行；3 票首事件在窗口首日，额外取 20261030 作 D-1
  且未进 hfq；001381 上市首日（20261103）事件无 D-1，按规则进入
  `baseline_unverified`，未猜 B0。首跑 25.9s（17 次真实请求、1.5s 间隔）；
  原样重跑 0 次上游请求、3 日/14 票全部命中台账，耗时约 0.04s，四表业务
  哈希一致。该结果只代表当次样本，不构成全窗口成功率或耗时结论。
- 2026-11-08 策略修订：旧逐票 raw 进程在完整 928 日因子事件集后仅处理 822/4,443
  事件票，按当时速度仍需约一天，经用户确认停止；已写入的 738,662 行 raw 保留，进程
  终止后的 stale 租约已 force 清理。新策略改为 D-1 + 窗口内逐日全市场 raw。
- 2026-11-08 全市场 raw 只读样本（CLI 实例租约、单会话串行、未写业务库）：
  20230103/04/05 各 4,906 行，20261103 为 5,224 行；全部 `adj=none`，分别含 6/4/4/8
  个停牌行，停牌行 pctChg 为空；001381 在 20261103 返回完整新股 raw。单日耗时约
  9.5/9.5/9.6/6.1 秒，4 日合计约 35 秒。该结果只代表当次样本，不构成全窗口完备率或
  长期容量结论；正式结果仍以全量结构、事件、B0 与覆盖检查为准。
- 2026-11-08 新实现真实 CLI 验收（`qstock_test`，20261102–20261104 + D-1）：
  3 次因子 + 4 次全市场 raw 请求，40.14s；raw 日 4/4 成功，14 只事件票本地重建，
  001381 仍按新股无 D-1 规则 pending。原样续跑 0 上游请求、0.033s；不加 limit 的
  本地全量重建另含 5,210 只无事件票，全部 B0=1 且无 pending，3.40s、0 上游请求。
  产物含 `raw_days.csv`。该样本不构成全窗口成功率承诺。
- 2026-11-08 生产全窗口结果（`qstock`，20230101–20261104）：928 因子日全部命中
  断点，D-1 + 928 日全市场 raw 最终 929/929 成功；930 次 raw 尝试含 1 次重试。
  17,538 事件、4,443 事件票 + 931 无事件票，窗口 raw 4,746,054 行。首轮存在真实
  待修复项，故 `partial_error`/exit 1，并非采集中断。原样续跑 0 上游请求、342.60s
  （本地全量重建），报告与库内 pending 均为 454。全量只读检查中事件/B0/F/hfq 公式
  和事件不变量均 0 异常；pending 分类与后续处置记录于 #163/#164/#165。
