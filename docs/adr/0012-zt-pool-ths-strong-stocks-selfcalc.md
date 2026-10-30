# ADR 0012：zt_pool 迁移同花顺涨停揭秘（低频纪律）+ strong_stocks 库内自算

2026-10-24，issue #120（spec #111；调研 #96，`docs/research/2026-10-20-zt-pool-alternatives.md`）。

## Context

- 东财链路本机连接级不可达（2026-10-19 事故，spec #111 背景），zt_pool 原
  承载（efinance/akshare `stock_zt_pool_*_em`）整体不可用；strong_stocks
  东财强势股池同为东财派生口径。
- THS 涨停揭秘 dataapi 本机实测（2026-10-20）：首请求即成功返回全字段
  （连板数 high_days、封单额 order_amount、炸板次数 open_num、首封时间
  first_limit_up_time、涨停原因 reason_type、板型 limit_up_type），但风控
  极敏感——秒内连发 6 次探测全部 Nginx forbidden（错误页直接打印本机
  IPv6，IP 级封禁），间隔 5 分钟重试仍封，恢复点未测到。
- 七个候选源无任何"强势股池"对应物；东财口径（60 日新高 或 近期多次涨停）
  是纯派生定义，调研结论推荐库内自算（口径透明、零外部依赖、可测试）。

## Decision

- **zt_pool → 同花顺涨停揭秘（`ThsAdapter`，唯一承载源）**：
  - 端点：涨停 `limit_up_pool`（实测 ✓）/ 炸板 `open_limit_pool`（社区实证）/
    跌停 `lower_limit_pool`（端点名与 dt 字段 ID 取自 limtupInfo 页面 JS
    资产；冒烟前猜测名 `down_limit_pool` 实测 HTTP 404——端点不存在与
    被封禁可区分）。dt 首封时间/板型字段为 first_limit_down_time/
    limit_down_type（响应已验证）；zt 池全扩展字段、zb/dt 池的
    sealed_amount/reason_type/high_days 上游不提供（炸板无封单、社区
    zb 字段集无 9001、dt 无连板口径），对应列恒 null（不伪造）。
  - **field 投影参数必需**：上游默认投影只给 code/name/latest/change_rate
    四字段（2026-10-23 冒烟实测），封单额/首封时间/板型/连板数/题材全靠
    field 参数显式请求（字段 ID 表取自页面 JS 资产；zt 字段组 2026-06-26
    社区实测互证，dt 组为 zt 组的跌停时间/形态 ID 替换版）。
  - **低频纪律**：每日每池一次——工具层先查采集台账，当日已有
    ok/empty 行即跳过上游（`ledger.already_collected`）；段抓取
    `max_retries=0` 单次尝试，不进入 fetch_chain 的同源重试；池间/分页间
    默认 20s 长退避（`THS_POOL_PAUSE_S` 可覆盖）；封禁命中本轮剩余池全部
    放弃（封禁状态下重试只会加剧封禁，等次日）。
  - **封禁探测**：HTTP 403 或响应体含 "forbidden"（Nginx 封禁页）即判定
    IP 级封禁。封禁虽属"收到响应后的拒绝"，但语义上是上游对本机的
    连接级拒服——抛 `FetchError(kind="connection")` 按
    `upstream_unreachable` 上报，**触发核心 section 中止线**（spec #111
    决策："新源故障（含 THS 风控）仍触发中止线"），retry_advice 明确
    "今日勿重试，等次日"。
  - **单池失败不拖垮他池**（akshare 段先例同构）：拿到的池照常落库，
    失败原因经 `pool_errors` 由工具层以 `partial_error` 报告并进台账 detail。
  - 字段映射（`_board_ths.py`，实测字段）：封单额 order_amount（元）→
    `sealed_amount`（万元，ADR 0006）；涨停原因 reason_type → 新列
    `reason_type`；板型 → 既有 `limit_up_type` 列（东财链路恒 None，此源
    补齐）；high_days 解析连板数（"2天2板"→2、"首板"→1，取板数末位）；
    首封时间 unix 秒 → "HHMMSS"（东八区，对齐东财落库格式）。THS 不提供
    行业/成交额/成交量/跌停价——对应列恒 None（不伪造），涨停题材归因走
    reason_type（调研结论）。
  - **schema**：sql/017 幂等迁移现库加 `sealed_amount real`、
    `reason_type text` 两列（ADD COLUMN IF NOT EXISTS；新库由 005 基线
    留待下次全量重建，现库走 017；回滚 DROP COLUMN，手册见
    `docs/limitpool-cli.md`）。
- **strong_stocks → 库内自算（`tools_strong.compute_strong_rows`）**：
  - 60 日新高：当日收盘 = 最近 60 个交易日（含当日，窗口首日锚定
    stock_daily 交易日历）收盘窗口最大值（含等值触碰；adj 取当日横截面
    口径 `select_dominant_adj`，issue #92 同规则）。
  - 近期多次涨停：最近 10 个交易日 zt_pool 涨停（pool_type='zt'）次数 ≥2。
  - 两集合并集入选；`reason` 退化为规则文本（"60日新高" /
    "多次涨停（近10日 N 次）" 及组合）；consecutive_boards 取自当日 zt 行；
    market_cap/industry 库内无真源恒 None（不伪造）。
  - **输入缺失语义**：stock_daily 当日无数据（自算输入未就绪，如收盘后批
    跑在日线入口之前）→ 按 `no_data_for_date` 正常语义跳过（status ok +
    note + empty 台账），**非故障、不触发复盘中止线**；先经 daily 入口补
    当日日线后重跑本段。
- **CLI**：`qstock-scheduler limitpool [--date] [--probe]`——zt_pool 三池
  THS 抓取 + strong_stocks 库内自算的固定入口（验收与定时挂载口）；
  `--probe` 单次页面请求探测封禁（不打 dataapi）。收盘后批 `close` 的
  两个 section 同步切换（生产路径），行为不变。
- **测试缝**：协议解析用录制/社区互证信封 fixture（`tests/fixtures/ths/`，
  信封形态社区互证、字段全集实测，冒烟实测复核）；fake 适配器注 tools_*
  层；生产路径的 `ThsAdapter` 以 monkeypatch 替换，不触网。

## Consequences

- zt_pool 与东财口径的**字段级对拍不可行**（东财本机不可达是迁移前提）：
  质量验证靠调研实测字段 + 首次真实抓取冒烟记录；腾讯分时自算对拍是
  演进方向（本期不实施，spec Out of Scope）。
- **真实抓取冒烟记录（2026-10-23，qstock_test_120，共三次、间隔分钟级低频）**：
  ① 首跑暴露两处接线错误并修复：limit=500 被上游拒绝（"limit must be
  less than or equal to 200"，上限 200）；猜测端点名 down_limit_pool 404
  （真实端点名 lower_limit_pool，取自页面 JS 资产）。② 修正后三池落库
  98 行（zt 80/dt 6/zb 12），但默认投影仅四基础字段——field 参数必需。
  ③ 带 field 参数复跑：zt 池 80/80 行带全扩展字段（封单额/首封时间/板型/
  连板数/题材），dt 池首封时间/板型 6/6，zb 池首封时间 12/12、炸板次数
  10/12；当日连板股（≥2）19 只。全程未触发风控（无 forbidden）。
- 涨停/跌停/炸板三池一日之内不可刷新（每日一次守卫）；盘中数据为时点
  快照，复盘使用窗口不受影响（19:30 后最佳，README 时间线）。
- strong_stocks 自算依赖 stock_daily 当日行（腾讯/新浪 T+0，19:30 后
  可靠）——收盘后批若早于日线入口运行，strong_stocks 当日为空台账
  （no_data_for_date），复盘就绪检查按 data_gaps 降级；调度上 limitpool
  建议挂 19:30 之后。
- THS 历史回溯深度未验证（实测 4 天可取）；跌停池端点名与 dt/zb 扩展字段
  覆盖以 2026-10-23 真实抓取为准（端点名 lower_limit_pool、dt 无
  封单额/题材/连板数、zb 无封单额/题材——对应列恒 null），详见上文冒烟记录。
