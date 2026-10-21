# 抓取错误三态契约：upstream_unreachable / no_data_for_date / empty_ok

2026-10-14 上游 `push2.eastmoney.com` 集群整体故障（TCP/TLS 握手成功后空响应，HTTP 000），`fetch_market_snapshot` 的 efinance 与 akshare 两源全断、baostock 按设计不支持全市场快照，但工具返回的错误无法区分三种本质不同的情况：**上游不可达**（应中止并稍后重试）、**该交易日无数据**（非交易日/盘后未发布，正常语义）、**正常空结果**。编排 skill 拿到含混错误无法决定中止还是降级，当日复盘在数据就绪阶段中止（取证见 `files/daily_review_20261014_blockers.md` P0-1）。

抓取（fetch）能力面的错误契约定为三态：

- **`upstream_unreachable`**：连接级故障——请求**未获得任何 HTTP 响应**（连接被重置、TLS 后空响应、超时、DNS/建连失败）。收到 HTTP 响应（含 4xx/5xx）不属此类，按各工具现有语义处理。工具返回 `status:"error"` + `error_kind:"upstream_unreachable"` + `retry_advice`（稍后重试）+ `attempted_sources` 逐源明细。
- **`no_data_for_date`**：上游正常响应但该日期无数据（非交易日、盘后未发布等）。语义为**正常而非失败**：`status:"ok"` + `rows_upserted:0` + `empty_kind:"no_data_for_date"` + `note`。
- **`empty_ok`**：查询条件合理的空集。本工具（全市场快照，无过滤条件）无此路径；该态预留给带过滤条件的抓取/查询工具。

连接级故障的**可机器判定标准**：沿异常的 `__cause__`/`__context__` 链按类名判定（标准库 `ConnectionError`/`TimeoutError`/`socket.gaierror`、`http.client.RemoteDisconnected`/`IncompleteRead`、requests/urllib3 的 `ConnectTimeout`/`ReadTimeout`/`MaxRetryError`/`NewConnectionError`/`ProtocolError` 等），命中即 `connection`。判定收敛在 `adapters/base.py` 的 `fetch_failure_kind`，按类名鸭子类型匹配、不 import 可选第三方库（与 `json_safe` 同策略）。by-design 拒绝（如 baostock 无全市场快照、efinance 不支持指数日线、北交所代码）由适配器抛 `FetchError(..., kind="unsupported")` 显式标注，**不算抓取故障、不参与 upstream_unreachable 判定**。

逐源分类在 `fetch_chain._with_fallback` 记录（`attempted_sources` 每项增加 `failure_kind`：unsupported / connection / other；补漏路径另有第四值 `empty`，见下节补记）；工具层聚合：全源失败时任一抓取源为 `connection` 即 `error_kind:"upstream_unreachable"`，否则 `error_kind:"fetch_failed"`（三态之外的通用抓取失败，保持现有 error 语义，不带重试建议）。

**不加非 push2 兜底源**（已决策）：数据源故障时快速失败、告知调用方稍后重试，不硬绕路取数。本契约只做错误分类，不做新适配器。

调用方（编排 skill）据此机械决策：`upstream_unreachable` → 中止并出异常报告、稍后重试；`no_data_for_date` / `empty_ok` → 按现有降级规则继续。逐源明细（适配器名 + 原始报错）可直接编进异常报告，并区分单源问题与集群性故障。

## 补记（2026-10-15，issue #49）：补漏路径的第四个逐源 failure_kind `empty`

`fetch_board_snapshot` 的 indices 段定点补漏按单 code 走 `fetch_index_spot_with_fallback`，源的指数口径可能根本不含该指数（如 efinance「沪深系列指数」口径系统性排除科创板系列）。此时上游正常响应、按 code 过滤后为空——属**口径性缺失而非故障**：`failure_kind:"empty"`，不视为该源成功（覆盖目标未达成，立即转下一源）、不计入抓取失败、与 `unsupported` 一样不参与 `upstream_unreachable` 判定，且不重试（口径不含，重试无意义）。聚合由 `aggregate_patch_error_kind` 负责：剔除 empty/unsupported 后任一连接级故障即 `upstream_unreachable`（带 retry_advice），有非连接失败为 `fetch_failed`，全源 empty/unsupported 归 `no_data_for_date`。

边界：`empty` 仅限补漏（patch）路径的逐源 `failure_kind`；通用 fallback（`_with_fallback`）逐源分类仍只有 unsupported / connection / other 三值，工具层 `error_kind` 侧仍严格三态，均不得引入 `empty`。

## 补记（2026-10-17，issue #78，spec #71/W1）：实时 section 历史回补不支持 + 盘中禁写

实时语义 section（`fetch_board_snapshot` 的 `boards`/`indices`、`fetch_market_snapshot`）的上游接口只有当日实时口径，**历史日期回补 by-design 不支持**，归 `unsupported`：对历史日期（trade_date < 当日）调用返回 section 级/工具级 `status:"error"` + `error_kind:"unsupported"` + `detail:"realtime_section_no_backfill"`，**零落库**（含台账——未发生采集动作）。请求的 section 全部被防线拒收时工具整体 `error_kind:"unsupported"`。这与适配器层 `FetchError(kind="unsupported")` 同词汇：by-design 不支持，不算抓取故障、不参与 `upstream_unreachable` 判定、不触发核心 section fail-fast。

关联防线（同属 #78，细节不在本 ADR 展开）：`indices` 段落库前校验行级日期==请求日期（不符即 `detail:"date_mismatch"` 零落库）；`fetch_market_snapshot` 传入日期与 API 生效日期不符（含 API 未报告）即 `status:"error"` + `detail:"date_mismatch"` 零落库；交易时段内（交易日 09:30–15:00 东八区）`stock_daily`/`index_daily` 写路径跳过当日行落库（盘中禁写，响应带 `note`，盘中 fetch/query 正常返回数据）。当日正常路径行为不变。

## 补记（2026-10-21，issue #112，spec #111）：北交所不再整体 unsupported；日线源代码归一化

个股日线 fallback 链改为 腾讯 → 新浪 → baostock 后，北交所 920xxx 由腾讯/新浪
承载（三复权口径实测可取），因此"北交所 = by-design unsupported"只对 **baostock**
成立，不再是全链的结论：`BaostockAdapter` 继续以 `is_bse_code`（4/8/9 前缀）
拒绝北交所并抛 `FetchError(kind="unsupported")`；腾讯/新浪适配器则通过
`adapters/_bse_codes.py` 把 920xxx 路由到 bj，旧码 43x/83x/87x 按北交所官方
248 条对照表归一化到 920 新码后再请求。

`base.is_bse_code` 与 `_bse_codes.market_of` 的差异是刻意的：前者是 baostock
的粗粒度拒绝守卫（9 开头一律不接，含 900xxx 沪 B 股），后者是 A 股市场路由
（仅 920xxx 与 4/8 旧码归 bj；900xxx/200xxx 等非 A 股明确 unsupported）。新增
数据源接北交所时以 `_bse_codes` 为准，不得复用 `is_bse_code` 当市场判定。

对拍（`daily_parity.compare_daily_sources`）的逐源失败沿用本契约分类：输出
`sources.<name>.failure_kind`（unsupported/connection/other）与 `status`
（unsupported/error/no_data），不另造分类。

## Consequences

- `fetch_market_snapshot` 首个落地（issue #46）；`fetch_board_snapshot` 及其他 `fetch_*` 工具的落地在后续 issue，落地时必须复用本契约的三态命名与判定标准，不得另造分类（补漏路径的 `empty` 是本 ADR 补记明定的唯一例外，边界见上节）。
- 新增数据源适配器时：by-design 不支持的能力必须抛 `FetchError(kind="unsupported")`；连接级异常不得被吞掉或改写成无 `__cause__` 的普通报错（保持 `raise FetchError(...) from e`），否则该源故障会被误分类为 `other`。
- `attempted_sources` 项新增 `failure_kind` 字段，属增量字段，消费方按既有结构读取不受影响。
- 空结果不触发 fallback 的既有语义不变：上游正常响应空 = `no_data_for_date`（成功语义），不视为该源失败。
- 判定依赖异常类名白名单，新型连接层库接入时需把其连接级异常类名补进 `_CONNECTION_ERROR_NAMES`。
