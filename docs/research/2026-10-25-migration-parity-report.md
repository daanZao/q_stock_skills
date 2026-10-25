# 核心面字段级对拍验收报告（issue #122，spec #111 总验收）

2026-10-25（周日，验收执行日；对拍基准交易日 **20261023**，最近交易日）。
执行环境：`feat/122-parity-acceptance`（基线 `spec/111-dedongcai-migration` @ 351caed）。
固定入口：`qstock-scheduler parity`（本 ticket 新增的总验收编排，复用各面
既有 parity 工具）+ 各面 `--check` 入口。全部执行只写隔离测试库
`qstock_test_122`；业务库 `qstock` 仅做只读核验（`index --verify` 与 SQL
SELECT）。原始运行 JSON 留存在 worktree `.parity-runs/`（`.scratch/` 不入库）。

## 0. 执行摘要

| 面 | 对拍基准（同一交易日 20261023） | 结果 | 结论 |
| --- | --- | --- | --- |
| boards | 申万官网收盘截面 vs 库内行 vs 库内自算 | 库内回填后 **155/155 字段一致** | 通过 |
| market_snapshot | 腾讯批量报价 vs 腾讯 rank vs 新浪直连（5577/5577/5576 行） | 价/量/额/涨跌 **0 差异**；差异集中在已知口径字段（PE/PB 口径、换手率精度） | 通过 |
| indices | 腾讯 vs 新浪指数快照（7 大指数）+ 库内覆盖核验 | 7 指数 same_day，字段差异 1/63（成交量口径微差 0.09%）；回填窗口内**无断档** | 通过 |
| lhb | 交易所官方（沪深北）vs 新浪互补 | 全日成交额零差异；净买额 24 股差异（披露颗粒度）；集合差为口径性；**深所腿 4/5 天连接级失败**（partial_error 已落台账） | 通过（附风险记录） |
| zt_pool | 跨源字段级对拍**不可行**（ADR 0012：东财不可达为迁移前提） | THS 页面探测 200 未封禁；zt 80 行扩展字段 80/80；腾讯快照涨停口径 87 只量级互证 | 通过（证据型验收） |
| 长尾 | strong_stocks 自算 / fundamentals / 清单 | 管道全通、schema 兼容；清单链 efinance 首跑成功 5925 行（含北交所 362） | 通过 |

**历史连续性**：业务库现状如实记录（迁移真空期 10-19 起全指数缺 + 已记录
的科创50 回填窗口 10-14 起）；新链在测试库回填后 indices 窗口无断档、lhb
回溯 5 日无断档、boards 回补 4 日无断档；实时面（snapshot/boards/zt_pool）
自采集日起覆盖。详见 §7。

**验收日重要现象**：东财 push2 当前**间歇性可达**（单次 curl HTTP 200；
efinance 清单首跑成功、8 分钟后复跑失败）——10-19 的持续封锁已演变为
不稳定状态。不改变本验收方法与结论（迁移决策依据 #93，验收基准按既定
口径用候选源互证）。

## 1. 验收方法

- 统一入口 `qstock-scheduler parity --date 20261023`：逐面复用已合入的
  parity 工具（`sw_board_parity` / `snapshot_parity` / `index_parity`(新增) /
  `tools_lhb.compare_lhb_sources`），zt_pool 以 THS 探测 + 连续性为证据；
  连续性 `check_history_continuity` 逐交易日查库内表行数并对照采集台账归因。
- 容忍度沿用 `daily_parity._PARITY_TOLERANCES` 口径：价格绝对差 0.011、
  涨跌幅/振幅/量比绝对差 0.05、量绝对差 100（股）、额相对差 0.005、
  换手率绝对差 0.02、PE/PB 相对差 0.05；boards 库内行比较点位/百分数
  绝对容差 1e-2、金额相对 1e-4（sw_board_parity）。
- 结构性缺项（by-design 不参与比对）：rank 榜单缺 OHLC/pre_close；新浪
  快照缺振幅/量比；新浪指数快照缺量比；hfq 价格类字段（个股日线对拍，
  后复权基准因源而异）。
- 每个网络源有限次数尝试：对拍逐源直取各 1 轮（快照主源约 70 请求/轮、
  rank 约 28 页、新浪约 56 页——#116 基线）；THS 仅页面级探测 1 次 +
  dataapi 低频纪律抓取 1 次（每日一次守卫）；东财直连仅 1 次 curl 探测
  留证。

## 2. boards（申万双级）

工具：`sw-boards --check`（三方位：申万官网 current 收盘截面 vs 库内
market_boards 行 vs 库内自算 query_sw_board_daily）。

| 项 | 结果 |
| --- | --- |
| 官网取数 | 155 板块（industry_l1 31 + industry_l2 124）全部成功，source=swsresearch |
| 库内行（回补后复对拍） | `backfill --source sw --start 20261020 --end 20261023` 落 620 行（155×4 交易日，source='swsresearch' 标注）；对拍日 20261023 **155/155 与官网一致，mismatches=0** |
| 自算口径 | 成分字典未灌（测试库）→ 自算无行，符合预期；point-in-time 自算属降级/交叉验证角色 |
| 容忍度判定 | latest_price/change_percent/change_amount 绝对 1e-2、volume 绝对 1、amount 相对 1e-4——零差异触及 |
| 历史回填 | 1999 起全历史回补路径（#118）本轮以 4 日窗口实证（155 板逐板 ok）；5/10 日涨跌"自落库日起积累"限制随回补解除 |

差异字段清单：无（迁移前东财口径与申万口径不同维度——东财行业/concept
口径退役为已定决策，不做跨口径数值等价判定，见 #113 第四态语义）。
资金流/量比/领涨股三字段按 planned_absent_fields 声明废弃，对拍与报告
均不出现（符合 #118 验收）。

## 3. market_snapshot（腾讯三层链）

工具：`snapshot --check`（逐源直取全市场，20261023）。

| 源 | 状态 | 行数 | 行级日期 |
| --- | --- | --- | --- |
| 腾讯批量报价（参考源） | ok | 5577 | 20261023（时间戳含日期，过 #78 防线） |
| 腾讯 rank 榜单 | ok | 5577 | 无（结构性） |
| 新浪直连 | ok | 5576 | 无（ticktime 无日期） |

逐字段差异计数（参考源=腾讯，compared：rank 66,924 值 / 新浪 78,064 值）：

| 字段 | rank 差异 | 新浪差异 | 判定 |
| --- | --- | --- | --- |
| latest_price / open / high / low / pre_close / change_percent / change_amount / volume / amount | **0** | **0**（latest_price 3 见下） | 核心数值面三源一致 |
| amplitude | 0 | 0 | — |
| pe_ratio | 0 | **4762** | 已知口径切换信号（腾讯疑 TTM / rank 明示 pe_ttm / 新浪 per 未验证），snapshot_parity 内置 note 声明；样本显示北交所 920xxx 差异最大（920006：腾讯 -852 vs 新浪 208.9，亏损股 PE 符号口径相反） |
| pb_ratio | 0 | 231 | 同上口径性 |
| turnover_rate | 0 | 32 | 精度性：腾讯两位小数 vs 新浪精确值（样本 1.48 vs 1.4357，差 0.045>0.02 容差） |
| market_cap / float_cap | 0 | 5 / 31 | 口径性（市值单位换算末位） |
| volume_ratio | 0 | 0 | — |
| latest_price（新浪 3 只） | — | 3 | 样本为边界价位的分单位显示差，价量额整体零差异下的孤例 |

容忍度判定：核心行情字段（价/量/额/涨跌）**全等**；估值类字段差异全部
落在调研已声明的口径切换信号内（`docs/research/2026-10-20-snapshot-alternatives.md` §7），不构成迁移阻塞。落库路径 `snapshot --date 20261023`
主源落 5577 行（测试库），台账 section=market_snapshot ok。

## 4. indices（指数快照 + 指数日线）

工具：`index_parity.compare_index_spot_sources`（本 ticket 新增，腾讯 vs
新浪逐 code 直取）+ `index --verify`（库内覆盖核验，纯读）。

指数快照对拍（20261023，same_day=True，双方行级日期均为 20261023）：

| 源 | 状态 | 指数数 |
| --- | --- | --- |
| 腾讯（参考源，唯一含量比） | ok | 7/7（含 000688 科创50） |
| 新浪 | ok | 7/7（量比结构性缺项跳过） |

逐字段差异：**63 个比对值中仅 1 个越容差**——399001 深证成指 volume
腾讯 69,075,209,500 vs 新浪 69,011,837,385（差 0.09%，>100 股绝对容差）。
判定：量级互证一致（相对差 0.0009），为深市成交量统计口径微差（含/不含
部分品种的尾差），非数据错误；价格/涨跌/成交额/振幅 6 指数零差异，
000688 科创50 全字段一致（迁移前东财口径无 000688 快照，#117 修复点）。

指数日线（`index --verify`，测试库回填 `index --start 20261012 --end 20261023`
后）：7 大指数各 10 行全部 **source=tencent**（T+0 当日行能力实证，含
000688），窗口 10-12~10-23 对照交易日历 **missing_days 全空、status 全 ok**
——科创50 回填窗口在新链下消除。业务库现状（只读核验，`index --verify
--start 20261001 --end 20261025` 对 `postgresql://localhost/qstock`）：
7 大指数止于 20261016（次晨批 baostock 源）、20261019~23 五交易日全缺
（迁移真空期，spec #111 既定接受）；000688 止于 20261013（akshare 源），
**20261014 起缺 = 已记录回填窗口**（#117 口径"自 2026-10-13 起"，业务库
精确缺口 10-14~10-16 三个交易日 + 真空期五日）。

## 5. lhb（交易所官方 + 新浪互补）

工具：`lhb --check`（交易所官方 vs 新浪互补，20261023）。

| 项 | 交易所官方 | 新浪 |
| --- | --- | --- |
| 状态 | ok（partial_error：深所腿连接级失败） | ok |
| 覆盖 | 64 股（lhb_basic） | 78 股 |

- **全日成交额 day_amount：双向可比的非累计类情形零差异**（rel 0.005 内容差为 0）——交易所一手披露与新浪转载面在成交额上完全互证。累计类情形（三日/异常期间）按既有口径规则跳过比对（交易所单日额 vs 新浪期间累计）。
- **席位净买额 net_buy_amount：24 股越容差**（如 000530 交易所 -6,676 万 vs 新浪 seat Σnet -5,678 万；000678 +7,226 vs +4,111）。判定：两源披露颗粒度不同（交易所=买五/卖五席位明细净差自算；新浪=seat JSONP 买卖各五去重 Σnet，且归类榜单可能不同），差异如实列出为对拍产出；该字段消费方（复盘龙虎榜节）以交易所官方为准，新浪为互补校验的角色不变（#114 设计）。
- **股票集合差**：exchange_only 1 只（180505）；sina_only 15 只（603/605/688 沪系与科创）——新浪页面按自归类口径收录，指标文案口径因源而异不逐条比对（既有 note）。
- **深所腿稳定性（风险记录）**：`lhb --start 20261019 --end 20261023` 五
日回溯中 10-20~10-23 四天深交所 ShowReport 连接级失败（"Remote end
closed connection without response"），partial_error 逐日落台账、
上所/北所与新浪侧正常落库（五日 lhb_basic 64/25/17/25/31 行）。10-19
当日深所正常。该现象与 10-19 前后交易所侧限流加剧一致；修复方向见 §9。
- 历史回溯能力：#114 已实测沪深至 2015、北交所至 2021-11（ADR/调研记录），
本轮 5 日回溯补证逐日幂等与断点续跑。最终全量复跑（§10）当日
  exchange_basic=25（深所腿仍失败，仅上所/北所入对拍），与 §5 主记录
  （64 股）并读：深所腿修复前的对拍基准会系统性缺深市股，新浪互补侧不受
  此影响（row_count 78 不变）。

## 6. zt_pool（THS 单源，对拍不可行的证据型验收）

依据 ADR 0012：跨源字段级对拍不可行（东财不可达是迁移前提；七个候选源
无涨停池对应物；腾讯分时自算对拍为演进方向、本期 Out of Scope）。本轮
验收证据：

1. **封禁探测**（`limitpool --probe`，页面级不打 dataapi）：HTTP 200
   未封禁，当日可服务。
2. **真实抓取**（`limitpool --date 20261023`，低频纪律单次）：三池 98 行
   落库（zt 80 / dt 6 / zb 12），台账 section=zt_pool ok：
   - zt 池扩展字段 **80/80 全量**：封单额（sealed_amount）、首封时间
     （limit_up_time）、涨停题材（reason_type）、连板数
     （consecutive_boards，max=6）、板型（limit_up_type）；
   - dt 池首封时间/板型 6/6（封单额/题材/连板上游不提供，恒 null 不伪造）；
   - zb 池首封时间 12/12（炸板无封单/题材，恒 null 不伪造）；
   - 与 ADR 0012 的 2026-10-23 冒烟记录（98 行、zt 80/80 全扩展字段）
     **完全复现**。
3. **独立源量级互证**（旁证，非字段级）：库内腾讯快照（§3 同轮落库，
   独立源）按涨停口径计 87 只（主板 ≥9.8% 68 + 创业/科创 ≥19.8% 16 +
   北交所 ≥29.8% 3）vs THS zt 池 80 只——量级方向吻合（±9%，差值来自
   ST 5% 涨停口径、新股无涨跌幅等口径细节，THS 口径为准）。
4. **库内历史连续性**：业务库 zt_pool 止于 20261019（东财口径最后采集日，
   20 天窗口覆盖至 9-14）；测试库 20261023 已覆盖。THS 历史回溯深度未验证
   为既有记录限制（ADR 0012）。

## 7. 历史连续性检查（验收标准 2）

检查工具：`check_history_continuity`（本 ticket 新增，纯读）——逐交易日
查库内表行数并对照采集台账归因（有行=覆盖；无行台账 ok/empty=上游确认
无数据，事件驱动面 lhb/zt_pool 为正常语义；无行无确认=断档，ledger 列
区分未采集 null 与采集失败 error）。indices 面复用 `verify_index_coverage`
对照交易日历逐指数列缺口。

**业务库（`qstock`，只读）现状**——迁移真空期如实呈现：

| 面 | 表 | 首行 | 末行 | 交易日数 | 结论 |
| --- | --- | --- | --- | --- | --- |
| boards | market_boards | 20261013 | 20261013 | 1 | 东财口径存量；申万口径待部署 |
| snapshot | market_snapshot | 20261013 | 20261013 | 1 | 同上 |
| zt_pool | zt_pool | 20260914 | 20261019 | 20 | 止于东财事故日 |
| lhb | lhb_basic | 20260901 | 20261019 | 28 | 同上 |
| indices | index_daily | — | 20261016 | 7 大指数止于 10-16 | **真空期 10-19 起 5 交易日全指数缺失**；000688 另缺 10-14~10-16（**已记录科创50 回填窗口**） |

除已记录的科创50 回填窗口外，业务库 10-19 起的断面是 spec #111 既定接受
的"迁移真空期不做过渡形态"——新链（本分支）尚未部署业务库，非数据断档
事故。部署动作为用户侧调度变更（close/morning/limitpool 挂 launchd），
不在本 ticket 范围。

**测试库（`qstock_test_122`，新链真实回填后）**：

| 面 | 窗口 20261019~20261023 | 覆盖 | 断档 |
| --- | --- | --- | --- |
| indices（index_daily） | 5 交易日 × 7 指数 | **10 交易日 × 7 指数全覆盖（10-12 起回填）**，verify 七指数 status 全 ok | **无（missing_days 全空）** |
| indices（market_indices） | — | 0（实时快照面，测试库未采集） | 窗口 5 日未采集（实时面自部署日起覆盖，#78 防线不可历史回补） |
| lhb | 5 交易日 | **64/25/17/25/31 行全覆盖，逐日台账 ok** | 无 |
| boards | 4 交易日（回补自 10-20 起） | 620 行（155×4，swsresearch 回补） | 窗口首日 10-19 未回补（回补窗口选择，非断档） |
| snapshot | 20261023 | 5577 行（主源，交易所官网清单驱动） | 实时面仅当日可采（#78 防线） |
| zt_pool | 20261023 | 98 行 | 同上 |

最终全量复跑（`parity --date 20261023 --window 4 --no-probe`，exit 0）
与上表一致：indices index_daily 七指数全 ok、lhb 五日 ok、boards 覆盖 4
日（gap=[10-19] 为回补窗口首日）、snapshot/zt_pool 覆盖验收日、
market_indices 未采集（实时面）。复跑中腾讯快照行数 5923（对拍参考源）
与首轮 5577 之差为**清单驱动源差异**——首轮 akshare 交易所官网清单当日
可用（5577 只），复跑时 szse.cn 间歇失败转库内 stock_list 兜底（5923 只，
即 efinance 清单口径含全部北交所）——覆盖数随清单源变动是清单驱动主源的
固有语义（`adapters/_snapshot_codes.py`），非快照源数据差异；两轮对拍
差异签名一致（diff_total 5064，同一口径字段分布）。

结论：**新链承载下回填/回溯窗口内无断档**（indices 回填窗口 = 验收标准
所列科创50 窗口的消除验证）；实时面自采集日起连续；连续性检查工具归因
正确（空库→gap、回补→covered、台账 empty→上游确认无数据的区分全部实测）。

## 8. 长尾能力（schema 兼容 + 冒烟）

| 面 | 入口 | 结果 |
| --- | --- | --- |
| strong_stocks（自算） | `limitpool --date 20261023` 自算段 | schema 兼容（strong_stocks 列集未变，BOARD_SECTION_TABLES 注册）；管道 ok：60 日新高 ∪ 近 10 日多次涨停规则执行、台账 source='selfcalc'；当日产出 0 只为种子数据范围所致（stock_daily 仅 600519+000001，600519 未触 60 日新高、zt_pool 仅 1 日不满足 ≥2 次条件），empty_kind=no_data_for_date 正常语义非故障 |
| fundamentals（透传） | `fundamentals --codes 600519,000001` | 两股均 ok：akshare 段 sections=[financial_abstract, valuation（百度股市通五指标）]，base_info 按 #113 第四态 planned_absent 显式声明；baostock 兜底未触发（首源成功）。sepa E 维度数据面不受影响（#115 验收点复核） |
| 清单 | `stock-list`（fetch）/ `stock-list --check`（互证） | fetch：efinance 首跑成功 5925 行 upsert（含北交所 362），schema 兼容（STOCK_LIST_FIELDS 两列映射、幂等）；check：第二轮 efinance 失败（非 JSON 响应，间歇性）、akshare 交易所官网腿失败（szse.cn 连接失败，#108 同类现象）、baostock 兜底 5565 行（无北交所，#100 记录口径）；fallback 与兜底机制工作正常 |

长尾 schema 兼容的结构性证据：本 ticket 未改任何 DDL；新列仅 #120 已合入
的 sql/017（zt_pool sealed_amount/reason_type，THS 承载用）；615 项既有
测试基线全绿 + 新增 32 项测试全绿（§10）。

## 9. 差异字段清单与容忍度总表（验收标准 1 汇总）

| 面 | 越容差字段 | 容忍度 | 判定 |
| --- | --- | --- | --- |
| boards | 无 | 点位 1e-2 / 额 1e-4 | 155/155 一致 |
| snapshot | pe_ratio(4762)/pb_ratio(231)/turnover_rate(32)/market_cap(5)/float_cap(31)/latest_price(3)（均相对新浪；vs rank 为 0） | 见 §1 | 已知口径切换信号 + 精度性，核心字段零差异 |
| indices | volume(1/63) | 量绝对 100 | 口径微差 0.09%，量级互证 |
| lhb | net_buy_amount(24 股) | 净买 rel 0.005 | 披露颗粒度差异，产出非失败；day_amount 零差异 |
| zt_pool | 对拍不可行（ADR 0012） | — | 探测 + 字段覆盖 + 量级旁证 |

**总结论：五个核心面对拍全部达到迁移验收标准**——候选源互证下核心数值
面一致，差异字段全部可解释为已声明的口径性/精度性/颗粒度差异；历史连续
性在新链承载下无断档（科创50 回填窗口已消除）；长尾 schema 兼容 + 冒烟
通过。

## 9+. 遗留风险与跟进建议（不阻塞验收）

1. **深交所 ShowReport 连接级失败（4/5 天）**：交易所侧限流/封禁风险在
   10-19 后加剧，lhb 主源深所腿当前不可用（partial_error 降级正常）。
   建议：① 观测修复时点（历史回溯断点续跑可补）；② 若持续，评估深所腿
   降级策略（新浪互补顶上已具备）。本 ticket 不动 lhb 链。
2. **东财间歇性可达**：封禁状态不稳定（同日一成一败），efinance 清理
   ticket（#111 user story 22）按既定推进，不回退迁移；清单链在 efinance
   移除前仍可能间歇抖动（baostock 兜底已验证）。
3. **THS 封禁探测仅页面级**：dataapi 真实可用性以每日低频抓取台账为准；
   建议把 `limitpool --probe` 纳入每日巡检（README 时间线口径）。
4. **快照换手率精度**：腾讯两位小数 vs 新浪精确值，32/5577 越 0.02 容差；
   消费方（复盘宽度数据）使用腾讯口径一致即可，如需高精度可自算
   （volume/流通股本）。不阻塞。
5. **zabb 池扩展字段结构性恒 null**（dt 无封单/题材/连板、zb 无封单/题材）：
   上游 by-design 不提供，列为已知限制（ADR 0012）。
6. **`sw-boards --date <历史交易日>` 被实时防线拒收**（落 error 台账
   guard_reject）：板块历史窗口回补走 `backfill --source sw`（本报告
   实证），语义正确；手册已写明。

## 10. 验收执行记录（可复现）

| 步骤 | 命令（PG_DSN=postgresql://localhost/qstock_test_122） | 结果 |
| --- | --- | --- |
| 对拍主跑 | `qstock-scheduler parity --date 20261023` | exit 0；五面 parity + 探测 + 连续性（空库基线） |
| 指数回填 | `qstock-scheduler index --start 20261012 --end 20261023` | 7 指数×10 行，全 tencent |
| 板块回补 | `qstock-scheduler backfill --source sw --start 20261020 --end 20261023` | 155 板×4 日 620 行 |
| 快照落库 | `qstock-scheduler snapshot --date 20261023` | 5577 行 tencent |
| 日线种子 | `qstock-scheduler daily --codes 600519 --start 20260701 --end 20261023` 等 | 81 行（≤100 纪律） |
| 涨停池 | `qstock-scheduler limitpool --date 20261023` | zt_pool 98 行 + strong_stocks 自算段 |
| 龙虎榜回溯 | `qstock-scheduler lhb --start 20261019 --end 20261023` | 5 日全 ok（深所腿 4 日 partial_error） |
| 清单 | `qstock-scheduler stock-list` / `--check` | 5925 行 / 逐源互证记录 |
| 基本面冒烟 | `qstock-scheduler fundamentals --codes 600519,000001` | ok（双 section） |
| 日线对拍（支撑面） | `qstock-scheduler daily --codes 600519 --date 20261023 --check` | 三源零差异 |
| boards 复对拍 | `qstock-scheduler parity --faces boards --date 20261023 --no-probe` | 155/155 一致 |
| 最终连续性 | `qstock-scheduler parity --date 20261023 --window 4 --no-probe` | exit 0；§7 测试库表（快照三面重取约 10.7 分钟，SZSE 间歇限流致慢） |
| 业务库核验 | `PG_DSN=.../qstock qstock-scheduler index --verify --start 20261001 --end 20261025`（纯读） | §7 业务库表 |
| 东财连通留证 | `curl --max-time 10 https://push2.eastmoney.com/api/qt/clist/get?...` | HTTP 200（间歇性） |
| 测试 | `pytest tests/ -q`（QSTOCK_TEST_PG_DSN=qstock_test_122，worktree 最终全量） | **647 passed（615 基线 + 32 新增，0 失败）** |

部署提示：业务库切换 = 用户侧把 close/morning/limitpool/stock-list 挂
launchd（`docs/scheduler-launchd.md`），并对业务库执行一次性回填
（`index --codes 000688 --start 20261014`、`lhb --start 20261020 --end <部署日前一交易日>`、`backfill --source sw`、`stock-list`）。
