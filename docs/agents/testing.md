# 分层测试门禁

本指南定义文档、影响面、全量和真实上游四层验证。生产侧可以提供全市场命令，测试证据只覆盖实际执行的样本；不得用抽样结果宣称真实全市场通过。

## 层级与门禁

| 层级 | 触发条件 | 上游与数据库 | 时间预算 |
| --- | --- | --- | --- |
| `docs` | 仅文档、CLI 手册或测试说明变更 | fake 上游；可用 `qstock_test` | 常规环境目标 ≤2 分钟 |
| `impacted` | Python、SQL、适配器或 CLI 行为变更 | fake 上游；按用例使用 `qstock_test` | 常规环境目标 ≤5 分钟 |
| `full` | 阶段性集成、 nightly 或高风险变更 | fake 上游；`qstock_test` | #133 后目标 ≤5 分钟，未达标时报告最慢用例 |
| `live` | 数据源、解析、抽样或发布验收 | 真实上游；仅隔离测试库 | 记录实测，不设未经验证的 SLA |

时间预算是反馈环目标，不是已验证性能指标。实际耗时按 #135 的正常测试采集机制记录，不为测性能额外增加一轮。

## docs：文档与 CLI 说明

先运行静态检查，再运行首次使用契约和受影响 CLI/核心测试：

```bash
git diff --check
.venv/bin/pytest -q tests/test_first_run.py
# 按实际修改的 CLI 追加对应测试文件，例如：
.venv/bin/pytest -q tests/test_lhb_tools.py tests/test_lhb_exchange_adapter.py
```

文档审查必须核对命令可执行、参数与实现一致、测试证据声明没有超出实际样本。仅文档变更不默认运行 `full`，也不执行真实上游回补。

## impacted：按影响面选择

先跑修改文件直接对应的测试，再跑共享契约；不为“覆盖率形式”扩大范围。

| 修改范围 | 必跑测试 |
| --- | --- |
| `tools_lhb.py`、`tools_board.py`、LHB CLI | `test_lhb_tools.py`、`test_fetch_board.py`、`test_collection_log.py` |
| LHB 适配器或解析器 | `test_lhb_exchange_adapter.py`、`test_lhb_parsers.py`、`test_lhb_ths_adapter.py` |
| `fetch_chain.py` | `test_fetch_chain.py` 及受影响数据源的核心测试 |
| `daily` 链 | `test_daily_chain.py`、`test_fetch_daily.py` 及对应适配器测试 |
| `index` 链 | `test_index_chain.py` 及对应指数适配器测试 |
| 申万板块或自算 | `test_sw_boards.py`、`test_sw_backfill.py`、`test_sw_board_daily.py` |
| schema / repository | 对应 repository 测试和所有受影响写入面的核心测试 |
| MCP 输出契约 | 对应工具测试及消费方契约测试 |

共享 PG 测试库由 pytest 与 live-sample 共用 runner 锁，不并行启动多个触库进程。测试失败时先缩短到失败用例定位，不以跳过失败或缩小断言收尾。

## full：阶段性自动化回归

```bash
.venv/bin/pytest -q tests/
.venv/bin/pytest -q skills/indicator-tools/tests/
```

`full` 使用 fake 数据源，不访问真实上游。它用于 nightly、阶段性集成或高风险变更，不是每个小编辑的机械步骤。#133 负责降低 PG fixture 成本；性能未达标时公开最慢用例，不靠减少测试覆盖达标。

## live：真实上游抽样

真实全市场 90 日或 10 日流程不属于测试方案。live 只执行 #134 的两类隔离样本：

1. 五个市场分组的代表股 90 日深度样本，固定种子、每组初始 3 只，异常股票最多增补一次；
2. 2–3 个一级/二级板块的成分股全集 10 日广度样本，隔离库中的 `stock_list` 只包含本轮 Universe。

live 不属于常规提交门禁。结果必须包含种子、代码、板块、替换原因、实际窗口、耗时和来源，不得写成“全市场通过”。固定入口如下，命令会清空指定的 `qstock_test*` 隔离库：

```bash
PG_DSN=postgresql://localhost/qstock_test \
  qstock-scheduler live-sample --seed 134 --end 20261028
```

参数、样本限制和输出契约见 `docs/live-sample-cli.md`。

## 完成证据

每次交付记录实际执行的层级、命令、测试数量、耗时和未执行项。下列情况不得声称完成：

- 目标测试失败、被跳过或以缩小断言通过；
- 把 fake 结果写成真实上游结果；
- 把代表股或板块样本写成真实全市场结果；
- 把未运行的 full/live 层级写成已通过。
