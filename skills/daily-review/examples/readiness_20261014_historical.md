# 历史模式就绪检查演练：20261014（台账启用前日期）

spec #71/W5 验收演练（issue #85）：以 20261014 为历史场景跑数据就绪检查，
验证**零 fetch 调用**、就绪归因**先读台账**（`query_collection_log`）。

## 场景

- 复盘日 `trade_date=20261014` < 执行日 20261017 → 步骤 1 判**历史模式**：
  `fetch_board_snapshot`/`fetch_market_snapshot`/`init_database`/`mx_search`
  一律不调（服务端防线为第二道，不依赖它兜底）。
- 20261014 是 spec 取证日：`market_boards` 当日缺失（板块节降级案例日）。

## 台账归因（query_collection_log，trade_date=20261014）

| section | 台账行数 | 判定 | 归因 |
| --- | --- | --- | --- |
| boards | 0 | 未采集 | 台账启用前（20261017），采集情况不可考 |
| zt_pool | 0 | 未采集 | 同上 |
| strong_stocks | 0 | 未采集 | 同上 |
| indices | 0 | 未采集 | 同上 |
| market_snapshot | 0 | 未采集 | 同上 |
| lhb | 0 | 未采集（可选 section） | 同上 |

台账启用日之前的日期恒无台账行，归因统一写"台账启用前，采集情况不可考"，
按"未采集（非正常空数据）"同等进 `data_gaps`——与"上游确认无数据"
（`status=empty`）严格区分。

## 表行数探测（纯查询，确认实际就绪状态）

| 表 | count | 就绪 |
| --- | --- | --- |
| market_boards | 0 | 缺（与 09 取证一致：当日板块未落库） |
| zt_pool | 71 | 就绪 |
| strong_stocks | 196 | 就绪 |
| market_indices（000001 探测） | 1 | 就绪 |
| market_snapshot（aggregate=breadth,amount） | 有聚合 | 就绪 |

## 判定线适用

核心 section 永久缺失 1 项（boards）< 2 → 直接降级出报，板块轮动节切换
申万行业自算口径（见 SKILL.md"板块节降级"节），就绪台账随报告落盘。
若缺失 ≥2 项，开工前提示用户确认（默认继续）。

## 复现

```bash
PG_DSN=postgresql://localhost/qstock .venv/bin/python - <<'PY'
from qstock_mcp.tools_ledger import query_collection_log
from qstock_mcp.tools_board import query_board_data
for section in ("boards", "zt_pool", "strong_stocks", "indices", "market_snapshot"):
    print(section, query_collection_log(trade_date="20261014", section=section)["count"])
print("market_boards", query_board_data("market_boards", trade_date="20261014")["count"])
PY
```

（演练前先 `ensure_schema` 把 collection_log 建到目标库；本演练于 20261017
对 qstock 库执行，全部 DDL 幂等。）
