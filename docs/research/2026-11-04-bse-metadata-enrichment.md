# 北交所 raw/因子路径与腾讯 `day` 元数据富化

日期：2026-11-04。关联：#158、#167；待核实 spec：#168。本文是一手来源复核与实现前契约研究，不代表功能已经落地。

## 0. 方法、证据等级与结论速览

八份 `docs/research/2026-10-31-*.md` 已整体作废；本次只读取其研究问题和缓存位置，没有继承其分析或结论。下文数值均在 2026-11-04 从原始响应、CSV/JSON 缓存重新计算；未调用腾讯或 baostock 上游，未连接或修改数据库，未修改代码。Issue 约束以当日读取的当前正文为准。【I158】【I167】

证据标签：**实测** = 本次直接解析原始缓存；**源码事实** = 当前仓库或已安装 baostock 客户端的代码；**目标** = Issue 正文要求；**建议** = 本研究提出的实现/验收契约；**待验证** = 样本不能回答的问题。缓存样本只代表其代码、窗口与采集时点，不外推全市场覆盖率、长期稳定性或上游容量。

| 问题 | 结论 | 等级与证据 |
| --- | --- | --- |
| 北交所 raw 从哪里来 | 批量默认排除；显式开启后，raw 取腾讯不复权 `day`，qfq 只参与因子估计；单只查询不走默认批量排除 | 目标：【I158】【I167】；源码现状：【S-TX】【S-BS】 |
| 北交所能否取得 baostock 绝对累计 B | 不能沿沪深路径取得；两个 `bj.*` 逐票请求均被服务端以 `10004011 股票代码未标识sh或sz` 拒绝 | 实测：【R-BS-LEDGER】；源码事实：【S-BS】【S-BSAPI】 |
| 腾讯双请求得到什么 | 同一窗口的 `k(t)=qfq_close/day_close` 只提供相对复权尺度；相邻共同行可估计 `f(t)=k(t-1)/k(t)`，不能命名为 baostock 绝对 B | 实测与公式：【R-Q1】【R-D1】【R-Q2】【R-D2】【R-Q3】【R-D3】 |
| 北交所 coverage | 无论本轮是否发现跳变，均为 `partial`；缺 `qfqday`、任一侧缺行或无法对齐时是“因子未知”，不是“无事件” | 目标：【I158】【I167】；缺键实测：【R-MISS】 |
| 周批成本 | 每个代码、每个 640 根分块需 `qfq` 与 `day` 各一次，即 `2N` 次/单分块周批；多分块为 `2×Σchunks` | 源码事实与算术：【S-TX】；限量实测：【R-PACE】 |
| 旧码 | 两个请求和入库业务键都应先归一到 920 新码；旧码原样请求会拿到冻结历史，未映射旧码不能臆造新码 | 源码事实：【S-CODE】【S-ADR】；实测：【R-OLD】【R-Q2】 |
| `day[6]` 元数据结构 | 沪深缓存的 30 个非空行均为 `nd/fh_sh/djr/cqr/FHcontent` 五键字符串；六只北交所样本 3,840 行均为 `{}` | 实测：【R-META-HS】【R-META-BSE】；仅限样本 |
| 富化能决定因子吗 | 不能。只按 `(stock_code, ex_date)` 富化已有事件的 `kind/detail/reg_date`；不得用 `fh_sh` 或文本复算值覆盖沪深生产 `factor_cum` | 目标：【I158】；税后与文本/价格差异实测：【R-TAX】【R-BS-DAILY】【R-META-HS】 |
| 是否已落地 | 尚未。当前 CLI 没有 `--include-bse`，当前表仍是 `(stock_code,trade_date,adj)` 的 `stock_daily`，当前腾讯映射忽略 `day[6]` | 源码事实：【S-SCHED】【S-SQL】【S-TX】 |

## 1. 目标边界与当前实现的距离

### 1.1 不可改变的目标约束

1. **目标**：沪深 raw 与累计因子 B 的主源是 baostock；`adjustFacto` 经适配层映射为 `adjustFactor`，B 是累计值而不是单次事件因子。北交所不属于 baostock 主源覆盖范围。【I158】
2. **目标**：批量/全市场默认排除北交所；只有显式 `--include-bse` 才进入腾讯 `qfq`+`day` 同窗口双请求、按周批处理，且 `coverage=partial`。单只查询不受默认批量排除限制。【I158】【I167】
3. **目标**：请求失败、缺键、缺行均不能转换成“无事件”；只有成功且语义明确的结果才能证明相应事实。北交所价格比、T+0 未确认数据只能判 partial。【I158】【I167】
4. **目标**：腾讯 `day` 行元数据只用于 `kind/detail/reg_date` 富化，不决定沪深生产因子数值；生产因子仍走 baostock B 或其规定的正式降级路径。【I158】

### 1.2 当前源码不能误当成目标实现

- **源码事实**：当前 `daily` 默认仍是 qfq，逐代码走腾讯→新浪→baostock；`--all` 直接取整个 `stock_list`，参数列表没有 `--include-bse`。【S-SCHED】
- **源码事实**：当前存储主键是 `(stock_code,trade_date,adj)`，读写都按 `adj` 过滤；`query_daily` 响应尚无 `adjust.events/coverage` 结构。【S-SQL】【S-REPO】【S-DAILY】
- **源码事实**：腾讯 `qfq` 请求缺 `qfqday` 时会回落 `day`，设置 `adjust_fallback` 并出 note；这对旧取数链是可见降级，但对新北交所因子链仍不足以证明“无事件”。【S-TX】【S-DAILY】
- **源码事实**：腾讯映射只读 OHLCV、换手率、成交额等位置，未读取索引 6；其 `change_percent/change_amount/amplitude` 由相邻 close 推算，腾讯没有权威 `tradestatus`。【S-TX】
- **建议**：北交所新 raw 行不得沿用“相邻 raw close 自算涨跌幅”作为权威 `pctChg`；没有权威列时保留 `NULL`，以免除权日产生伪跌幅。这与 #158 的 raw 字段约束一致。【I158】【S-TX】

## 2. 北交所 raw + 相对因子路径

### 2.1 baostock 边界是显式不支持，不是空表推断

**源码事实**：项目 baostock 适配器对 4/8/9 开头代码显式抛 `unsupported`；其个股代码路由只生成 `sh.*`/`sz.*`。已安装客户端的逐票因子接口只展示 `sh.600000` 形式，日因子接口是单日全表、单页最多 20,000 行。【S-BS】【S-BSAPI】

**实测**：缓存台账中，`query_adjust_factor('bj.920001')` 与 `query_adjust_factor('bj.920571')` 都返回 `error_code=10004011`、`error_msg=股票代码未标识sh或sz`，而非成功空结果。四份日因子缓存合计 144 行也只有 `sh/sz`；这些缺行本身不是“不支持”的证明，显式错误才是直接证据。【R-BS-LEDGER】【R-BS-DAILY-FACTOR】

**结论**：北交所不能复用沪深“一次日表请求得到绝对 B”的路径，也不能把腾讯比值结果伪标成 baostock B。【I158】【R-BS-LEDGER】

### 2.2 同窗口双请求与数学口径

**建议的处理顺序**：

1. 先将输入代码归一为 canonical `920xxx`，再用同一个 `symbol/end/count/分块游标` 分别请求 `adjust=qfq` 与空 adjust；前者必须实际返回 `qfqday`，后者必须实际返回 `day`。当前接口单请求最多 640 根且忽略 start，因此多段回溯也必须保持两种口径的分块边界一致。【S-TX】【S-CODE】
2. raw 只取 `day` 的 OHLCV 等原始列；qfq 价格不得覆盖 raw。`close=NULL/<=0` 按 #158 隔离，不进入比值或派生。【I158】【S-TX】
3. 仅对两侧**日期完全共同且价格有效**的行计算：

   ```text
   k(t) = qfq_close(t) / day_close(t)
   f(t) = k(t-1) / k(t)       -- t-1 是同一共同序列的前一行
   ```

   若把 qfq 写成 `raw×B(t)/B(anchor)`，则 `k(t)=B(t)/B(anchor)`；所以 k 是窗口相对尺度，f 是相邻尺度之比，而不是独立的绝对 B。该公式只描述同一窗口内的数学关系，不证明任意跳变都是真实公司行为。【R-Q1】【R-D1】【I158】
4. 若需在派生层使用累计序列，可在窗内归一 `C(t)=k(t)/k(window_last)`，再由事件 f 重建；必须标 `source=tencent-qfq-day-ratio`、`coverage=partial`。不得把 C 的绝对值与 baostock B 跨源逐值比较。【I158】【R-Q1】【R-D1】
5. 跨窗口不能直接拼接 k/C 绝对值；要用重叠行/共同事件校准或从 f 重建。北交所不同 `end` 的重锚一致性本轮没有样本，列为待验证，而不是沿用沪深结论。【R-Q1】【R-D1】

**契约缺口提示（建议实现前显式解决）**：#158 的 `factor_cum` 注释以 baostock B 定义；北交所只能得到上述相对 C。实现若复用该列，必须在来源/文档中明确“北交所为归一相对累计值”，不能声称其绝对量纲与 B 相同；否则应另存相对因子表示。【I158】【R-BS-LEDGER】

### 2.3 三只北交所原始样本复核

本次以 Decimal 从原始 close 重算，三个配对均为 640/640 行、日期集合完全相同，且窗口末行 `k=1`；这只是三个窗口的观测，不是所有代码的保证。【R-Q1】【R-D1】【R-Q2】【R-D2】【R-Q3】【R-D3】

| 代码 | 跳变日 | 前一共同行 k | 当日 k | k 的相对跳变 |  implied `f=k_prev/k_cur` | 证据 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 920001 | 2024-06-04 | 0.9627329193 | 0.9742930591 | +1.200763% | 0.9881348432 | 【R-Q1】【R-D1】 |
| 920023 | 2024-05-14 | 0.9957692308 | 1.0000000000 | +0.424874% | 0.9957692308 | 【R-Q2】【R-D2】 |
| 920571 | 2025-11-25 | 0.9900199601 | 1.0000000000 | +1.008065% | 0.9900199601 | 【R-Q3】【R-D3】 |
| 920001 | 2025-12-08 | 0.9950421418 | 1.0000000000 | +0.498256% | 0.9950421418 | 【R-Q1】【R-D1】 |

这些数值证明“双序列能够暴露小于 1% 的尺度变化”，但没有交易所事件全集作为分母，因此不证明召回率，也不证明每个跳变都是分红/送转。920001 还出现 2024-04-16 的 k 相对下降 0.472244%；其原因未由公告核实，故不能把所有正向/反向抖动自动命名为事件，也不能增加“k 单调”约束。【R-Q1】【R-D1】【I158】

**建议**：事件识别采用可重放的稳定平台/邻域算法并保留候选与异常台账；不要只用单日 `abs(Δk)` 硬阈值。具体窗口、阈值、小额分红误报/漏报率均需北交所独立事件样本标定，目前待验证。【R-Q1】【R-Q2】【R-Q3】

## 3. 请求节奏、成本与续跑

### 3.1 请求数

设本轮显式纳入 N 只 canonical 北交所股票，第 i 只双口径各有 `c_i` 个 640 根分块，则：**实测接口形态 + 算术**，`requests=2×Σc_i`；单分块周批为 `2N`，raw 的 `day` 响应同时供元数据解析，不需要第三次请求。【S-TX】【R-META-BSE】

旧缓存的报价清单含 362 个代码，其中 356 个为 `bj920xxx`、6 个为其他旧码/特殊代码；只把 **N=356** 当作算术示例，不当作 2026-11-04 当前全市场数量。单分块双请求即 712 次；多历史分块另按公式增加。【R-UNIVERSE】

### 3.2 节奏不是上游容量

- **实测**：2026-10-31 两份腾讯台账合计 29 次请求均记 `ok=true`，配置间隔 1.6s，总墙钟 38.2s+54.5s；样本响应耗时合计 20.6s+28.8s。它只支持“该次串行样本在此节奏完成”，不支持长期 TPS 或并发上限结论。【R-PACE】
- **源码事实**：当前 K 线客户端单请求超时 20s，多段间 `CHUNK_PAUSE_S=0.3`；注释记录连发 5–6 个请求曾触发 TLS 断连。当前 CLI 的 1s 是逐 code 休眠，不保证同一代码内部两个 HTTP 请求之间的间隔。【S-TX】【S-SCHED】
- **建议**：北交所周批先采用**串行、每个 HTTP 请求完成后至少间隔 1.6s、不并发**，并为每请求保留硬超时、失败台账和断点续跑；0.3s 多段参数不能直接当成双请求周批的安全容量。上游可承载 TPS、最短间隔与并发上限均为**待验证**。【R-PACE】【S-TX】
- **算术成本示例**：712 次请求若每两次之间都等待 1.6s，仅等待时间为 `711×1.6=1137.6s`（18 分 57.6 秒），还需另加响应与重试时间；这不是生产耗时预测。【R-PACE】【R-UNIVERSE】

### 3.3 周批续跑契约

**建议**：以 `(canonical_code, window_start, window_end, chunk_end, adjust)` 记录抓取状态；只有对应响应键、业务码和日期对齐校验通过才记成功。重跑复用已验证分块，失败分块重试；不得因一个代码失败而把整批其他代码标 complete。北交所即使全部成功仍输出 partial 和 note。【I158】【I167】【S-TX】

**建议**：每周窗口至少带前一共同交易日/稳定平台所需重叠，不以自然周一切断 f 计算；跨窗重叠的长度取决于最终事件识别算法，目前待验证。【R-Q1】【R-Q2】【R-Q3】

## 4. 旧码、停牌、新股、缺键与缺行

### 4.1 旧码归一

**源码事实**：仓库内置 248 条官方新旧码映射，例如 `832023→920023`、`430047→920047`；映射不是简单替换。当前请求前会归一，但未映射旧码原样保留；ADR 还明确指出旧实现只归一请求、库存仍记录调用方原码，混用会形成两条序列。【S-CODE】【S-ADR】

**实测**：旧码 `bj832023` 的 qfq 原始响应虽成功，但 640 行止于 2022-06-09；归一后的 `bj920023` 同参数窗口为 2024-03-08..2026-10-30。成功响应因此不等于当前连续历史。【R-OLD】【R-Q2】

**建议**：默认排除、显式纳入、双请求、因子事件键和 raw 业务键全部使用同一个 canonical 920 代码；输入别名只进审计字段。未映射旧码不得猜测新码，应标 `legacy_unmapped/unknown`，不进入“当前无事件”集合。存量别名拆分需要单独迁移与验收，不能靠新写入自动合并。【S-CODE】【S-ADR】

### 4.2 缺键与日期不齐

**实测**：`bj920047` 的 qfq 请求返回节点只有 `day/prec/qt/version`，没有 `qfqday`，但 `day` 有 640 行；因此“能取 raw”和“能估计因子”是两个独立状态。【R-MISS】

**建议的状态机**：

| 情形 | raw | 因子/事件语义 | coverage/note |
| --- | --- | --- | --- |
| `day` 有、`qfqday` 有、日期对齐 | 可写 | 可计算相对 k；候选事件仍需算法校验 | 固定 partial |
| qfq 请求只回 `day` | 可写 day | `factor_unavailable`；不得写 f=1 或声称无事件 | partial + 缺键 note |
| 任一侧某日期缺行/日期集合不同 | 已有有效 raw 可写 | 该接缝 `alignment_unknown`；不跨缺口补 f=1 | partial + 对齐鲁项 |
| 两侧均空或请求失败 | 不伪造 raw | 空、失败、未上市、停牌不可混为无事件 | 按实际状态记 empty/error/unknown |
| 元数据 `{}` 或缺列 | 不影响 raw | 不删除、不创建、不否决价格比事件 | 元数据缺失，非无事件 |

依据：缺键实测【R-MISS】；目标缺键/缺行约束【I167】；当前空列表协议与中段缺口默认假设【S-BASE】【S-GAPS】。

### 4.3 停牌与新股

- **源码事实**：腾讯无权威 `tradestatus`；当前查询层的启发式只识别特定 OHLC 全等且 volume/change_percent 为空的占位行，不能证明所有停牌形态。当前头尾自愈还把中段缺口视作停牌/非交易日，因此新因子管线必须另留“缺口未知”状态。【S-TX】【S-DAILY】【S-GAPS】
- **实测**：`bj920162` 的历史请求缓存中 `day=[]`，但响应同时含另一个时点的 `qt` 报价节点；这说明 K 线空数组本身不能扩写成“该证券无事件”，也不能拿 qt 补造历史 raw 行。【R-EMPTY】
- **建议**：新股首个有效共同行没有前一共同行，f 无定义；可初始化北交所相对因子状态和后续锚点，但必须保持 partial，不得倒推上市前无事件。长期停牌、复牌、停牌期间公司行为及两侧日期错位均待北交所专项样本验证。【I158】【R-EMPTY】【S-DAILY】

## 5. 腾讯 `day[6]` 元数据：schema 与解析边界

### 5.1 一手样本结构

**实测**：六份沪深 `day` 缓存共 3,840 行，其中 30 行索引 6 非空；30/30 都恰好含 `nd,fh_sh,djr,cqr,FHcontent`，所有值均为字符串。其余行在该样本中为 `{}`。30/30 的 `cqr` 等于本行日期，`djr` 等于样本中的前一 K 线日期。【R-META-HS】

| 键 | 样本支持的解释 | 富化用途 | 证据 |
| --- | --- | --- | --- |
| `cqr` | 除权除息日；样本 30/30 等于行日期 | 仅用于匹配已有事件 `ex_date`，不匹配则报异常 | 【R-META-HS】 |
| `djr` | 股权登记日；样本 30/30 等于前一 K 线日期 | 转 `yyyymmdd` 后写 `reg_date` | 【R-META-HS】 |
| `FHcontent` | 方案原文；观察到纯派息、纯转增、转增+派息 | 原样写 `detail`，并只做 kind 词法分类 | 【R-META-HS】 |
| `fh_sh` | 现金字段，但历史样本与税前文本不一致 | 不用于生产因子，不作为 detail 的替代 | 【R-META-HS】【R-TAX】 |
| `nd` | 年度字符串；不能区分同一年度多次方案 | 不进入当前三字段富化键 | 【R-META-HS】【I158】 |

**实测**：六只北交所代码（920001/920023/920047/920185/920571/920982）各 640 行，合计 3,840 行，索引 6 全为 `{}`。这只能写为“这些窗口未提供元数据”，不能写成“北交所无事件”或全市场永久为空。【R-META-BSE】【R-Q1】【R-Q2】【R-Q3】

### 5.2 文本不是数值因子真源

**税后边界实测**：600000 的三行分别为：

| 除权日 | 腾讯 `FHcontent` | 腾讯 `fh_sh` | baostock 税前/税后原始字段 |
| --- | --- | --- | --- |
| 2012-06-26 | `10派3元` | `2.7` | 每股税前 0.3、税后 0.27 |
| 2013-06-03 | `10派5.5元` | `5.225` | 每股税前 0.55、税后 `0.495或0.5225` |
| 2014-06-24 | `10派6.6元` | `6.27` | 每股税前 0.66、税后 `0.594或0.627` |

因此 fh_sh 在这些历史行对应税后口径；至少这三例不能当税前现金。样本没有定位其他年份的切换规则。【R-META-HS】【R-TAX】

**文本/参考价差异实测**：用 `P=前一 baostock raw close`、文本税前现金和送转比例复算参考价，再与 baostock `preclose` 对比，以下四例差异超过 0.005 元：600519/2025-06-26 为 +0.0730 元；601318/2025-06-30 为 +0.0200 元；601318/2024-10-18、2024-07-26 均为 +0.0100 元。对应 baostock 分红文本仍只给税前/税后方案，不能仅凭这些响应证明差异原因就是差异化分红；“回购专户导致差异化分红”等解释必须另查实施公告，当前为待验证。【R-META-HS】【R-BS-DAILY】【R-BS-DIVID】

**结论**：无论税后字段还是文本复算，都不能覆盖沪深生产 B/factor_cum。它们可以解释 kind/detail，可作异常诊断，但不参与 #158 的生产因子定值。【I158】【R-TAX】【R-BS-DAILY】

### 5.3 解析器契约（建议）

1. 在 JS 赋值去除后用 JSON 解析，再取 `data[symbol].day[i][6]`；不要直接正则扫描带 `\uXXXX` 的响应文本。当前通用 K 线 JSON 解析可复用，但行映射需新增显式元数据出口。【S-TX】【R-META-HS】
2. `{}`、缺索引、`null`、非 dict 都归为 `metadata_unavailable`；不得映射成 `no_event`。非空 dict 先保留原值，再校验必填键和日期格式；部分键/额外键不丢原文，标 `metadata_unparsed` 或 `metadata_partial`。【R-META-HS】【R-META-BSE】
3. `detail=FHcontent` 原样保留，包括 `", "` 空格和 1–3 位小数；不能用 `fh_sh` 重建或规范化 detail。样本出现 `10转3股`、`10转3股, 10派5元`、`10派280.242元`，证明空格和小数位不能写死。【R-META-HS】
4. kind 使用稳定机器词：文本含“派”且有“送/转”→`cash+share`；仅“派”→`cash`；仅“送/转”→`share`；其他文本→`unknown` 并保留 detail。送股、送+转+派、配股等未在样本出现，需新增 fixture 后才扩词汇，不得解析失败即丢事件。【R-META-HS】
5. `cqr` 必须与本行日期一致才能按该 ex_date 富化；`djr` 转日期后写 reg_date。样本支持这两个语义，但不应把“djr 必须等于前一 K 线日期”设为永久通用约束，以免停牌/日历差异制造拒绝。【R-META-HS】

## 6. 富化写入契约（建议）

富化是**已有因子事件的局部更新**，不是第二条因子生产线：

```text
input: canonical stock_code + day row + parsed metadata
match: stock_adjust_factor(stock_code, ex_date=cqr)
set:   kind, detail=FHcontent, reg_date=djr
keep:  factor_cum、source、created_at 及其他事件字段不变
```

- 有事件、无元数据：保留事件，三字段维持原值/NULL；不判无事件。【I167】【R-META-BSE】
- 有元数据、无匹配事件：记 `unmatched_metadata` 异常，不创建绝对 B，不用文本计算 factor_cum；沪深由 baostock 事件链排查，北交所保持 partial。【I158】【R-BS-DAILY】
- 有匹配事件：只更新三字段；`source` 仍表示因子来源，不因富化改成 tencent。重复富化应得到同样三字段和同样因子值。【I158】【S-TX】
- 北交所样本的 `{}` 不为价格比候选事件补造 kind/detail/reg_date；以后若出现非空元数据，也只能按同一匹配规则富化，不能替代 qfq 缺键/缺行状态。【R-META-BSE】【I167】

## 7. 验收设计（建议，不是已执行测试）

### 7.1 Golden 与结构验收

| 组 | 必须断言 | 证据来源 |
| --- | --- | --- |
| 双请求同窗口 | 每个分块的 symbol/end/count/游标一致；实际键分别恰为 qfqday/day；qfq 不写 raw | 【S-TX】【R-Q1】【R-D1】 |
| 三只比值样本 | 重算 §2.3 四个日期的 k、f；共同日期缺失时不出值；结果始终 partial | 【R-Q1】【R-D1】【R-Q2】【R-D2】【R-Q3】【R-D3】 |
| 缺键 | 920047 可保留 day raw，但因子状态为 unavailable；不得产生“无事件”事件集或 f=1 | 【R-MISS】【I167】 |
| 旧码 | 832023 的两个请求及入库键均为 920023；未映射旧码不臆造；别名重跑不生成第二序列 | 【S-CODE】【S-ADR】【R-OLD】 |
| 空行/新股/停牌 | day 空不造 raw；首共同行不算 f；任一侧缺行不跨缺口填 f=1；停牌原因未知不被覆盖成无事件 | 【R-EMPTY】【S-DAILY】【S-GAPS】 |
| 因子隔离 | 北交所相对 C 不与 baostock 绝对 B 做等值断言，响应/台账明确 ratio 来源和 partial | 【I158】【R-BS-LEDGER】 |

### 7.2 元数据 Golden

1. 688300/2026-10-16：`cqr=2026-10-16`、`djr=2026-10-15`、`FHcontent=10转3股`，期望 `kind=share`、`reg_date=20261015`、detail 原文；事件 factor_cum 在富化前后逐值相同。【R-META-HS】【I158】
2. 688300/2025-05-29：`10转3股, 10派5元`，期望 `kind=cash+share`；逗号后空格原样保留。【R-META-HS】
3. 600519/2026-06-26：`10派280.242元` 三位小数不截断；600000 三个历史样本证明 fh_sh 不参与现金或因子计算。【R-META-HS】【R-TAX】
4. 四个文本/preclose 差异样本只触发诊断 fixture，不改变 baostock factor_cum；不得把“差异化分红”写成已证实原因。【R-BS-DAILY】【R-BS-DIVID】
5. 非事件行 `{}`、缺列、null、部分键、额外键、错误 cqr、未知 FHcontent 模板都有解析 fixture；缺失/异常不删除已有事件，未匹配元数据进入异常台账。【R-META-HS】【R-META-BSE】
6. 六只北交所缓存重放后 metadata 三字段不被虚构，价格比路径的缺键状态也不因 `{}` 改变。【R-META-BSE】【R-MISS】

### 7.3 作业、幂等与输出验收

- 默认批量 Universe 不含 canonical 北交所；`--include-bse` 才进入双请求周批；显式单只查询不被该默认过滤器拒绝。当前 CLI 尚无该开关，需新增契约测试而非宣称已有。【I158】【I167】【S-SCHED】
- 用假时钟记录每次 HTTP 调用，验证串行、请求间 1.6s 保守节奏、超时和失败续跑；N 只单分块恰好 2N 次，day 响应同时供 raw 与元数据，无第三次富化请求。【R-PACE】【S-TX】
- 同一原始响应重放两次，raw、事件三字段、factor_cum、来源及摘要保持幂等；中断后只续未完成分块。任何缺键/缺行/未决异常不得输出“数据可以使用”，北交所成功批也固定 partial+note。【I158】【I167】
- 性能验收只记录限定样本的真实耗时、失败和重试；不得由 29 次旧缓存样本推导全市场长期 TPS。放量前仍需独立的小规模连续周批实测。【R-PACE】

## 8. 待验证清单（不得静默转成结论）

1. **北交所事件真值与识别精度**：需要交易所公告/实施公告构成独立事件集，标定小额分红、反向跳变、缩股及平台算法；三只配对样本不能给全市场召回率。【R-Q1】【R-Q2】【R-Q3】
2. **跨窗口重锚**：至少两只北交所、两个不同 end、含重叠事件的原始响应，验证 f 不变量与重叠拼接；本轮没有该样本。【R-Q1】【R-D1】
3. **停牌/复牌/新股**：需要停牌跨公司行为、长期停牌复牌和近端新股双口径样本，确认两侧是否同时缺行及占位形态；当前腾讯无权威 tradestatus。【S-TX】【R-EMPTY】
4. **缺键范围**：920047 只证明该代码该窗口缺 qfqday，不能外推缺键比例或永久不支持。【R-MISS】
5. **北交所元数据覆盖**：六只×640 行全空只代表样本；需用有独立事件日的其他北交所样本复核，不能以 `{}` 推断无事件。【R-META-BSE】
6. **元数据模板与历史**：送股、送+转+派、配股、部分键、2015–2023 的 fh_sh 口径变化均未覆盖；差异化分红需实施公告确认。【R-META-HS】【R-TAX】【R-BS-DIVID】
7. **请求容量**：上游最短间隔、可承载 TPS、并发上限和 712 次级周批稳定性均待验证；先串行低频限量试跑，不自动放量。【R-PACE】【S-TX】
8. **存储语义**：实现前需确认北交所相对 C 在 `factor_cum` 中的来源与量纲说明，避免与 baostock B 混称；coverage=partial 不能替代这个语义说明。【I158】【R-BS-LEDGER】

## 9. 证据索引

### Issue / 目标

- 【I158】https://github.com/daanZao/q_stock_skills/issues/158 ，当前正文 §0、§2.2、§3、§4.4、§5、§7。
- 【I167】https://github.com/daanZao/q_stock_skills/issues/167 ，当前正文 What to build 与 Acceptance criteria。

### 当前源码 / 客户端源码

- 【S-TX】`src/qstock_mcp/adapters/tencent_adapter.py:85-90`、`:113-184`、`:204-285`、`:369-390`。
- 【S-BS】`src/qstock_mcp/adapters/baostock_adapter.py:22-23`、`:51-62`、`:85-106`、`:151-177`、`:183-199`。
- 【S-BSAPI】`.venv/lib/python3.12/site-packages/baostock/evaluation/season_index.py:107-183`、`:185-243`。
- 【S-CODE】`src/qstock_mcp/adapters/_bse_codes.py:29-73`；`src/qstock_mcp/data/bse_code_map.csv:1-3`、`:83`、`:218`。
- 【S-ADR】`docs/adr/0010-daily-fallback-tencent-sina-baostock.md:39-47`、`:52-66`。
- 【S-SCHED】`src/qstock_mcp/scheduler.py:375-388`、`:411-458`、`:830-853`。
- 【S-SQL】`src/qstock_mcp/sql/001_stock_daily.sql:1-24`。
- 【S-REPO】`src/qstock_mcp/repository.py:32-49`、`:101-118`。
- 【S-DAILY】`src/qstock_mcp/tools_daily.py:31-79`、`:115-138`、`:156-159`、`:262-299`。
- 【S-BASE】`src/qstock_mcp/adapters/base.py:8`、`:408-414`。
- 【S-GAPS】`src/qstock_mcp/gaps.py:1-21`。

### 腾讯原始响应与台账

- 【R-Q1】`.scratch/tencent_adj_141/raw/05_bj920001_qfq.txt:1`。
- 【R-D1】`.scratch/tencent_adj_141/raw/12_bj920001_none.txt:1`。
- 【R-Q2】`.scratch/tencent_adj_141/raw/07_bj920023_qfq.txt:1`。
- 【R-D2】`.scratch/tencent_event_153/raw/bj920023_20261031.txt:1`。
- 【R-Q3】`.scratch/tencent_adj_141/raw/08_bj920571_qfq.txt:1`。
- 【R-D3】`.scratch/tencent_event_153/raw/bj920571_20261031.txt:1`。
- 【R-MISS】`.scratch/tencent_adj_141/raw/06_bj920047_qfq.txt:1`；键/行数台账另见 `.scratch/tencent_adj_141/probes.json:291-345`。
- 【R-OLD】`.scratch/tencent_adj_141/raw/09_bj832023_qfq.txt:1`；首末行摘录另见 `.scratch/tencent_adj_141/probes.json:462-516`。
- 【R-EMPTY】`.scratch/snapshot_155/raw/P3_bj920162.txt:1`。
- 【R-UNIVERSE】`.scratch/tencent_batch_prec_150/raw/R2_all362.txt:1-362`；请求台账 `.scratch/tencent_batch_prec_150/ledger.json:13-20`。
- 【R-PACE】`.scratch/tencent_adj_141/probes.json:692-699`；`.scratch/tencent_adj_141/probes2.json:977-984`；各次 `ok/elapsed` 同文件 probes 数组。
- 【R-META-HS】`.scratch/tencent_event_153/raw/sh688300_20261031.txt:1`、`sh600519_20261031.txt:1`、`sh601318_20261031.txt:1`、`sz000001_20261031.txt:1`、`sh600028_20261031.txt:1`、`sh600000_20141231.txt:1`（均位于同目录）；请求 URL/窗口/行数见 `.scratch/tencent_event_153/ledger.json:2-115`。
- 【R-META-BSE】`.scratch/tencent_event_153/raw/bj920023_20261031.txt:1`、`bj920571_20261031.txt:1`、`bj920982_20261031.txt:1`、`bj920185_20261031.txt:1`；`.scratch/tencent_batch_prec_150/raw/R7_kline_bj920001_640.txt:1`、`R8_kline_bj920047_640.txt:1`；窗口/行数台账见 `.scratch/tencent_event_153/ledger.json:117-191` 与 `.scratch/tencent_batch_prec_150/ledger.json:62-80`。

### baostock 原始响应

- 【R-BS-LEDGER】`.scratch/baostock_bse_154/ledger.jsonl:1-6`，尤其 `:5-6` 的服务端显式错误。
- 【R-BS-DAILY-FACTOR】`.scratch/baostock_bse_154/raw/d_20240514.csv:1-22`、`d_20240604.csv:1-64`、`d_20251125.csv:1-13`、`d_20261016_ctrl.csv:1-49`（均位于同目录）。
- 【R-TAX】`.scratch/tencent_event_153/bs_raw/divid_sh600000_2012.json:1`、`divid_sh600000_2013.json:1`、`divid_sh600000_2014.json:1`（均位于同目录）。
- 【R-BS-DAILY】`.scratch/tencent_event_153/bs_raw/daily_sh600519.json:1`、`daily_sh601318.json:1`（fields/rows 原始缓存）；对应腾讯文本见【R-META-HS】。
- 【R-BS-DIVID】`.scratch/tencent_event_153/bs_raw/divid_sh600519_2025.json:1`、`divid_sh601318_2024.json:1`、`divid_sh601318_2025.json:1`（均位于同目录）。
