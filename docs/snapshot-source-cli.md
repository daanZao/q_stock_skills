# 全市场快照固定入口操作手册（`qstock-scheduler snapshot`）

issue #116（spec #111）：market_snapshot 承载链迁移为 **腾讯批量报价（主源）→
腾讯 rank 榜单 → 新浪直连** 三层 fallback，东财链路（akshare spot_em）退出快照。本手册是该能力的固定 CLI 入口说明——抓取、三源对拍都走
同一入口，可重复运行、可挂定时任务；不使用一次性临时脚本。

## 适用场景

| 场景 | 命令要点 |
| --- | --- |
| 收盘后采集当日全市场快照 | `snapshot`（无参） |
| 校验快照归属日（主源时间戳含日期） | `snapshot --date <当日>`（与 API 生效日期不符即拒收零落库） |
| 迁移/换源后验证数据质量 | `snapshot --check`（三源直取对拍，不落库） |
| 对拍冒烟（先看几十只） | `snapshot --check --limit 50` |

## 前置条件

- 已完成 editable 安装：`.venv/bin/pip install -e ".[sources,dev]"`；
- 环境变量 `PG_DSN` 指向目标库（写库模式必需；`--check` 不连库）；
- 数据库表已初始化（`init_database` 或此前已运行过任一写库批）。

## 链路结构与口径

- **主源 腾讯 qt.gtimg.cn 批量报价**（80 只/请求，全市场约 70 次/轮
  ——5576 只实测 70 请求、26.3s）：
  18 个快照字段全覆盖、含北交所 920xxx、字段 30 时间戳含日期（可带参过
  #78 日期防线）、amount 原生万元、市值亿 ×1e4、成交量手 ×100（科创板
  原生股不换算）。需股票清单驱动：默认 akshare 交易所官网清单
  （`stock_info_a_code_name`）→ 库内 `stock_list` 兜底，双路皆不可用转
  rank 榜单。
- **第 2 位 腾讯 rank 榜单**（proxy.finance.qq.com，200 行/页约 28 请求/轮）：
  免清单；缺 open/high/low/pre_close（置 None 不伪造），涨跌家数口径改由
  change_percent（zdf）判定（repository 聚合 SQL）；pe_ratio 为 pe_ttm
  口径；无时间戳 → 只能无参调用。
- **第 3 位 新浪直连**（Market_Center.getHQNodeData，100 行/页约 56 请求/轮）：
  绕过 akshare `stock_zh_a_spot` 封装（其丢弃 per/pb/mktcap/nmc/
  turnoverratio 五列）；只缺振幅/量比；ticktime 无日期 → 只能无参调用；
  上游明示重复运行封 IP，仅作兜底，勿高频。

## 命令

```bash
# 收盘后采集（无参：主源报告交易日则用之，否则回退当天）
qstock-scheduler snapshot

# 带参校验归属日（主源时间戳含日期；不符即 date_mismatch 零落库）
qstock-scheduler snapshot --date 20261021

# 三源对拍：逐源直取、字段级比较，不写库（差异含已知 PE 口径切换信号）
qstock-scheduler snapshot --check

# 对拍冒烟：只比前 50 只（按代码序）
qstock-scheduler snapshot --check --limit 50
```

参数说明：

- `--date`：仅主源（时间戳含日期）可带参过 #78 防线；rank/新浪兜底成功时
  API 未报告交易日，带参调用会被 date_mismatch 拒收（零落库）——兜底
  场景请无参调用。
- `--check`：逐源独立直取（单源失败不中断），按代码交集与参考源（有数据的
  最高优先级源）逐字段比对；结构性缺项跳过（rank 缺 OHLC/pre_close、新浪
  缺振幅/量比，见输出 `structural_gaps`）；`pe_ratio` 差异属已知口径切换
  （三源 PE 口径不同）。
- `--limit`：只对拍前 N 只（抓取模式忽略）。
- 重复执行幂等：`(trade_date, stock_code)` 业务键 upsert，采集台账
  append-only 追加。

## 落库与台账

- 写 `market_snapshot`（18 数据字段 + source），业务键
  `(trade_date, stock_code)` 幂等 upsert；`source` 记实际成功源
  （`tencent` / `tencent_rank` / `sina`）。
- 采集台账落 `market_snapshot` 一行（status ok/empty/error + error_kind +
  逐源 attempted_sources 明细），与 MCP `fetch_market_snapshot` 同路径同语义。
- 全失败按三态契约分类：`upstream_unreachable`（连接级）/ `fetch_failed`，
  exit 1；上游正常响应空结果为 `no_data_for_date`（exit 0，非故障）。

## 速率与风控实测记录

- **主源一轮全市场实测（2026-10-22 02:34 CST）**：70 次批量请求（5576 只
  ÷ 80/批，另加 1 次 akshare 清单请求），全程 **26.3s**（含 0.1s/批限速）。
- **三源对拍实测（2026-10-24 CST）**：154 次请求（70 qt + 28 rank + 56
  sina），全程 **66.8s**。腾讯两源 66,924 个可比值**零差异**（量/额/市值
  换算口径互证一致）；新浪 78,064 个可比值 5,064 个差异，集中在 pe_ratio
  （4,762，已知口径切换信号）与 pb_ratio（231）。
- qt.gtimg.cn 为公开无鉴权报价端点，无官方限流文档；70 请求/轮已含
  0.1s/批自控节奏，盘中请勿挂高频轮询（每日 1–2 次：close 后与复盘前）。
- 腾讯 proxy.finance.qq.com 与 K 线端点同域，实测连发 5–6 请求可触发
  TLS 断连——兜底链路已内置 0.3s/页限速，不建议再收紧。

## 冒烟记录

- 2026-10-22 02:34 CST `snapshot` 真实抓取指向测试库 `qstock_test_116`
  （业务库零写入）：主源成功，**5576 行落库**（trade_date=20261021，
  API 时间戳日期优先），沪深北分布 2318/2906/352——**北交所 352 只
  920xxx 全覆盖**，价格/成交额零 NULL。
- 2026-10-24 CST `snapshot --check` 三源对拍：三源全市场直取全部成功
  （tencent 5577 / rank 5577 / sina 5576 行），差异明细见上节；对拍
  `mismatch` 退出码 1 为设计行为（差异计数 > 0，主因已知 PE 口径切换）。
- 日期防线实测：带参 `--date` 与 API 生效日期不符即 `date_mismatch`
  零落库（测试用例覆盖：tests/test_scheduler.py
  `test_snapshot_fetch_date_mismatch_guard_zero_writes`）。
