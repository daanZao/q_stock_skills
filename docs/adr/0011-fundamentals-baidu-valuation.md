# ADR 0011：基本面透传剔东财——百度股市通估值接入 + base_info 弃用（planned_absent 先行承载）

2026-10-21，issue #115（spec #111；调研 `docs/research/2026-10-20-fundamental-gaps.md`）。

## Context

- 基本面透传链原为 akshare → efinance → baostock。剔东财直接命中两段：
  - efinance `get_base_info`（东财 push2 单日估值/行业快照）随 efinance 退役消失；
  - akshare `valuation_indicator` 段（乐咕 `stock_a_indicator_lg`）**先于**剔东财
    已断供——当前 akshare 1.18.94 已无该函数（AttributeError 进 errors 键，
    section 级容错使链不 fallback，baostock 永远补不上估值）。
- 断供面即估值：个股 PE/PB/市值日序列与快照双缺。baostock 对估值字段零对应，
  新浪 abstract 也不含估值。
- 替代候选评估（调研报告第三节）定稿：akshare `stock_zh_valuation_baidu`
  （百度股市通）零新依赖、本机实测可用（600519 PE-TTM 近一年 366 行），
  覆盖 总市值/市盈率(TTM)/市盈率(静)/市净率/市现率 五指标日序列；
  缺 PS/股息率/流通市值/行业，属可接受的已知缺口（候选 B tushare 需 2000
  积分门槛，列长期方案，不进本期）。
- #113 将为全部采集面落地第四态 `planned_absent` 编排契约（弃用面不误报、
  不中止）；本 ticket 不能等 #113，需"同等手段"先行。

## Decision

- **akshare 段重接线**：`fetch_fundamentals` 的估值段由乐咕 `stock_a_indicator_lg`
  改为百度 `stock_zh_valuation_baidu`（period=近一年，单股单指标一次请求即
  全年序列），section 名 `valuation_indicator` → `valuation`：五指标各自
  的 date/value 记录按指标名分组（上游列名原样保留，JSON 安全化后 date 为
  ISO 字符串）。单指标失败只记 errors 不拖垮其他指标（沿用 section 级容错）；
  `financial_abstract`（新浪）不动。
- **efinance 段弃用**：`EfinanceAdapter.fetch_fundamentals` 不再触达东财 push2，
  显式抛 `FetchError(kind="unsupported")` 弃用错误；`default_fundamentals_adapters()`
  链改为 **akshare → baostock**。baostock 季度 `profit`/`growth` 兜底语义不变。
- **弃用清单先行承载第四态**：新增 `deprecations.PLANNED_ABSENT_SECTIONS`
  常量（section → 弃用原因），`get_fundamentals` 响应带 `planned_absent` 键
  显式声明（ok/error 两路径都带）——base_info 是决策性缺席：不进 errors、
  不误报故障、不静默消失。#113 的第四态机制落地后可直接消费该常量生成
  planned_absent 占位（接口不变，只改 import 来源）。
- **固定 CLI 入口**：`qstock-scheduler fundamentals --codes A,B`——逐 code 走
  get_fundamentals 链出状态报告（section 覆盖/单 section errors/弃用清单/
  逐源明细），proxy 面不落库；可挂定时任务做链路健康巡检，也是本 ticket
  的真实拉取验收入口。
- **测试口径**：协议解析用录制响应 fixture（2026-10-21 真实拉取 600519 百度
  五指标 + 新浪 abstract 节选，存 `tests/fixtures/baidu_valuation/`），fake ak
  模块注适配器层，零网络；fake 适配器注 tools_* 层。

## Consequences

- 估值序列（PE-TTM/PE-静/PB/PCF/总市值）恢复供给，sepa E 维度五项字段
  （EPS/营收/毛利率/扣非/ROE）全在幸存的新浪 abstract 内，E 维度消费不受损；
  兜底态（baostock）缺扣非/营收同比/多季度深度的既有降级语义不变。
- 百度估值非官方授权接口（gushitong opendata），有改版/风控风险；上游列仅
  date/value、单位保留上游（总市值为亿元，ADR 0006 的万元口径不适用于
  proxy 透传）。免费源瞬时抖动（SSL EOF）靠链内既有重试兜底。
- 已知缺口（PS/股息率/流通市值/行业）无兜底；如未来需要，tushare
  （fina_indicator + daily_basic）是唯一全字段候选，但需新依赖 + 积分门槛。
- 北交所基本面：新浪 abstract 未验证、baostock 明拒、百度未验证——三源均
  不承诺，与调研报告一致。
- efinance 适配器其他方法（盘面/快照/清单）仍在链中，其整体移除是 spec #111
  的其他 ticket；本 ADR 只约束基本面面。
