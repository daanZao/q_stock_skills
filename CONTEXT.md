# q_stock_skills

面向通用 AI agent 的证券数据与分析能力项目的领域语言。本文件只是词汇表，不记录实现细节与设计决策。

## Language

### 项目定位

**MCP 数据服务**:
本项目的核心交付物：一个 MCP server，作为旧 B/S 分析系统 Server 端的替代品，直接向 AI agent 提供数据与分析结论持久化能力。由五个能力面组成（见下）。
_Avoid_: 后端、B/S 服务

### MCP 数据服务的五个能力面

**抓取（fetch）**:
执行 web 数据源接口获取证券数据，多源 fallback（efinance → akshare → baostock）。
_Avoid_: 爬虫

**规格化落库（ingest）**:
对基础证券数据做清洗/规格化，写入本地数据库。

**原始数据封装（proxy）**:
对 web 接口提供的非标准化数据做透传封装，不规格化、不落库。首期封装基本面数据（财务指标、估值等），供方法论 skill（如 SEPA 四要素）使用。
_Avoid_: 落库

**统一查询（query）**:
基于库内数据的统一数据获取接口。

**结论存储（conclusions）**:
分析结论的读写接口。结论以 JSON 保存在一张通用结论表中，结论类型由写入方 skill 自报。

### init 能力面词汇

**轻量初始化**:
`init_database` 默认执行的初始化路径：幂等建表 + 股票清单 + 全市场快照 + 主要指数日线，各部分独立成败报告，重复调用幂等。
_Avoid_: 全量初始化

**全量回溯**:
由 `backfill_history=True` 显式开启的全市场个股历史日线补全（数千只 × 全历史），复用日线头尾自愈，单股失败不中断；绝不默认执行。
_Avoid_: 回填、补数据（不加限定时）

### Skill 家族（继承自旧项目词汇）

**Agent skill**:
独立的、自包含的 agent 能力包（SKILL.md + 可选脚本），由外部 AI agent 在会话中加载使用。本项目交付的 skill 均属此类，供通用 agent 使用，不绑定特定仓库。
_Avoid_: skill（不加限定时）

**协作 skill**:
本仓库 `.agents/skills/` 下的纯 Markdown 流程指引，仅服务于本仓库的开发协作过程。
_Avoid_: agent skill

**工具函数 skill**:
基础证券计算能力的 agent skill，内含 Python 脚本。指标计算（MA/MACD/BOLL/RSI 等）与序列数学工具（一阶/二阶导数）。经管道组合消费统一查询吐出的 JSON（stdin 进 stdout 出），不直接访问数据库；现算现用，结果不物化。
_Avoid_: 指标库

**方法论 skill**:
表达一套完整分析/选股方法（如 SEPA）的 agent skill，以判断逻辑与框架指引为主体，调用工具函数 skill 获取计算结果。
_Avoid_: 策略 skill、分析框架

### 分析工作流

**每日复盘**:
收盘后（午市或全天）基于当日盘面数据做的市场级总结，结论经结论存储持久化。由用户手动触发 agent 执行，不依赖常驻调度。
_Avoid_: 大盘复盘
