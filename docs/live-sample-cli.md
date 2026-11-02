# live-sample：真实上游分层抽样验收

`live-sample` 用固定种子执行代表股 90 交易日深度样本和申万板块成分全集 10 交易日广度样本。结果只覆盖输出中的本轮 Universe，不是全市场验收。

## 运行

```bash
export PG_DSN='postgresql://localhost/qstock_test'
qstock-scheduler live-sample --seed 134 --end 20261028 \
  > files/live_sample_20261028.json
```

**隔离与清空语义**：数据库名必须以 `qstock_test` 开头。命令会先重建该库的 public schema，再写入本轮数据；非测试库在任何上游请求前拒绝执行。不要指向业务库，也不要与其他使用同一测试库的任务并发运行。

依赖真实清单、腾讯→新浪→baostock 日线链和申万分类文件。运行前按项目安装方式准备 `sources` 与 `classify` 可选依赖。

## 参数

| 参数 | 缺省 | 含义 |
| --- | --- | --- |
| `--seed N` | `134` | 固定股票与板块抽样种子；更换种子轮换样本 |
| `--end YYYYMMDD` | 最近已收盘交易日 | 两类样本的共同截止交易日 |
| `--throttle SEC` | `1.0` | 每次个股查询后的串行间隔；`0` 仅供 fake 自动化测试 |

日线不启用并发。1 秒/只沿用 2026-10-21 腾讯连接级限速实测；最小安全间隔和 TPS 上限仍未验证，不把该参数解释为上游 SLA。

## 样本规则

- 上海主板、深圳主板（含原中小板）、上海科创板、深证创业板、北交所各初始 3 只，共用同一 90 交易日窗口。
- 窗口起点后才开始有行情标记 `new_stock` 与 `history_insufficient`，末尾缺行情也标记历史不足；中间缺行单独列入 `missing_days`，不直接等同于新股。无数据、历史不足、连续 20 交易日及以上停牌标记为 `sample_limited`。停牌依据分为显式行 `marked_rows` 和连续缺行 `missing_rows`。每组只在出现这些样本限制时补抽 1 只；补抽仍不合适则记录 `replacement_still_limited`，不递归。采集 `error` 与正常无数据分开报告，不触发补抽。
- 从截止日 point-in-time 申万成分派生一级/二级板块，固定种子选择 2–3 个，优先合计 100–300 只；板块内股票全集共用同一 10 交易日窗口，不裁剪。
- `stock_list` 只写代表股尝试样本与板块成分的本轮 Universe。`coverage.stocks` 也仅限该 Universe，不覆盖其他股票。

## 输出与退出码

stdout 为单个 JSON，包含 seed、初始/补抽代码与原因、实际窗口、板块代码和成分数、逐股票行数/停牌/缺数/来源/耗时、Universe、coverage 及汇总。`summary.sources` 分别记录清单、申万字典和日线来源。

- `0`：采集无失败；`sample_limited` 是已披露的样本限制，不是采集失败。
- `1`：存在采集失败或前置检查失败，JSON 为 `partial_error` / `error`。
- `2`：CLI 参数解析错误。

自动化契约使用 fake 上游，不触网：

```bash
.venv/bin/pytest -q tests/test_live_sample.py
```
