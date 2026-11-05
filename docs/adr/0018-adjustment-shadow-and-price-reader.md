# ADR 0018：复权影子模型与个股价格读取 expand 接缝

2026-11-04，issue #158/#159/#160；一手证据见 `docs/research/2026-11-04-adjustment-core-backfill.md`。

## Context

legacy `stock_daily` 同时承担多口径价格存储与所有消费方读取，直接切换会把查询、自算强势股、覆盖核验和板块自算分散修改。baostock 原始响应还证明 `adjustFacto` / 单票 `adjustFactor` 不总是累计因子，不能作为业务 B。

## Decision

- 累计 B 只取 `backAdjustFactor`；`adjustFacto(r)` 仅可留原始快照审计，不进入 B、f、F 或派生。
- 影子模型采用 `f=B_prev/B_cur`、`F=B/B0`、`hfq=raw×F`、`F_latest=B_latest/B0`、`qfq=hfq/F_latest`。首次窗口事件以权威 raw+pctChg 计算 `B0=B1×f1`，缺证据则 pending，不猜基线。
- `stock_daily_raw`、`stock_adjust_factor`、`stock_daily_hfq`、`stock_factor_state` 为四张影子表；raw/事件支持同键修订，权威整日响应替换该日事件集以支持改期和撤销。qfq 只标量派生，不物化。
- `price_reader.py` 是所有个股价格读取的唯一接缝，覆盖区间行、日期、横截面 adj、强势股窗口与板块聚合。`QSTOCK_PRICE_SOURCE` 缺省为 `legacy`；#162 注册 `new` 区间派生读取，测试可注册替代实现，未注册值显式失败。
- #159/#160 是 expand 与 walking skeleton；#161 接入 morning，#162 接入 `query_daily` 纯读派生与覆盖门控，#163 接入 CLI 回填 A。生产缺省仍读 legacy，正式切流、daily/T+0 自愈与完整迁移验收由后续票据承接。
- #163 回填 A：整日事件替换与受影响 `stock_factor_state.rebuild_pending` 标记同事务，撤销票另落 `stock_factor_retracted` 标记；hfq/state 由随后单票重建更新，崩溃续跑可据标记与 pending 补齐。每日响应中与上一个事件 B 相等的行不写事件表（无价格阶跃，不造 `f=1` 占位）。回填 `--end` 必须覆盖库内最新复权事实；历史窗口在有更新数据时直接拒绝，避免回退窗口外派生。

## Consequences

- 后续 new 实现只需接入价格读取接缝，不再逐个修改消费方。
- 四表业务哈希排除运行时间戳；修订/撤销须同事务重建事实与派生。
- 无效 close 留在 raw 供审计、不得派生，并写 `stock_daily_raw/invalid_close` 台账；pending/隔离均不得判 complete。
- 窗口首日事件所需的窗口外 D-1 raw 作为「窗口基线 D-1 输入」保留在 `stock_daily_raw` 供审计/续跑，但不进入 hfq、事件集或窗口查询交付；它不是 legacy 价格，也不用于从旧 qfq 反推。
- ADR 0004/0010 仍描述当前 legacy 采集路径；后续切流票据必须显式更新其“qfq 落库”结论，不能把本 ADR 的影子模型误读为已切换生产行为。
