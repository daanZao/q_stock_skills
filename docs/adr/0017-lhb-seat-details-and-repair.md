# ADR 0017：LHB 沪深席位详情按父记录落库与默认修复

2026-10-30，issue #137；扩展 ADR 0015/0016 的 basic 采集与市场级后备语义，不改变其来源优先级。

## Context

- `lhb_stock_detail` 的 schema 与查询入口早已存在，但单日、`close` 和历史回溯只写 basic/statistic，已有 basic 无法通过默认重跑补齐席位详情。
- 同花顺同一历史页同时包含 basic 和买卖席位表，逐股追加请求会放大风控压力；上海交易所文件本身也已包含席位。两源的字段能力不同：同花顺提供买额、卖额、原始净额和 `orgcode`，交易所后备可能只有方向金额，不能伪造反向金额、净额、百分比或三个月计数。
- 详情是 basic 的子记录。整行 upsert 不能处理父记录换源、榜单原因变化或部分父记录异常；整天拒绝又会丢失已经取得的有效 basic 和其他股票的详情。

## Decision

- 深圳历史从同花顺同一页面解析 basic 与详情，每日期仍最多一次请求；单日及官方后备使用深交所明细，上海使用交易所文件。北交所 basic 保持现状，不保存详情、不计详情缺失。统计前置 staging 仍只提供 basic，不写窗外详情；两个营业部累计榜不变。
- `lhb_stock_detail` 增加可空 `seat_code`、`buy_amount`、`sell_amount`、`net_buy_amount`。`seat_type` 使用 `buy`/`sell`，兼容列 `amount` 等于该方向金额；无可靠来源的百分比、三个月计数及官方未提供的反向金额/原始净额保持 NULL。
- 详情按 `(trade_date, stock_code, lhb_reason)` 父业务键整体替换。重复席位或买额−卖额与原始净额矛盾时，该父记录的新详情整组拒收，其他父记录继续；basic 成功而详情缺失或部分失败时保存已取得数据并报告 `partial_error`，单日 CLI、`close` 和批次退出码均为 1。
- 默认历史窗口区分三种状态：basic 与详情完整则保留；basic 完整但详情缺失或部分失败则复用库内 basic、只补详情；其余整天重抓。`--force` 同时重抓 basic 和详情。basic 失败仍保留旧日，详情部分失败不再丢弃有效 basic。
- 响应和采集台账增加 `detail_rows`、`detail_status`、`detail_missing`、`detail_source`；basic 与 detail 分别记录实际来源。原 `rows` 口径不变，详情行数单列。
- 2026-10-30 对同花顺 2026-10-20 历史页做一次单页样本验证：72 条 basic、33 个深圳父记录、330 条详情，解析零详情错误。请求沿用每日期一次、日期间 20 秒退避；最小安全间隔、TPS、并发及长期可用性仍待验证，单页结果不外推。

## Consequences

- 旧 basic 可通过普通历史重跑逐步修复，无需整窗 `--force`；完整详情默认不被重写，重复执行不会产生重复子行。
- 上海官方失败、仅以同花顺补 basic 时会形成可重跑的详情缺口，而不是被报告为完整日。北交所不会因此产生伪 `partial_error`。
- ADR 0014 的 statistic 自算窗口及 `buy_seat_count`/`sell_seat_count` 恒 NULL 不变；本期不因详情已接线而隐式改变统计口径。
