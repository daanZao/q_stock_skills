# PostgreSQL 独立新库，放弃 SQLite 便携性

数据库选型定了 PostgreSQL，放弃对"通用 agent 用户零安装"更友好的 SQLite。理由：当前第一阶段是为自己服务，本机 PG 环境已就绪，写路径（落库 + 结论存储）性能更好。`PG_DSN` 环境变量配置保留未来切换的口子。

同时决定：新项目**新建独立数据库**，不共用旧项目的 `appdb`。旧 B/S 系统仍在运行，共用库意味着每次 schema 演进都要双边协调。schema 从旧 `SKILLProject/skills/stock-data/sql/` 及盘点过的 appdb 实际表结构继承起步，DDL 拥有权收归本项目。

**不带**旧库的 `posts`/`images`/`recommended_stocks`/`post_analysis_relations` 集群——那是旧 B/S 产品的股吧功能残留，没有任何 agent skill 使用。旧库 `zt_pool`/`strong_stocks` 的历史 `amount` 列数据损坏（等于 `volume`），不做历史数据迁移；历史日线靠查询自愈按需回补。

## Considered Options

- SQLite 默认：零安装、对通用 agent 分发最友好，但写并发和性能不如 PG，且第一阶段用户就是自己——被否，但 DSN 抽象保留日后重开的可能。
- 共用 `appdb`：免迁移、旧数据直接可用，但被旧系统的写路径拖住——被否。
