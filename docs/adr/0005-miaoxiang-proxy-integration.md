# 妙想 skills 接入：proxy 透传 + 验证结论落 conclusions，不进 fallback 链

东方财富妙想 skills 分两条路径接入 MCP server。**mx-data（金融数据）**以 proxy 能力面的透传工具接入：不规格化、不落库、不进 fetch fallback 链；定位为"三源选股后的交互式验证源"，验证结论经 conclusions 表沉淀（如 `conclusion_type="sepa.mx_verified"`，payload 摘录关键指标，符合 ADR 0003）。**mx-search（资讯搜索）**走"抓取 + 落库 + 查询"：结果落独立的资讯表（标注 `subject_type`/`subject_code` 标准代码、资讯时间与抓取时间，业务键幂等 upsert），分析时按标准代码查近期资讯；落库依据是实测确认正文为原文截断、LLM 加工仅在查询侧（issue #20）。mx-zixuan / mx-moni 属账号操作类，超出数据服务定位，不接入。

理由：mx-data 实测（2026-09，issue #19）数值与三源精确一致、NL 问句确定性可接受，但它是自然语言查数网关——无结构化参数、有每日配额、未披露期间静默回退旧值不报错——只能交互式抽查，不能批量对账，进 fallback 链会破坏抓取确定性。mx-search 补足三源空白（新闻/公告/研报），且资讯有沉淀复用价值（复盘与选股反复引用近期资讯），故落库；初版"透传不落库"的边界经实测风险重估后由本段取代。

## Consequences

- **问句口径对齐**：mx-data 输出由问句驱动；调用方组装问句时按库内字段口径（报告期、单季/累计、指标名）构造，验证语义才与库内数据可比。
- **披露期校验是硬约束**：查未披露期间会静默返回上一期旧值；调用前必须自行判断该期是否应已披露。
- **配额自行记账**：响应无配额用量字段；按妙想各 skill 分别记录请求次数，code=113 即触顶。
- **解析防御**：mx-data 响应有两种变体（指标类数字编码键 / 分红类中文列名），解析器不能写死；按 `secuCode` 过滤港股（02338.HK 会混入）；文档宣称的 `secuList` 实测不存在，不可依赖。mx-search 结果需去重、`source`/`jumpUrl` 等字段需兜底（研报无原文链接）。
- proxy 词汇扩展：CONTEXT.md 中 proxy 面"首期封装基本面数据"的表述随实施更新为"基本面 + 妙想网关"。
- 实施（新工具进 `tools_*` 与 `server.py`、资讯表新 DDL 与 repository 业务键 upsert、payload 字段约定进各 skill 文档）另起会话，按 `docs/knowledge-graph.md` 的入口指引执行；实测资产在 `.scratch/mx-skills/`（调研与对比报告、原始响应、可复跑脚本，可作测试 fixture 模板）。
