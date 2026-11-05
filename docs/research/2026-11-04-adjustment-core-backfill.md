# 复权核心与存量回填一手来源复核（#158）

日期：2026-11-04。范围：独立核对 #158 当前正文的公式、拟议 DDL/状态语义、窗口基线 B0、D-1、修订/撤销、停牌、新股、退市、覆盖、迁移与分层验收。不修改代码、数据库、issue 或既有文档。

**证据边界**：2026-10-31 的八份报告均已作废；本次只用它们定位原始缓存，没有继承或引用其分析结论。规范依据是 [#158 当前正文](https://github.com/daanZao/q_stock_skills/issues/158)（本轮读取 `updatedAt=2026-11-04T02:43:35Z`）；接口依据是 baostock 官方文档、已安装客户端源码、缓存原始响应及本轮三个定点实际调用。

## 0. 结论先行

1. **核心公式成立**：`f=B_prev/B_cur`、`F=B/B0`、`hfq=raw×F`、`F_latest=B_latest/B0`、`qfq=hfq/F_latest=raw×B/B_latest` 相互一致；事件日恒等式为 `F(D)×f(D)=F(D-1)`，不要求 `f<=1` 或 F 单调。
2. **B0/D-1 公式也成立**：#158 的 `realclose=close(D-1)×(1+pctChg/100)` 应理解为“假设无公司行为时按当日涨幅得到的理论收盘”，不是上游 `preclose`。由此 `f1=close(D)/realclose=preclose(D)/close(D-1)`，所以 `B0=B1×f1`。688300 与 600381 均数值通过。
3. **B 字段已决策**：原始行证明线上 `adjustFacto` / 单票 `adjustFactor` 可明显偏离 `backAdjustFactor`，官方文档还把它称为“本次复权因子”。累计 B 只采用实际表现为累计序列的 **`backAdjustFactor`**；`adjustFacto(r)` 仅可留原始快照审计，不得进入 B、f、F 或任何派生。
4. **DDL 能表达同键修订，不能仅靠 upsert 表达撤销**。成功重查某事件日期后，必须事务内“upsert 当前行 + 删除该日陈旧行”，并从旧/新事件的最早受影响日期重建；请求失败不得删除或解释为空。
5. **迁移原则可行但有条件**：逐交易日取得全市场事件，窗口内所有股票都从权威 raw 源回补，窗口首事件反推 B0，legacy 不跨边界混排。无事件票也只使用 raw，不得复用旧 qfq；旧 qfq 仅保留在 legacy，不参与新价格体系。
6. **验收方向正确**：全量结构/事件/基线/覆盖校验，普通行固定种子分层抽样，每股每表规范哈希验证幂等/续跑/修订/撤销；不做全市场逐值新旧对拍，也不为验收重抓全市场。

## 1. 一手来源与实测条件

### 1.1 官方与当前源码

- [baostock 官方 Python API](https://www.baostock.com/mainContent?file=pythonAPI.md)：`adjustflag=3` 为不复权；raw `volume` 单位为股、`amount` 单位为元；`pctChg=(close-preclose)/preclose×100%`；除权日 `preclose` 是交易所公布参考价；首发日 `preclose=首发价格`；`tradestatus=0` 表示停牌，日线包含停牌证券。
- [baostock 官方复权因子说明](https://www.baostock.com/mainContent?file=factorInfo.md)：`dividOperateDate` 为除权除息日，返回 `foreAdjustFactor/backAdjustFactor/adjustFactor`，并把 `adjustFactor` 描述为“本次复权因子”。
- [baostock 官方本地复权示例](https://www.baostock.com/mainContent?file=localdatafactorInfo.md)：原始日 K 与因子分开保存，再按事件日匹配因子生成复权行情，支持“事实与派生分离”的方向。
- 已安装 `baostock==0.9.3`：单票接口以除权日起止日过滤，见 `.venv/lib/python3.12/site-packages/baostock/evaluation/season_index.py:107-146`；逐日接口只接一个日期且单页上限写死 20000，见同文件 `:185-240`。逐日接口不在上述公开 API 目录中，因此其语义还必须以实际响应核对。
- 客户端接收循环没有读超时，见 `.venv/lib/python3.12/site-packages/baostock/util/socketutil.py:55-84`。本轮调用使用进程内 90 秒 alarm 兜底；这不等于已验证生产 120 秒硬超时实现。
- 当前适配器尚无因子采集，日线 `_FIELDS` 不含 `preclose`，见 `src/qstock_mcp/adapters/baostock_adapter.py:22-23`；北交所显式拒绝，见 `:151-158`；全市场 raw 映射将 amount 元转万元并保留 tradestatus，见 `:113-145`。
- 当前库仍是以 `(stock_code,trade_date,adj)` 为键的 `stock_daily`，见 `src/qstock_mcp/sql/001_stock_daily.sql:3-22`；upsert 与按 adj 读取见 `src/qstock_mcp/repository.py:32-49`。四张新表及 `adj-backfill` 目前未在源码/SQL 中实现，本文审查的是 #158 设计而非既有行为。

### 1.2 缓存原始响应

以下为实际接口落盘行，不采用对应旧报告的解释：

- 2026-10-31，`query_adjust_factor`，串行、相邻请求至少 1.5 秒；688300 窗口 2021-01-01..2026-10-31，8 行；raw 日线同窗口、`adjustflag=3`，1410 行。参数、字段、成败和耗时见 `.scratch/baostock_adj_143/ledger.jsonl:1-2`。
- 同日、同条件，600519 单票因子 11 行，见 `.scratch/baostock_adj_143/ledger.jsonl:3`；原始值见 `.scratch/baostock_adj_143/raw/p1_adjust_600519.csv:1-12`。
- 2026-10-31，`query_daily_adjust_factor(2026-10-16)` 成功返回 48 行，实际第五列拼为 `adjustFacto`，见 `.scratch/baostock_adj_143/ledger.jsonl:10` 与 `.scratch/baostock_adj_143/raw/p3_dailyadjust_20261016.csv:1`。
- 另三次逐日因子调用为 2024-06-04、2025-11-25、2024-05-14，分别 63/12/21 行；两次 `bj.*` 单票调用均返回 `10004011 股票代码未标识sh或sz`。原始台账见 `.scratch/baostock_bse_154/ledger.jsonl:1-6`。这些样本只证明当次响应，不外推全历史完备率或长期容量。
- 退市代码 600200 的单票因子请求在 2026-10-31 成功返回四个历史事件，见 `.scratch/baostock_adj_143/ledger.jsonl:12` 与 `.scratch/baostock_adj_143/raw/p3_adjust_600200.csv:1-5`；这只证明因子历史保留，不证明其 raw 全窗口仍可取。

### 1.3 2026-11-04 本轮定点实测

环境同仓库 `.venv`，`baostock==0.9.3`；一个登录会话内串行三次数据调用，相邻至少 1.5 秒，无并发、无重试、无写库、无落盘新文件，每次 90 秒 alarm；三次均 `error_code=0`：

| 接口与样本 | 条件 | 直接结果 |
| --- | --- | --- |
| `query_adjust_factor(sh.600381)` | 2010-01-01..2016-12-31 | 2012-05-15 的 back/adjustFactor 均 7.192060；2014-06-27 均降至 0.893104 |
| `query_adjust_factor(sh.600094)` | 2008-01-01..2013-12-31 | 仅 2011-10-11 一行，back/adjustFactor 均 1.983970；无前序 B，不能独立算出 #158 所列单次 f |
| `query_history_k_data_plus(sh.600381)` | 2014-06-20..07-04，`adjustflag=3`，含 close/preclose/pctChg/tradestatus | 06-26 close=2.08；06-27 close=preclose=16.75、pctChg=0、volume=0、tradestatus=0；窗口内所有行均停牌 |

同日晚些时候为补齐 600094 Golden 另执行两次请求：同一登录会话串行、间隔 1.5 秒、每次 90 秒 alarm、无重试、无写库，均 `error_code=0`；原始响应裁剪见 `tests/fixtures/adjustment/600094_20111011.json`：

- `query_adjust_factor(sh.600094)` 改为 1990-01-01..2013-12-31，返回 9 行；2007-03-19 的 B=2.646807，2011-10-11 的 B=1.983970。
- `query_history_k_data_plus(sh.600094)` 同窗口 `adjustflag=3`，返回 3994 行；2011-10-10 close=4.37、tradestatus=0，2011-10-11 close=7.67、preclose=5.83、pctChg=31.560900、tradestatus=1。

单次耗时应只代表上述样本；上游可持续 TPS、并发上限和全历史完备率均**待验证**，实现仍应低频串行并加外部硬超时。

## 2. B 字段：反例与最终决策

### 2.1 已证实的反例

官方字段表的存在不等于第五列永远是累计值：

- 600519，2023-06-30：`backAdjustFactor=6.889798`，但 `adjustFactor=0.988591`；下一行 2023-07-03 的 back 又为 6.889798。见 `.scratch/baostock_adj_143/raw/p1_adjust_600519.csv:5-7`。
- 603786，2024-06-04 逐日响应：`backAdjustFactor=1.039163`，`adjustFacto=1.008751`，见 `.scratch/baostock_bse_154/raw/d_20240604.csv:64`。
- 603336，2025-11-25 逐日响应：`backAdjustFactor=4.298803`，`adjustFacto=1.000000`，见 `.scratch/baostock_bse_154/raw/d_20251125.csv:13`。

若把这些第五列写成 B，会制造假的因子重置，并进一步污染 B0、F、hfq 与全序列 qfq。688300 的第五列恰好等于 back（`.scratch/baostock_adj_143/raw/p1_adjust_688300.csv:2-9`），只能证明该样本相等，不能推广。

**最终决策**：B 只取 `backAdjustFactor`；第五列可留原始快照供审计，但不得进入任何业务因子或派生。至少要有 `B>0且有限`、同票事件顺序和双路径对账；双字段不一致时仍取 back，不触发二选一或自动阻断。

### 2.2 返回行不必然都是价格阶跃

600519 原始序列在 2023-06-30 与 2023-07-03 的 back 完全相等，见同一缓存 `:5-6`。因此“接口返回一行”与“B 发生阶跃”不能混为一谈。派生所需事件应按 B 的实际变化处理；若业务还要保留无价格变化的登记信息，应与价格事件分层，而不是写入 `f=1` 后声称发生价格调整。逐日接口是否也稳定返回这类重复 B 行，**待验证**。

## 3. 公式独立推导与定值

### 3.1 基本关系

令 B(t) 在事件日取当日累计值，在事件之间沿用最近值；D-1 是市场日历上前一交易日，不是“最后一根非停牌行”或“前一个事件日”。则：

```text
f(D)       = B(D-1)/B(D) = B_prev/B_cur
F(t)       = B(t)/B0
hfq(t)     = raw(t)×F(t)
F_latest   = B_latest/B0
qfq(t)     = hfq(t)/F_latest = raw(t)×B(t)/B_latest
```

事件日：

```text
F(D)×f(D) = (B_cur/B0)×(B_prev/B_cur)
          = B_prev/B0 = F(D-1)
```

无事件日 B 不变，所以 F 不变。最新事件之后 `B=B_latest`，故 qfq=raw。新增事件只需改变 `[ex_date,today]` 的 hfq，但新的 F_latest 会使**全历史 qfq 重新锚定**；因此 qfq 不应物化后跨事件复用旧绝对值。

B、F、f 只要求正且有限。600381 直接证明 f>1、F 下降合法，不能加 `f<=1` 或“F 单调不减”约束。

### 3.2 688300：正常送转

原始输入：

- 2026-06-10 `B_prev=2.965589`，2026-10-16 `B_cur=3.855379`，见 `.scratch/baostock_adj_143/raw/p1_adjust_688300.csv:8-9`。
- D-1=2026-10-15 raw close=136.01；D=2026-10-16 raw close=102.70、preclose=104.62、pctChg=-1.835200、tradestatus=1，见 `.scratch/baostock_adj_143/raw/p1_daily_688300.csv:1400-1401`。
- 逐日事件表同票同日的 back 与第五列均为 3.855379，见 `.scratch/baostock_adj_143/raw/p3_dailyadjust_20261016.csv:44`。

计算：

```text
f_authoritative = 2.965589/3.855379
                = 0.7692081634516347...
单事件归一 B0=1 时：B1=1/f=1.300038204889484...
F(D-1)=1；F(D)=1.300038204889484...
hfq(D-1)=136.01
hfq(D)=102.70×1.300038204889484=133.5139236421500...
F_latest=F(D)
qfq(D-1)=136.01/F_latest=104.6200023110568...（约 104.62）
qfq(D)=133.5139236421500.../F_latest=102.70
```

104.62 是事件日上游 preclose/除权参考价，102.70 才是事件日 raw close；不得互换，也不得以理论精确 `1/1.3` 替代实际 B 比值。

### 3.3 600381：f>1、F 下降且停牌日事件发生

2026-11-04 本轮直接调用得到：

```text
B_prev=7.192060（2012-05-15）
B_cur =0.893104（2014-06-27）
f     =7.192060/0.893104=8.052880739533134... > 1
D-1=2014-06-26，raw close=2.08，tradestatus=0
D  =2014-06-27，raw close=16.75，preclose=16.75，pctChg=0，tradestatus=0
```

用 B0≈7.192063461538462（下一节由 pctChg 反推）：

```text
F(D)=0.893104/B0=0.124179104477612... < F(D-1)=1
hfq(D-1)=2.08
hfq(D)=16.75×F(D)=2.08
```

所以事件登记不得看 tradestatus；停牌行的 OHLC 是上游合成占位，不是当日成交价格，也不参与收益、波动、均线等交易指标。官方只笼统描述停牌 OHLC 沿用前收，而本样本显示公司行为生效日合成价会跳变，故更不能用“停牌且 pctChg=0”推断无事件。

### 3.4 600094：长停牌后缩股恢复上市

第二次受控补证取得完整邻域与前序 B：

```text
B_prev=2.646807（2007-03-19）
B_cur =1.983970（2011-10-11）
f     =2.646807/1.983970=1.334096281697808...
D-1=2011-10-10，raw close=4.37、tradestatus=0
D  =2011-10-11，raw close=7.67、preclose=5.83、pctChg=31.560900、tradestatus=1
```

价格路径 `preclose(D)/close(D-1)=5.83/4.37=1.334096109839817...`，与因子路径在既定 0.1% 容差内一致；`B_cur<B_prev`，因此 F 下降。该样本同时覆盖长期停牌合成行与恢复上市真实成交行。

## 4. B0 与 D-1：公式已证实，缺口处理必须严格

### 4.1 为什么 `B0=B1×f1` 正确

官方 pctChg 定义给出：

```text
pct = (close(D)-preclose(D))/preclose(D)×100
```

#158 定义的 `realclose` 是“无公司行为时按 pctChg 得到的理论收盘”：

```text
realclose(D) = close(D-1)×(1+pct/100)
             = close(D-1)×close(D)/preclose(D)
f1           = close(D)/realclose(D)
             = preclose(D)/close(D-1)
```

这正是首个事件的价格事件因子，因此 `B0=B1×f1`。不要把 realclose 改名为或误解为上游 preclose。

688300 定值：

```text
realclose =136.01×(1-1.8352/100)=133.51394448
f1        =102.70/133.51394448=0.7692080433994231...
B0        =3.855379×f1=2.965588537153224...
```

与前一事件实际 B_prev=2.965589 的差来自六位因子/价格精度，量级合理。600381 则 `realclose=2.08`、`f1=16.75/2.08=8.052884615384615...`、`B0=7.192063461538462...`，同样与前序 B=7.192060 对齐。

### 4.2 D-1 和失败语义

- D-1 必须按交易日历取确切前一交易日 raw close；窗口首日即事件时，额外取窗口外最后一个交易日作为**计算输入**，不能拿窗口内首行自行当 D-1，也不能跨越 raw 缺口取更早行。
- 停牌期间 D-1 仍是前一市场交易日，其 close 可以是合成占位；600381 证明这种输入能恢复事件连续性。
- 首次事件缺 pctChg、raw close(D-1) 或 close(D)，或计算出的 B0 非正/非有限，均不得猜 `B0=1` 或填其他 provisional 值；应置 `baseline_factor=NULL`、`factor_latest=NULL`、`rebuild_pending=true`、coverage=partial。
- 只有窗口内确无事件且事件采集状态完整的股票，才可令 B0=1、F≡1。
- 新股上市首日官方 preclose 是首发价，且没有 D-1，不能套用事件公式。新股无事件时也须初始化 B0=1、F_latest=1 和状态，不能长期 unknown；新股首个后续事件有真实 D-1 时再按上式反推。上市首日即发生特殊公司行为的情形未实测，应进入异常而非硬算。

## 5. DDL 与状态语义审查

#158 四表的方向是：raw 为唯一价格事实、事件表存 B、hfq 物化、state 存 B0/F_latest/anchor，qfq 读时标量派生。需钉死以下语义：

1. **两个 factor_cum 不是同一量**：`stock_adjust_factor.factor_cum=B`；`stock_daily_hfq.factor_cum=F=B/B0`。`stock_factor_state.factor_latest=F_latest=B_latest/B0`，不是 B_latest。Golden 和 SQL 命名/注释必须防止混用。
2. **同键 upsert 是修订而非历史追加**：当前仓储已有同键更新先例（`src/qstock_mcp/repository.py:32-43`）。raw 修订重算对应 hfq；事件 B 修订从 ex_date 起重建；若修订的是窗口首事件，还要重算 B0。
3. **撤销必须删除陈旧键**：主键 `(stock_code,ex_date)` 下，事件改期会留下旧日期，单纯 upsert 无法撤销。成功获得某日全量权威结果后，应在同一事务内替换该日事件集，收集旧∪新受影响股票，从最早旧/新 ex_date 重建。失败时保留旧事实并标未知/待修复，绝不能当“成功为空”。
4. **F_latest 与 hfq 要一致提交**：事件撤销/修订既可能改变后续 hfq，也可能改变全历史 qfq 的分母。state、事件和 hfq 应处于可恢复的一致重建流程，避免查询读到新分母配旧 hfq。
5. **rebuild_pending 只是开关，不是原因**：还需可查询的异常/采集台账记录日期、原因、来源和影响范围。事件表本身无法区分“成功无事件”与“从未成功查询”。
6. **无效 raw 要可审计隔离**：拟议 DDL 没有 close 正价约束，正文规定 NULL/≤0 隔离，因此验收要同时核对有效 raw↔hfq 对齐和隔离行台账；不能让无效行悄悄消失。
7. **latest_trade_date 是数据锚，不是对退市票强制推进到今天**：活跃无事件股票每日推进；退市股票保留最后有效 raw 日期和最后 B。complete 应按证券生命周期内预期日期判断。
8. **state 必须能表达基线未知**：`baseline_factor` 与 `factor_latest` 可空，但必须同时为空或同时非空；非 pending 不得为空，非空值必须正且有限。基线未知时写 NULL/NULL+pending+partial；pending 也可保留上一组有效值，但不得判 complete。CHECK 必须显式处理 NULL，避免 PostgreSQL 将 UNKNOWN 当通过。

现有 `collection_log` 已有 ok/empty/error 与“无行=未采集”的基本语义，见 `src/qstock_mcp/sql/013_collection_log.sql:1-23`，可承载逐日因子采集状态，但必须在迁移清单和查询 coverage 时真正使用；仅建四表不足以证明事件完整。

## 6. 生命周期、覆盖与读取边界

### 6.1 覆盖判定

- `complete`：预期上市日期内 raw 连续且有效；每个交易日因子请求均有成功记录（成功空也算）；事件集、B0、state/hfq 对齐；无未解决异常。正式 pctChg 降级通过精度校验不因“派生”自动 partial。
- `partial`：已知 raw/事件缺口、基线不可信、rebuild_pending、legacy、T+0 未确认、北交所替代路径或其他可返回但不完整的数据。
- `unknown`：尚无状态或没有足够逐日成功记录判断。新上市无事件不应在成功初始化后继续 unknown。
- 当前 `tools_coverage` 默认只数 legacy qfq 日期，见 `src/qstock_mcp/tools_coverage.py:39-59`；新模型不能直接沿用为 raw/事件 complete 证明。

### 6.2 新股、退市与证券范围

- 新股：无 D-1 的首日不造 f；无事件也建 state。新股与其他股票一样按既定范围回补权威 raw，不得复用 legacy qfq。
- 退市：600200 缓存证明因子历史可查；raw 可取深度待定点验证。迁移 Universe 不能只取“当前上市清单”。当前适配器会过滤 `status!=1`，见 `src/qstock_mcp/adapters/baostock_adapter.py:65-80`，而全量回溯当前从 stock_list 取代码，见 `src/qstock_mcp/tools_init.py:193-212`；新迁移应合并 legacy 股票集合与事件股票集合，并按 ipoDate/outDate 界定预期覆盖。
- 证券过滤：逐日因子表会包含当前 A 股日线范围之外的代码，需按证券范围过滤，不能把所有返回行都当个股事件。北交所默认排除；baostock 当前代码显式拒绝，缓存 `bj.*` 调用也直接报错。开启北交所只能标 partial，不能把沪深因子覆盖外推到北交所。

### 6.3 legacy 边界

2023-01-01 前保留 legacy_qfq、只读、coverage=partial；跨边界 qfq 不拼接，返回明确范围错误；窗口外 hfq/raw 不可用时不回落 legacy qfq。窗口外 D-1 raw 只是首个事件基线的 staging 输入，不因此把 legacy 价格改造成新口径，也不从 legacy qfq 反推 raw/hfq。

## 7. 存量迁移复核

推荐顺序与必须满足的条件：

1. 建四张影子表、可恢复迁移工件、逐日采集状态和异常台账；固定窗口起点与迁移锚点。
2. 每个历史交易日调用一次逐日因子接口，持久化“成功非空/成功为空/失败”；失败可续跑，不得写成空事件。
3. 以 **backAdjustFactor=B** 建事件集；过滤证券范围，识别相同 B 的非阶跃行。导出事件票与无事件票，窗口内所有股票都逐只重抓 `adjustflag=3` raw，amount 元转万元。
4. 无事件票也只使用权威 raw，不得复用旧 qfq。raw 缺失、非正价、来源/单位异常须隔离或进入明确修复清单，不能判 complete；旧 qfq 仅留在 legacy，不参与新价格体系。
5. 对窗口首事件用权威 raw+pctChg 反推 B0；需要时额外取窗口外 D-1。随后算 B、F、hfq、F_latest；无事件且覆盖完整者 B0=1。
6. 全量结构/事件/覆盖验收和普通行分层抽样复用本地产物；旧 qfq 差异只作归因诊断，不做全量逐值对拍。
7. 验收通过后切读，legacy 改名只读。切换后继续统一 raw+因子管线，不双写 legacy；回滚若已有新写入，必须先生成并验证缺失 legacy 修复工件。

**待实测的迁移假设**：2023 以来逐日事件接口的全窗口成功率/修订情况、全量 raw 回补的成功率与耗时、退市 raw 覆盖、首次事件 D-1 可得率。现有少量缓存不能代替这些盘点。

## 8. 分层验收与哈希幂等

### 8.1 全量必须检查，不能抽样代替

- 结构：主键重复、正且有限 B/F、有效 raw 与 hfq/state 对齐、每股状态存在、行数/股票数/日期覆盖、逐日因子成功状态完整。
- 每一事件：D-1/D/D+1 的 `F(D)×f(D)=F(D-1)`、`hfq=raw×F`、`qfq×F_latest=hfq`；首事件额外核对 B0。允许精度容差，不断言 F 单调，不按 tradestatus 排除。
- 覆盖与异常：已知缺口/rebuild_pending 不得 complete；权威 B 与 pctChg 路径相对误差 ≤0.001 才对账通过，0.1% 不是事件过滤阈值。
- Golden：至少覆盖 688300、600381、600094、600519、301569，以及停牌、退市、北交所样本。600519 还应覆盖第五列≠back 和相同 B 行；600094 的前序 B/raw 邻域需先补齐直接证据。

### 8.2 普通行情抽样

固定种子抽 100 只有事件股票 +100 只无事件股票，每股 5 个普通交易日；Golden 不占名额。分层至少覆盖交易所、价格区间、年份、来源；事件票还应覆盖首次/非首次事件和正常/反向因子。普通行只核对派生公式、单位和消费结果，不与旧 qfq 全量逐值比较。任一失败扩大到同类别全检并归因，不能换样本放行。

### 8.3 哈希、续跑、修订与撤销

- 每股每表按业务键排序，对规范化后的业务列计算行数+摘要哈希；raw、事件、hfq、state 分别比较。
- **排除 `created_at/updated_at/rebuilt_at/last_rebuilt_at` 等运行时间戳**，否则幂等重跑或续跑对比会因写入时间不同而假失败；数值序列化规则须固定。
- 同一输入重跑哈希不变；中断续跑与 uninterrupted 参考运行哈希一致。
- 本地构造权威 raw 修订、B 修订、事件改期和事件撤销：旧键不得残留，重建范围正确，F_latest/qfq 重锚正确，重复执行修复后哈希稳定。
- amount 元→万元做定值测试；全量聚合/哈希只读迁移产物，不重新请求全市场，也不导出全部明细。首次执行记录耗时，再据实测设性能预算。
- 3–5 个连续交易日上游 smoke 只验证真实请求、单位、成功空/失败、超时和续跑；`daily --check` 不能单独证明派生正确。

## 9. 已证实与待验证清单

### 已证实

- 核心 f/F/hfq/qfq 公式、事件不变量、qfq 最新段等于 raw；f>1 和 F 下降合法。
- #158 的 B0 公式在正确理解 realclose 后成立；688300、600381 数值均与前序累计因子对齐。
- 688300 的 104.62 是事件日 preclose，102.70 是 raw close；归一 hfq=136.01/133.513923642…，qfq 约 104.62/102.70。
- 600381 在 tradestatus=0 时发生事件，B 从 7.192060 降至 0.893104；停牌不能过滤事件。
- 600094 的前序 B 与 D-1/D raw 邻域已补齐，因子路径与价格路径均给出 f≈1.334096，恢复上市边界已证实。
- **第五列不是可靠的累计 B**；600519、603786、603336 原始反例存在，最终决策为 B 只取 `backAdjustFactor`，第五列仅留原始快照审计。
- 单票接口按除权日窗口过滤；逐日接口为单日全市场事件响应且第五列实际拼作 `adjustFacto`；客户端自身无读超时。
- 退市代码因子历史可保留；首发日 preclose 的官方语义是首发价；raw 股/元单位及停牌语义有官方文档。
- 同键 upsert 本身不足以处理事件撤销；事件完整性与 raw 连续性必须进入 coverage，而非只看行数。

### 待验证/实施前必须补齐

- 以 `backAdjustFactor` 为 B 跑全量 Golden 与双路径对账，覆盖双字段不一致和相同 B 的非阶跃行。
- 2023 以来逐日事件接口的成功率、空日、重复 B、修订/撤销实例和全历史完备率；先 3–5 日小样本，再按迁移产物全检。
- 新股上市首日特殊事件、退市股票 raw 全窗口，以及事件票和无事件票全量 raw 回补的缺失/非正/单位/来源盘点。
- 成功重查后的事件集替换、异常台账与 state/hfq 一致重建需要用本地修订/撤销用例验收；上游真实修订频率未知。
- 北交所若显式开启，其替代源覆盖与精度需独立验证；沪深样本不得外推。
- baostock 长期可承载 TPS、并发上限与全量耗时未知；本轮五次串行成功不构成容量结论。

## 9. #161 连续交易日 smoke（2026-11-05）

实现后在 macOS 主进程串行调用 `query_daily_history_k_AStock(date)` 与 `query_daily_adjust_factor(date)`，样本为 2026-10-30、2026-11-02、2026-11-03 三个连续交易日；每次调用外部硬超时 120s，相邻请求间隔 1.5s，无并发、无写库、无历史重抓。六次请求均成功：raw 分别为 5223/5223/5224 行，close 均非空且为正，股票代码当日唯一；因子事件分别为 11/3/5 行，`(stock_code, ex_date)` 唯一。原始 amount 经适配层转换为万元，preclose 保留。该结果只代表本轮三日样本，不构成长期可用性、TPS 或全历史完备率结论。

## 10. #163 回填 A 开发验收（2026-11-05）

实现 `qstock-scheduler adj-backfill` 后，在隔离库 `qstock_test`、真实 baostock、macOS 主进程执行窗口 2026-11-02–2026-11-04（3 个连续交易日）的端到端小样本：串行、相邻请求 1.5s、单请求 120s 外部硬超时、无并发、`--limit 20`（本样本事件票 14 只，未触发截断）。命令产物见 `files/adj_backfill_smoke_20261102_20261104/`，原始 stdout/stderr 在同目录旁。

- 3 次 `query_daily_adjust_factor` 均成功：ok=3、empty=0、error=0，共 14 行事件 / 14 只事件票；事件表只写这 14 行，无占位因子。
- 14 次单票 raw 请求均成功，窗口内 raw 41 行；3 只票首个事件在窗口首日（2026-11-02），额外请求 2026-10-30 作为 D-1 基线输入，D-1 行保留在 raw 但不进入 hfq。
- 首跑耗时 25.9s（17 次真实请求，相邻 1.5s），状态 `partial_error`：14 票中 13 票建立 B0 并重建，1 票进入待修复台账。
- 待修复样本 `001381`：2026-11-03 上市首日即出现 `backAdjustFactor=1.000000` 事件，raw 从上市首日开始、没有 D-1，按 §4.2 不套用首事件公式，state 为 `baseline_factor=NULL、factor_latest=NULL、rebuild_pending=true`，`coverage=partial`，未猜 B0；这不是回填 bug。
- 原样重跑同一窗口：3 个因子日与 14 只票全部命中本回填 `collection_log` 断点，上游请求 0 次（`summary.upstream_requests.total=0`），耗时约 0.04s（14 票本地重建，跳过项不占节流等待）。四张业务表（raw/事件/hfq/state）按业务列排序的 MD5 与首跑逐表一致，见 `files/adj_backfill_final_before_hash.txt` / `after_hash.txt`。
- 评审后（同窗口）复测一致：首跑 25.9s、重跑 0.039s/0 请求、业务哈希一致；本样本未出现同 B 非阶跃行，因此事件数仍为 14。同 B 过滤、morning 降级台账不冒充权威日、历史 `--end` 拒绝（`window_behind_data`）均有单测覆盖。
- 该样本只证明当次真实采集、IPO 无 D-1 边界与断点续跑，不构成全窗口成功率、事件完备率、长期 TPS 或全量耗时结论；正式回填后的结构/事件/B0/覆盖全检按 `docs/adj-backfill-cli.md` 的只读 SQL 执行，Golden/哈希幂等归 #165。

## 11. #163 正式回填的上游登录限速与单会话改造（2026-11-05）

正式回填在 `qstock` 业务库执行 `adj-backfill --start 20230101 --end 20261104`。前 45 分钟按每请求
登录/登出推进到 508/928 个因子日（8,559 事件 / 4,116 票、0 失败日），随后因子日退化到约
40–45s/日；进程 46 分钟仅 1.65s CPU，PostgreSQL 连接长期 idle，时间耗在 baostock 网络等待。
16:22 单次探针（`socket.setdefaulttimeout(20)`）：DNS 正常（`114.94.20.42`），TCP 三次连接均
0.03–0.04s，但 `bs.login()` 20.04s 后返回 `10002007 网络接收错误`，`query_daily_adjust_factor`
20.0s 同样失败，`logout` 19.85s 同样失败。故不是本地网络/DNS/代码异常；只可能是上游故障或本机
在 45 分钟内约 508 次登录/登出触发登录限速。正式回填已停在 508 日断点，raw 阶段尚未开始。

据此把回填改为单登录会话：`BaostockAdapter(persistent=True)` 在整个回填进程内复用一次
`bs.login()`，因子日与事件票 raw 均在同一会话内串行查询（相邻 ≥1.5s、单请求 120s 外部硬超时、
会话被踢重登 ≤3 次），结束/异常时关闭会话。该改造有适配器单测（一次登录、踢后重登、关闭）
覆盖；**长会话下全量 5,000+ 请求的可承载性尚未实测**，必须等 baostock 恢复后用真实小样本先验证
再续跑，不能把本次单会话改造当作已证实的容量结论。事故证据与恢复策略见
`files/adj-backfill_formal_upstream_issue.md`。
