# ADR 0009：指标管道剔除停牌占位行，不填充不伪造

2026-10-17，issue #81（spec #71/W2）。

## Context

query_daily 对停牌交易日产出占位行（issue #80：`tradestatus=0` 权威标注，
缺失时启发式——OHLC 全等且 volume/change_percent 为 null）。indicator-tools
的 `parse_cols` 原契约对 OHLCV/amount 六列全有全无校验，停牌占位行
（volume/amount 为 null）直接 exit 1，002813/002860 型含停牌区间的序列
任何指标都算不出末点（09 取证案例，`files/09-historical-review-hardening.md` W2）。

## Decision

- **剔除，不填充不伪造**：停牌占位行在 `parse_cols` 入口按"无该交易日数据"
  剔除，剔除后序列喂指标计算、交集对齐（corr/deviation）与最小长度检查。
- **判定顺序**：有标注以标注为准（`suspended=true` 或 `tradestatus=0`；
  `suspended=false` 显式标注的行不剔除）；无标注时兜底启发式（OHLC 全等
  且 volume/change_percent 为 null），与 #80 服务端标注口径同源。
- **严格性不放松**：剔除仅覆盖停牌占位形态；其余行的必需列缺失/非数值
  仍 exit 1（全有全无校验继续严格）。
- **可观测**：剔除明细以 `excluded_suspended`（`{"count", "dates"}`）记入
  响应——单标的平铺、`corr` 按标的代码分键、`deviation` 按 stock/benchmark
  腿分键；无剔除时字段不出现（输出结构对无停牌输入零变化）。

## Consequences

- 含停牌区间的个股序列 deviation/corr/vol_price 等可算出末点，dev10/dev30
  与"仅按有效交易日对齐"口径一致。
- 停牌日被剔除而非按 0 填充：均线窗口不掺假量，deviation 累计不含 0% 伪偏离；
  代价是停牌期间的"持仓不变"语义由消费方自行理解（复盘 skill 已知悉
  excluded_suspended 字段）。
- 消费规则成文于 `skills/indicator-tools/SKILL.md` 输入契约小节。
