# MCP server 的能力边界：五个数据能力面，指标计算留在 skill 侧

本项目的 MCP 数据服务是旧 B/S 分析系统 Server 端的替代品，能力边界定为五个数据面：init（建库初始化）、fetch/ingest（抓取并规格化落库）、proxy（基本面数据透传封装，不落库）、query（库内数据统一查询，日线缺数据自动补抓的自愈契约）、conclusions（分析结论读写）。

**指标计算（MA/MACD/BOLL/RSI 等）不在 MCP 边界内**，而是留在 agent skill 侧：skill 内嵌 Python 脚本经管道组合消费 query 工具吐出的 JSON（stdin 进 stdout 出），现算现用，结果不物化、不写回数据库。

理由：指标是库内日线数据的纯函数，物化只会引入一致性问题；数据接口的变化频率远低于指标清单，把指标放在 skill 侧让 MCP server 保持稳定，指标演进不拖累 server 发版；管道组合让 skill 不直接依赖数据库连接，任何能跑 Python 的通用 agent 环境都可用。

## Consequences

- 新增指标不需要改 MCP server，只需要改/新增 skill。
- skill 脚本的输入契约是 MCP query 工具的输出 JSON 结构，该输出结构因此成为跨仓库的稳定契约，变更需谨慎。
- 调用方需保证查询的日线长度满足指标收敛要求（如 MACD 建议 ≥200 根）。
