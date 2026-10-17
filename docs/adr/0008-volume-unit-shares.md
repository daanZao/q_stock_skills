# ADR 0008：库内 volume 统一为股，换算收敛在适配器映射层

2026-10-17，issue #79（spec #71/W3）。

## Context

东财系（efinance/akshare）日线 `成交量` 单位为**手**，baostock `volume` 为**股**
（调研互证见 `docs/research/volume-unit-audit.md`，issue #56）。适配器原样透传导致
`stock_daily.volume` 混源量纲跳变 100 倍，一切量纲指标（均量、量比、量价相关）在
跨源序列上失真（09 取证 002813 案例，`files/09-historical-review-hardening.md` W3）。
ADR 0006 只规约了金额单位（万元），volume 无规格是缺口。

## Decision

- **库内所有 volume 列标准单位 = 股**（stock_daily / index_daily / market_snapshot /
  market_indices）。
- 换算收敛在**适配器映射层**（与 ADR 0006 金额同层）：东财系 `_gu()`（×100，
  `adapters/_eastmoney.py`，`_board_em.py` 指数映射复用）；baostock 原生为股，
  透传不换算。
- **存量迁移为一次性运维动作**（`src/qstock_mcp/migrate_volume.py`，
  `python -m qstock_mcp.migrate_volume`）：`--audit` 全量机检（amount×1万÷volume
  反推单价与价格列比值判股/手，与 source 列互证，互证矛盾与缺列行进异常清单），
  `--apply` 只迁移判定为"手"且互证一致的行（分批 UPDATE）；已迁移行复核为股
  不再命中，天然幂等。生产执行前人工确认机检清单。
- market_indices 无 source 列（盘面表 DDL 决策），机检按东财系口径计（盘面
  section 实为东财系单源）。

## Consequences

- 新落库数据全部股口径；迁移完成前混源序列量纲仍不连续，消费方（指标管道）
  以迁移完成时间为准。
- 新增数据源适配器：volume 必须换算为股后落库，映射层单测需含量纲断言
  （`tests/test_adapters.py` 先例）。
- 同一坑存在于 q_stock_analysis 的 data_provider（跨仓库同步，不在本 spec 实施范围）。
