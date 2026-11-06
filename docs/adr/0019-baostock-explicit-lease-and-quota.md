# ADR 0019：baostock 显式租约、单会话与请求预算

2026-11-05，issue #163 事故复盘；一手证据见
`docs/research/2026-11-05-baostock-session-and-quota.md`，官方规则见
<https://www.baostock.com/blacklist>。

## Context

baostock 官方黑名单页只写了一条额度规则：**每日 API 请求不超过 5 万次，并且不能并发连接访问**；
本自然年度首次被限制冻结 6 小时，同年多次按 `本年累计限制次数 × 6 小时` 增长，黑名单登录码
`10001011`。当前 `BaostockAdapter` 默认 `persistent=False`，每个方法调用都是一次
`login → query → logout`；2026-11-05 正式回填跑到约 508 个因子日后，登录/查询退化到 20s
无响应（`10002007`，非黑名单码），而 45 分钟约 1,524 次协议动作只占日额度的约 3%。约束首先
是**握手节奏与并发**，其次是日总量。

## Decision

1. **显式租约，不自动申请/释放。**
   - CLI：`qstock-scheduler baostock-lock acquire|release|status`。
   - MCP：`baostock_lock` 工具，`action=acquire|release|status`。
   - 租约文件 `~/.qstock-mcp/baostock.lock`（JSON：owner/pid/command/acquired_at/
     token_hash/reason）与 `~/.qstock-mcp/baostock.token`（0600，token 原文）。
   - 数据命令/工具**只检查租约**，不自动申请、不自动释放；没有租约返回
     `baostock_locked`（含持有者/命令/时间与“去哪个入口 release”的提示）。
   - 进程退出**不自动释放**；释放必须显式执行，`release --force` 处理崩溃遗留的 stale 锁；
     不做 TTL 自动过期。一次只允许一个租约（单主机；单出口 IP 下等价于单 IP）。
2. **一个租约内、一个进程一个会话。** 进程内 `BaostockApi` 单例 + `threading.Lock`；同一进程
   的所有请求复用同一 `login` 会话，禁止每请求 login/logout。单请求 60s 超时；超时/协议错误
   重置会话并释放请求锁，下个请求重登；正常完成只释放请求锁。MCP 常驻进程内会话保留到租约
   释放；CLI 进程退出即关闭会话（租约仍保留，需显式释放）。不同 CLI 进程各登录一次，但租约
   保证它们不并发。
   - 所有 baostock 入口统一走 `BaostockApi`：`fetch_daily`、`fetch_astock_daily`、
     `fetch_daily_adjust_factor`、`fetch_stock_list`、`fetch_index_daily`、`fetch_fundamentals`。
   - 删除调用方 `persistent` 开关和每请求 `_with_relogin`；`BaostockAdapter` 只保留字段映射。
3. **日请求预算。** 本地 `~/.qstock-mcp/baostock-budget.json`，按协议动作计
   （login/logout/query/page；翻页按每页计入），Asia/Shanghai 自然日重置；`acquire` 时检查，
   **45,000 警告、50,000 拒绝新租约/新请求**。不做 PG 账本（proxy 路径可能无 DB；额度本身
   按主机/IP）。
4. **错误与重试。**
   - `10001001`：重登一次，重试**同一请求**，不得记成“无数据”。
   - `10001011`：致命，零重试、零换源、零换 IP；终止当前批次并写审计；由人工按官方统计页
     确认释放。
   - `10001005`（账号登录数达上限）：致命，不自动重试，提示等待其他会话释放。
   - `10002004`/`10002007`/超时/连接重置：重置会话，瞬时错误最多重试一次（指数退避）。
   - 参数类/`10004xxx`/unsupported：不重试。
   - 结构化错误种类：`baostock_locked`、`baostock_budget_exhausted`、
     `baostock_blacklisted`、`baostock_login_limit`、`baostock_timeout`、
     `baostock_upstream_error`；这些是 baostock 专属错误，到 fetch 边界再映射到
     抓取三态（ADR 0007），不与之混用。
5. **MCP 契约与描述。** 所有数据类 MCP 工具在无租约时返回结构化 `baostock_locked`，
   并在 description 中声明“先通过 `baostock_lock` 申请租约”；`baostock_lock` 的 description
   明确 acquire/release/status、锁住时的处理与不自动释放；MCP server instructions 与相关
   skill 文档同步声明。实际 `server.py` 描述在实现 `baostock_lock` 的同一提交更新（先声明一个
   不存在的工具会误导客户端）。
6. **迁移与恢复。** 先实现租约/单会话/预算/错误分类，再用小样本（≤50 请求）验证长会话与
   登录速率；#163 正式回填在此之前保持暂停（510/928、raw 0 行）。续跑流程：`baostock-lock
   acquire` → `adj-backfill` → `baostock-lock release`；期间不得与 morning/close/daily
   并发。

## Considered Options

- **API 内部自动按批次加锁**（上一版设计）：无法从另一个入口显式解锁，进程崩溃/长批次时
  不可控；否决。
- **OS `flock` 自动释放**：崩溃即释放，但用户要求“主动申请/主动解锁”，且无法留下持有者
  owner/command 供排查；否决。
- **MCP 进程级会话跨工具调用**：会留下 idle 登录连接，和另一个进程的活跃连接构成并发，
  也可能占用 `10001005` 的账号会话额度；否决。
- **PG advisory lock**：单主机场景过重；文件租约已足够，多主机共享出口 IP 时再评估。
- **每请求 login/logout**：2026-11-05 事故直接成因；否决。

## Consequences

- 操作者/agent 必须显式 acquire/release；锁住时数据命令/工具直接失败，不再静默排队。
- 崩溃会留下 stale 锁，需要 `status` 查看、`release --force` 清理；这是显式租约的代价。
- 45k/50k 与单主机假设是保守策略，不是官方验证过的并发/IP 归属结论；多主机共享 IP 时
  需要额外的跨主机协调。
- `docs/adj-backfill-cli.md`、`AGENTS.md`、`CONTEXT.md`、`docs/knowledge-graph.md` 与
  MCP 描述必须同步声明本机制；新增长批任务先申请租约。
