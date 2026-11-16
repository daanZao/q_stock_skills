# Reconcile checkpoint — 2026-11-16

- checkpoint commit：`5fcb09265ebd2b0ec3164a7ebb4cc3987b401f2a`
- spec：#158，最近修订 `2026-11-14T04:40:44Z`
- 当前任务：#167（持续采集）；独立提交拆分：实现、文档修正、reconcile 技能。

## Ticket set

- #161、#162、#166：closed。
- #158、#167、#183、#184：open。
- #168：北交所专项，明确从 #167 移出，不属于本次交付。

## Invariants

| id | statement | source of truth | consumers |
| --- | --- | --- | --- |
| I-01 | 累计 B 仅取 baostock `backAdjustFactor`；`adjustFacto(r)` 仅审计。 | #158 §2.2；ADR 0018 | 所有 raw+B 写入与派生 |
| I-02 | 金额万元、成交量股；baostock amount 在适配层由元除以 10000。 | AGENTS.md 数据契约；ADR 0006/0008 | 所有落库入口 |
| I-03 | 只有覆盖 `QSTOCK_ADJ_WINDOW_START` 且事件集完整的全窗口采集可建 B0；morning 仅新股首日例外。 | #158 2026-11-14 增补 §1–§4 | init、adj-backfill、daily --all、morning |
| I-04 | daily/T+0 只消费 B0；无锚只写 raw+因子证据，NULL/pending，不生成或改写 hfq/qfq。 | #167 2026-11-14 增补；CONTEXT 窗口基线 B0 | 单票 daily、query_daily 自愈 |
| I-05 | daily 窄窗保留全量 pending；只有纯 alternative_raw 原因且全部替代行已权威覆盖时才解除，其他原因保留。 | ADR 0018；#167 门禁与 T+1 修订 | daily、morning、修复验收 |
| I-06 | 0.1% 对账只覆盖 `rebuild_from` 起受影响事件；mismatch/unverified 进入 repair 并阻止 complete。 | #158 §3.4、§5.2 | daily、morning、backfill |
| I-07 | 所有替代 raw 日期都纳入后续 baostock 升级；全窗口回填复用已有可信 B0，不覆盖为 NULL。 | #158 增补；#167 T+1 AC | daily、adj-backfill |
| I-08 | baostock 全市场响应按适配器+日期复用；force 不重复请求；raw/因子每次实际调用及 CLI throttle 均至少 1.5 秒，串行。 | AGENTS.md 数据源纪律；ADR 0010/0019 | daily、adj-backfill、init |
| I-09 | 所有可自愈的 MCP 数据工具入口均验证实例租约；无锁先询问用户，持有实例显式 release。 | ADR 0019；docs/baostock-lease.md | server 与 skill 家族 |
| I-10 | 代码完成、数据处理完成、八项验收通过、生产切读分别验收；#183/#184 不被本票替代。 | #158 §11；#184 | 生产执行 |

## Defect ledger

| id | class | owner | status |
| --- | --- | --- | --- |
| D-01 | 查询窗误建 B0/隐式 B0=1 | #167 | closed：`allow_baseline_init` 仅全窗口/新股首日；无锚不写派生 |
| D-02 | 跨股票因子证据误复用、force 重复请求 | #167 | closed：本股票/全市场证据隔离 + 同日响应 cache |
| D-03 | pending 无原因可能被误清 | #167 | closed：`alternative_raw` 原因台账 + 其他 repair 原因保留 |
| D-04 | 中段替代 raw 未升级、窄窗覆盖已有 B0 | #167 | closed：全部替代日期升级；窄窗复用已有 B0 |
| D-05 | raw→因子连发、throttle 可低于 1.5 秒 | #167 | closed：统一上游时钟与 run/CLI clamp |
| D-06 | 撤销票 pending/verified 与结构验收签名不一致 | #167 | closed：显式 pending、retracted verified、summary 权限透传 |
| D-07 | 文档与实现分叉 | 文档提交 | closed：CONTEXT、ADR、README、手册、skill 同步 |
| D-08 | reconcile 技能混入实现交付 | reconcile 独立提交 | open until split commit |

## Next trigger

- #183 生产数据修复与 #184 生产切读之前，必须重新执行 full reconcile。
- 默认每 10 个关闭任务或 7 天；规范句、B0/pending/租约不变量变化时立即执行。
