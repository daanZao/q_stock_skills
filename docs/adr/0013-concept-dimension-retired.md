# ADR 0013：概念板块维度永久退役（THS 概念成分端点准入闸未过）

2026-10-25，issue #119（spec #111；调研 #106，research 分支
`docs/research/2026-10-20-ths-concept-boards.md`）。

## Context

- spec #111 留了一条概念板块的活路：每周自抓 THS 概念名单+成分、每日库内
  自算概念行情，**准入闸**先行——手动验证 THS 概念成分分页端点
  （q.10jqka.com.cn）在周频 + 有效 cookie/Referer 下可过 IP 风控；不通则
  概念维度退回 #106 弃用方案。
- #106 调研（2026-10-20）已实测：该端点无 cookie 返 chameleon JS 挑战、
  带有效 hexin-v 返 `Nginx forbidden`（IP 级，打印本机 IPv6）；akshare
  1.18.94 已无 `stock_board_concept_cons_ths`，成分只能自抓该端点。
- 概念板块在复盘链路的 6 个消费点全为 C 级增强项，"整栏删除"降级机制
  已具备；且 THS 承接恰恰补不上两个差异化字段（领涨股、成分/命中家数）
  ——两者全压在被封禁的成分端点上。

## 准入闸实测（2026-10-25 本机，4 次低频请求）

| # | 端点 | 凭证 | 结果 |
|---|---|---|---|
| 1 | `q.10jqka.com.cn/gn/detail/code/307822/`（概念名单页） | hexin-v + Referer | HTTP 200，351 个概念链接（名单端点可用） |
| 2 | `q.10jqka.com.cn/gn/detail/field/264648/order/desc/page/1/ajax/1/code/308614/`（**成分分页端点**） | akshare ths.js 现算有效 hexin-v + Referer，**单次请求**（此前仅 1 次名单页请求、间隔 8s） | **HTTP 403 `<h1>Nginx forbidden.</h1> request info: 180.164.138.29`** |
| 3 | `data.10jqka.com.cn/dataapi/limit_up/limit_up_pool`（同集团 dataapi 对照） | 无 cookie | HTTP 200 真实数据 |
| 4 | `q.10jqka.com.cn/gn/detail/code/308614/`（概念详情页对照） | 同上 | HTTP 200，**0 个股链接、无成分数据**（board-infos 仅行情统计） |

判定要点：

- 首请求即 403——**即使按最保守周频纪律执行，成分端点对本机仍不可用**；
  与 10-20 调研（3 探针同结果）相隔 5 天两次独立实测，非瞬时封禁。
- 对照端点 #3 200：封禁**非 10jqka 集团级**（#120 的 zt_pool dataapi 链路
  不受影响），是 q.10jqka.com.cn 概念成分端点**端点级**对本机的拒绝。
- 对照端点 #4 不含成分数据：成分无替代承载路径（详情页 board-infos 只有
  今开/昨收/最高/最低/成交量/涨幅/涨跌家数/净流入/成交额，无个股清单）。
- 名单页虽 200，但"有名单、无成分"无法支撑任何消费点（自算行情、命中
  家数、交叉命中全部以成分为地基）——名单单独无保留价值。

## Decision

**概念板块维度永久退役**（按 #106 弃用方案落地，不复检准入闸——除非
THS 风控策略公开变更并有新实测证据，届时作为新 ticket 重新立项）：

- **采集清单删除**：概念维度无任何抓取/落库路径。整体摘除 issue #84
  概念成分积累死链——`tools_backfill.accumulate_em_concepts`、
  `fetch_board_cons_with_fallback`（fetch_chain）、`BoardConsAdapter`
  协议（base.py，DataAdapter 合成减为七协议）、akshare/efinance/baostock
  三适配器的 `fetch_board_cons` 实现、`_board_em.map_cons_rows`、
  `repository.upsert_em_cons`、scheduler close 批的 cons 门控钩子
  （`cons_adapters` 注入缝一并移除）。
- **存量只读**：`market_boards` 概念行与成分字典 `sw_industry_class`
  source_system='em' 行保留只读，不参与任何计算/求交（读方
  query_sw_board_daily 只读 'sw' 体系，天然隔离）。
- **消费方定稿**：daily-review / limit-up-analysis 概念栏目措辞从
  "降级时删除"改为"已退役"常态说明（口径附注有据：弃用面非数据缺口，
  不设栏目、不入 data_gaps、无降级路径）；payload 契约
  `sectors.概念强度` / `top75_structure.concept_strength` 固定 null；
  领涨股/成分交叉命中无自算来源（'em' 字典停止积累且永不可补），一并
  弃用。
- **按 CONTEXT.md 弃用语义不占第四态**：永久弃用面从采集清单删除即可，
  `PLANNED_ABSENT_SECTIONS` 不登记概念（概念自 #118 起已不在
  ALL_SECTIONS，boards 段仅承载申万行业）。

## Consequences

- 复盘/涨停分析的板块维度收敛为申万行业单一异体系（#118 决策的边界
  更干净）；概念强度、前 75 概念聚类、个股概念归属、概念领涨股/交叉
  命中从所有报告与契约中消失（历史报告为弃用前产物）。
- mx-data 个股概念归属问答**不受影响**（不是板块枚举，#66 已证）——
  它仍可服务个股题材标注，但不再与任何概念板块清单求交（清单退役）。
- d.10jqka 年 K 线（免鉴权、风控温和，#55 实测）留作按需单板块历史查询
  工具箱手段，不建采集链（#106 既定保留项）。
- 残留风险：q.10jqka.com.cn 与 data.10jqka.com.cn 风控策略同源同集团——
  本次实测 dataapi 未被封（zt_pool 链路无恙），但未来 THS 若把端点级
  封禁升级为集团级，#120 的 zt_pool 低频纪律需重新评估（现状按 #120
  既有纪律运行，本票不改变其风险面）。
