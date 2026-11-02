# 股票清单固定入口操作手册（`qstock-scheduler stock-list`）

issue #122（spec #111 长尾验收）：股票清单链（akshare 交易所官网 →
baostock，efinance 已随 issue #121 退役）的健康巡检与冒烟固定入口。清单
承载链之外，本入口同时是迁移期清单面多源互证的留证工具（bse.cn WAF /
baostock 兜底的当前真实分工由此可查）。本手册是该能力的 CLI 入口说明——
可重复运行、可挂定时任务；不使用一次性临时脚本。

## 适用场景

| 场景 | 命令要点 |
| --- | --- |
| 清单例行刷新落库（巡检/初始化补清单） | `stock-list`（按 code 幂等 upsert stock_list 并落台账） |
| 清单链多源互证（不落库） | `stock-list --check`（逐源独立抓取比对行数/代码集/北交所覆盖） |
| 迁移期清单链分工留证 | `stock-list --check`（akshare WAF / baostock 兜底逐源记录） |

## 前置条件

- 已完成 editable 安装：`.venv/bin/pip install -e ".[sources,dev]"`；
- 环境变量 `PG_DSN` 指向目标库（**fetch 模式写 stock_list 并落台账**：
  业务库执行前请阅读下方"回滚方法"；验证/冒烟用测试库，如
  `postgresql://localhost/qstock_test_122`。`--check` 模式不触库）；
- 数据库表已初始化（`init_database` 或任一写库批执行过，幂等建表）。

## 命令

```bash
# 1. 清单抓取落库（fallback 链：akshare 交易所官网 → baostock；
#    按 stock_code 幂等 upsert；落采集台账 section=stock_list）
PG_DSN=postgresql://localhost/qstock_test_122 qstock-scheduler stock-list

# 2. 多源互证（逐源独立抓取，不落库不写台账）
qstock-scheduler stock-list --check
```

fetch 模式输出要点：`source`（实际承载源）、`rows_upserted`、
`bse_rows`（北交所 4/8/9 开头代码行数——akshare 交易所官网源含北交所腿、
baostock 兜底不含，见 `--check` 的 `bse_coverage` 逐源对照）、
`attempted_sources`（逐源尝试明细）。

check 模式输出要点：`reference_source`（有数据的最高优先级源）、
`comparisons`（逐源行数 + 代码集合双向差集样本与计数——清单口径各源
不同，差异为对拍产出非失败信号）、`bse_coverage`（逐源北交所覆盖数）。

退出码：fetch 模式 `0` = ok 或 empty（上游确认无数据，正常语义非故障；
`1` = 全链失败，台账记 error）；check 模式 `0` = ok 或 mismatch（差异为
产出），`1` = 全部源失败。

## 回滚方法

fetch 模式按 `stock_code` 幂等 upsert `stock_list` 一张表（不清表、不动
其他表）；需要撤销时从备份恢复该表或用下列 SQL 按来源删除本次写入：

```sql
-- 以输出中的 source 值为准（如 'baostock'）
DELETE FROM stock_list WHERE source = '<本次source>';
```

台账 `collection_log` 为 append-only 审计表，保留记录不删除。
check 模式无任何写副作用，无需回滚。

## 已知现状（迁移期）

- efinance（东财）适配器本机连接级不可达（spec #111 前提），已随
  issue #121 整体退役，清单链定稿为 akshare → baostock；退役前链首腿
  恒失败的逐源记录在 #121 前的 `--check` 输出中留证。
- akshare 交易所官网源（`stock_info_a_code_name`）内含北交所腿（bse.cn），
  其 WAF 拦截 python 客户端导致整函数失败（issue #108）——修复前清单链
  实际降级为 baostock 单源兜底（#100 调研结论），`--check` 的
  `bse_coverage` 使该现状逐源可见。
