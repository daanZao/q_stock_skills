# ADR 0010：个股日线 fallback 链改为 腾讯 → 新浪 → baostock（东财退出）

2026-10-21，issue #112（spec #111；调研 #100/#104）。

## Context

- 2026-10-19 东财 push2 行情接口对本机连接级不可达，个股日线原链
  efinance → akshare → baostock 的前两源都是东财，只剩 baostock（T+1、北交所
  by-design 拒绝、无换手率列）可用。
- #104 收盘后实测（15:37）：腾讯 K 线（qfq）与新浪日线当晚均返回当日行
  （T+0），沪/深/北三只代表股 OHLC 逐值一致；baostock 仍为 T+1。
- 北交所：baostock 不支持；腾讯 `bj920xxx` 三复权口径实测可取。旧码
  43x/83x/87x 在腾讯侧冻结在迁码前（430047 停 2020、833171 停 2022），必须按
  北交所官方新旧代码对照表归一化到 920 新码。
- akshare 封装 `stock_zh_a_hist_tx` / `stock_zh_index_daily_tx` 实测把第 6 列
  成交量误命名为 amount、丢弃真实成交额列，且符号归一化不含 920xxx——不满足
  ADR 0006/0008 的金额/成交量口径。

## Decision

- **新增 `TencentAdapter`（首源）**：自研 stdlib urllib 薄封装直调
  `proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get`，按原始列位解析
  `[date, open, close, high, low, volume, {}, turnover%, amount万元]`。单请求
  上游上限 640 根且忽略 start，适配器按 end 逐段向前回溯、本地裁剪区间。
  单位换算：科创板 sh688/sh689 的 volume 实测已是股，其余（沪深主板/创业板/
  北交所）为手 ×100 入库；amount 原生万元；turnover 原生百分数。
- **新增 `SinaAdapter`（次源）**：复用 akshare `stock_zh_a_daily`（新浪源，
  非东财；JS 解密与复权因子合并不重复实现）。amount 元→万元、turnover
  fraction→百分数、volume 原生股。全历史下载慢且上游明示易封 IP，故列第二。
- **baostock 第三（兜底）**：保留 `tradestatus` 权威停牌列；`_FIELDS` 补 `turn`
  恢复 `turnover_rate`（个股与指数接口共用字段，`index_daily.turnover_rate`
  同步恢复为 baostock 实测值）。
- **链成员切换**：`default_daily_adapters()` = 腾讯 → 新浪 → baostock；
  `fetch_daily`/`query_daily` 自愈与 `init_database(backfill_history=True)`
  全量回溯均走该链。`AkshareAdapter.fetch_daily`（东财 `stock_zh_a_hist`）
  显式抛 `FetchError(kind="unsupported")` 退役，避免误接线后静默走回东财。
  其他能力面（盘面/快照/清单）仍用 `default_adapters()`，由 #111 后续 ticket
  逐个迁移——本 ADR 只约束个股日线。
- **北交所归一化**：`adapters/_bse_codes.py` + `data/bse_code_map.csv`（北交所
  官方新旧代码对照表 248 条，2025-10-09 全面切换完成）。请求前把旧码归一化到
  920 新码；对照表未覆盖的旧码原样请求（保留冻结历史可取）。归一化只作用于
  请求，`stock_daily.stock_code` 仍记录调用方传入的代码。
- **复权键缺失的语义**：请求 qfq/hfq 但上游只给 `day` 时，按"无复权事件"
  处理并返回 day 数据（实测 920047 三口径数值一致）；不臆造调整序列。
  **回落可见化**（2026-10-21 双轴评审后补）：发生时置适配器
  `adjust_fallback` 标记并记 WARNING 日志，fetch/query 响应 note、采集
  台账 detail、`daily --check` 源条目 note 三处同步标注，不静默落库。
- **三源对拍**：`daily_parity.compare_daily_sources` 逐源直取同一交易日 bar 做
  字段级比较、不落库。`qfq`/`none` 全字段；`hfq` 只比成交量/成交额/换手率——
  各源后复权基准不同，价格类字段直接比会误报。固定入口
  `qstock-scheduler daily --check`。
- **限速**：多段回溯段间 0.3s（实测连发 5–6 个腾讯请求触发 TLS 断连）；
  CLI `--all` 缺省 1 秒/只。

## Consequences

- 收盘后当晚即可落 T+0 当日行；个股日线不再依赖东财。
- 腾讯单请求 640 根：全历史回溯逐股约 19 次请求，属重操作；日常全市场 T+1
  补数仍优先 `morning` 批（baostock 一把抓约 20s）。
- 新浪走全历史下载（老股 6.5–15.4s）且封 IP 风险有官方文档背书，只作腾讯
  故障时的备选，不作主源。
- 腾讯/新浪无 `tradestatus`：两源写入行为 null，停牌语义仍靠 baostock 兜底与
  ADR 0009 的启发式；腾讯/新浪的 change_percent/change_amount/amplitude 为
  前收推算值，与 baostock/东财原生列可能有末位差异（对拍容差已覆盖）。
- 北交所旧码/新码若混用会形成两条 `stock_daily` 序列；统一代码口径需单独的
  数据迁移动作。
- `default_adapters()` 仍含东财，其他能力面的退役不在本 ADR 范围内。
