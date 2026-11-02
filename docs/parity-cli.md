# 迁移总验收对拍固定入口操作手册（`qstock-scheduler parity`）

issue #122（spec #111 总验收）：剔除东财迁移后，五个核心面（boards /
market_snapshot / indices / lhb / zt_pool）的跨源字段级对拍 + 库内历史
连续性检查的统一固定入口。本手册是该能力的 CLI 入口说明——一次开发、可重复
运行、可挂定时任务（如每周盘后巡检）；不使用一次性临时脚本。

## 它做什么

一条命令跑完总验收的两类检查，**全程不落库**（纯读库 + 逐源直取上游 +
THS 页面级探测）：

1. **逐面对拍**（复用各面既有 parity 工具，不重复造轮子）：

   | 面 | 对拍工具 | 比对口径 |
   | --- | --- | --- |
   | boards | `sw-boards --check` 同工具（三方位） | 申万官网收盘截面 vs 库内 market_boards 行 vs 库内自算 query_sw_board_daily |
   | snapshot | `snapshot --check` 同工具（三源） | 腾讯批量报价 vs 腾讯 rank 榜单 vs 新浪直连，逐字段容差比对 |
   | indices | `index_parity.compare_index_spot_sources`（本 ticket 新增） | 腾讯 vs 新浪指数快照逐 code 字段级比对（新浪量比为结构性缺项跳过） |
   | lhb | `lhb --check` 同工具 | 交易所官方 vs 新浪互补：股票集合 / 全日成交额 / 席位净买额 |
   | zt_pool | 跨源对拍**不可行**（东财不可达是迁移前提，ADR 0012 记录） | THS 封禁探测（页面级，不打 dataapi）+ 库内连续性为验收证据 |

2. **库内历史连续性**：逐交易日在库内表中查行数并对照采集台账归因——
   有行 = 已覆盖；无行但台账 ok/empty = 上游确认无数据（lhb/zt_pool 等
   事件驱动面为正常语义）；无行且无确认 = 断档（`ledger` 列区分未采集
   `null` 与采集失败 `error`）。indices 面复用 `index --verify` 的
   verify_index_coverage（对照交易日历逐指数列缺口，`missing_days` 即
   回填命令的 `--start` 定位，科创50 回填窗口由此显式呈现）。

## 适用场景

| 场景 | 命令要点 |
| --- | --- |
| 迁移总验收（五面全跑） | `parity --date <最近交易日>` |
| 只验收某几面 | `parity --faces boards,snapshot --date <日>` |
| 快速冒烟（抽样对拍） | `parity --limit 200 --date <日>` |
| 只看库内历史连续性 | `parity --no-probe`（连续性总是执行） |
| 离线/网络受限环境 | `parity --no-probe`（跳过 THS 页面探测） |

## 前置条件

- 已完成 editable 安装：`.venv/bin/pip install -e ".[sources,dev]"`；
- 环境变量 `PG_DSN` 指向目标库——**连续性检查读该库**（验收读业务库
  `postgresql://localhost/qstock`；开发自测用隔离测试库，如
  `postgresql://localhost/qstock_test_122`）。本命令**不写任何库**；
- `--date` 建议传最近交易日：非交易日（周末/休市）上游实时数据停留在
  最近交易日，官网"当日收盘截面"语义仍成立（收盘后调用=当日收盘截面）。

## 命令

```bash
# 1. 总验收：五面全跑（trade_date 缺省当日）
PG_DSN=postgresql://localhost/qstock qstock-scheduler parity --date 20261023

# 2. 面子集 + 抽样冒烟
qstock-scheduler parity --faces snapshot,indices --limit 200 --date 20261023

# 3. 跳过 THS 探测（离线环境/只读连续性）
qstock-scheduler parity --no-probe --date 20261023
```

参数说明：

- `--date`：对拍交易日 yyyymmdd，缺省当日。
- `--faces`：逗号分隔面集，缺省 `all` = boards,snapshot,indices,lhb,zt_pool。
- `--window`：连续性检查窗口自然日，缺省 120（约半年交易日）。
- `--limit`：快照/板块对拍抽样只数（冒烟用；indices/lhb 忽略）。
- `--no-probe`：跳过 THS 封禁探测（探测为单次页面请求，不打 dataapi，
  不违反低频纪律；默认开启）。

## 输出与退出码

stdout 为单一 JSON（日志走 stderr）：

- `parity.<面>`：各面 parity 工具原样输出（差异字段清单见各面
  `diffs`/`field_diff_counts`；`mismatch` 与 zt_pool `infeasible` 为对拍
  产出/已记录决策，**非失败**）。
- `parity_summary`：逐面对拍状态（zt_pool 恒为 `infeasible`，依据
  ADR 0012）。
- `continuity.faces.<面>`：逐面 `covered_days` / `empty_confirmed_days` /
  `gap_days`（含台账归因）/ 表首末日期；indices 面为
  `index_daily`（verify 输出，逐指数 `missing_days`）与 `market_indices`
  并列结构。
- `continuity_summary`：逐面 `ok`/`gap`。

退出码：`0` = 对拍源全部可用（连续性断档不进退出码——它是验收报告的
证据而非命令失败）；`1` = 任一面 parity 源失败（如官网/腾讯不可达）或
参数非法；`2` = argparse 参数错误。

## 回滚方法

本命令纯读 + 单次页面探测，**不产生任何写库副作用**，无需回滚。探测只
是 GET 一次 THS 页面（不打 dataapi），不触发也不重置"每日一次"低频守卫
（守卫读采集台账，仅 dataapi 抓取落账）。

## 与其他入口的关系

- 逐面深入对拍/单面重验仍用各面入口：`sw-boards --check`、
  `snapshot --check`、`lhb --check`、`index --verify`、`limitpool --probe`、
  `daily --check`（个股日线三源，supporting 面不在五核心面内）。
- 本入口是它们的**只读编排**：不替换、不改写任何一面工具的行为。
