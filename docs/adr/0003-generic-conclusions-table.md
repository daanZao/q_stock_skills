# 通用结论表：JSON payload，schema 对未来 skill 保持开放

分析结论的持久化用**一张通用结论表**：`(subject_type, subject_code, trade_date, conclusion_type)` 为业务唯一键，`payload` 为 `jsonb`。结论类型由写入方 skill 自报（如 `daily_review.close`、`sepa.stage`）。渲染给人看的 markdown 报告不进 MCP，由 skill 侧自己写文件系统，报告路径记入 payload 字段做关联。

理由：结论的生产者是未来不断增生的 skill 群，按结论类型分强类型表会让每加一种结论都要迁移 schema，skill 生态被 server 发版绑死；通用表 + JSON 让加新结论类型零 schema 变更。需要字段级过滤时用 PG 的 jsonb 操作符即可覆盖。

旧系统的 `analysis_conclusions` 表是反面参照：无唯一约束（upsert 靠代码兜底）、sequence 漂移需自愈，新表在 schema 层把唯一性钉死。

## Consequences

- 查询方要知道结论类型才能解释 payload 结构——结论类型的字段约定由各自 skill 的文档承载，server 不校验。
- 同一标的同一日期同一结论类型只允许一条结论，重复写入为 upsert 语义。
