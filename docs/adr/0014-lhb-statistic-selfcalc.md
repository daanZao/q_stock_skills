# ADR 0014：lhb_stock_statistic 由库内 lhb_basic 滚动自算（新浪 ggtj 回归对拍校验位）

2026-10-25，issue #114（spec #111；code-review 修正，PR #123）。

## Context

- issue #114 验收标准：**交易所官方源覆盖 lhb_basic / lhb_stock_statistic
  两表**字段，新浪五函数留作互补校验。#97 调研落地时把 statistic 子表交给
  新浪 ggtj（akshare `stock_lhb_ggtj_sina`）承载，窗口自东财「近三月」改为
  「近 60 日」——承载源与 ticket 要求冲突（交易所官方源只覆盖了
  lhb_basic），spec #111 收尾 code-review 发现。
- 反转的实质障碍只有一个：statistic 的 `buy_seat_count`/`sell_seat_count`
  两列需要席位级计数，而 lhb_basic 不持席位信息（席位明细仅用于当日金额
  自算，lhb_stock_detail 席位级表 schema 在、写路径未接线）——历史窗口
  内的席位计数无法从库内派生。

## Decision

- **statistic → 库内自算**（交易所官方数据派生，满足 ticket 两表覆盖）：
  `fetch_board_snapshot(lhb 段)` 落 lhb_basic 后由
  `repository.recompute_lhb_statistic` 以**近 60 日历日窗口**（含当日，
  承接 ggtj「近 60 日」窗口语义）按股聚合——appear=窗口行数（业务键含
  lhb_reason，同股当日多榜单各计一次）、buy/sell/net=窗口合计（万元，
  ADR 0006）。DELETE+INSERT 幂等可重跑，窗口滑出/上游修订自愈。
- **新浪 ggtj 回归 `--check` 对拍校验位**（`SinaLhbClient.ggtj` +
  `tools_lhb._compare_statistic_snapshot`）：库内自算快照 vs ggtj 逐股比对
  上榜次数（精确）与买/卖/净额（rel 0.005），不落库；ggtj 窗口锚定采集
  执行日，与目标日非同日时差异含窗口滑移成分，输出 note 声明，供人工核对。
- **buy/sell_seat_count 恒 NULL（不伪造）**：与 close_price/change_percent、
  yyb 缺列同一处置——交易所官方汇总无席位计数来源。接线
  lhb_stock_detail（席位级写路径）后可派生升级，本期不实施（能力增量非
  恢复，研究报告已有定位）。
- 适配器契约随之收窄：`ExchangeLhbAdapter.fetch_lhb` 返回
  basic/yyb_capital/yyb_most 三子表；`lhb_stock_statistic` 从
  `BOARD_SECTION_TABLES` 移除（不再走适配器 upsert）。

## Consequences

- ticket「两表字段覆盖」仅剩席位计数两列缺口，以上述 NULL 处置记录在本
  ADR 与 issue #114 评论（gh issue comment 114）。
- `--check` 从「不连库」变为「只读连库」（读 statistic 自算快照；不落库、
  不落台账），手册 `docs/lhb-source-cli.md` 已同步。
- 历史回溯逐日重算 statistic 为 O(天数 × 窗口内股票数)，单日毫秒级，
  可忽略。
- ggtj 对拍为软校验：窗口锚点不同日时不判严格失败，差异供人工核对——
  与 basic 对拍的「集合差信息级」同构。
