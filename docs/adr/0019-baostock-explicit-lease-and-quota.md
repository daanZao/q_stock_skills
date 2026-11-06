# ADR 0019：baostock 实例租约、单会话与请求预算

2026-11-05，issue #163 事故复盘；2026-11-06 由 #173 修订租约归属与生命周期。
一手证据见 `docs/research/2026-11-05-baostock-session-and-quota.md`，官方规则见
<https://www.baostock.com/blacklist>。

## Context

baostock 官方黑名单页只写了一条额度规则：**每日 API 请求不超过 5 万次，并且不能并发连接访问**；
本自然年度首次被限制冻结 6 小时，同年多次按 `本年累计限制次数 × 6 小时` 增长，黑名单登录码
`10001011`。旧 `BaostockAdapter` 默认每方法调用一次 `login → query → logout`；2026-11-05
正式回填约 508 个因子日后出现 20s 无响应（`10002007`，非黑名单码）。约束首先是握手节奏与
并发，其次是日总量。

#170/#171 的文件租约只拒绝第二次 acquire，所有能读取 token 的进程都可进入数据路径。
#172 因此未通过：它不能保证不同 CLI/MCP 进程不并发。单纯在请求期间增加 OS session lock
也不足够，因为 acquire 后尚未启动数据请求时仍可被另一入口使用，且无法表达持有方 release。

## Decision

1. **租约归属具体进程实例。**
   - 租约文件 `~/.qstock-mcp/baostock.lock` 与 `baostock.token` 保持 0600；lock 记录
     `entry=cli|mcp`、PID、随机 `instance_id`、owner/command/acquired_at/reason/token_hash。
   - 数据路径与正常 release 同时校验 token 和完整实例身份。另一个进程即使属于同一入口、
     能读取 token，也不能共享租约。
   - 一次只允许一个实例租约/会话。锁冲突返回 `baostock_locked`，显示真实持有者，不重试、
     不静默换源。
2. **CLI 使用命令级自动租约。**
   - 首次实际进入 baostock 抓取时自动 acquire；同一命令后续请求只验证本实例仍持有租约，
     不重复 acquire。完全无关 baostock 的命令不申请。
   - 命令正常结束或数据异常时，先关闭 `BaostockApi` 会话，再在 finally 中正常 release。
   - 公开 `qstock-scheduler baostock-lock` 只保留 `status` 与 `release --force`；旧独立
     `acquire → 数据命令 → release` 三进程流程废止。
   - 数据成功但 logout/release 失败返回非零 `baostock_cleanup_error`，保留数据结果；数据
     已失败时保留原错误并附 cleanup 警告。
3. **MCP 使用用户确认后的实例显式租约。**
   - agent 发现无锁后先询问用户，得到确认才调用 `baostock_lock(acquire)`；工具不自动申请。
   - 相关数据工具在整个工具入口验证当前实例租约，在 PG 或其他数据源工作前返回
     `baostock_locked`。仅持有实例可正常 release，release 同时关闭当前进程会话。
   - `QSTOCK_PRICE_SOURCE=new` 的纯 PG `query_daily` 不触 baostock，豁免租约；其他非
     baostock 工具不受影响。
4. **stale 只经人工确认，代际变更不得误删。**
   - 不做 TTL 或自动进程存活判定。`status` 展示实例信息；操作者确认持有进程终止后才执行
     `release --force`。
   - acquire、正常 release、force 共用租约操作互斥，并在操作前核对预期 token/实例代际。
     文件消失或已换成另一实例时，旧 finally/force 不得删除新租约。
   - 持有进程收尾遇到损坏或代际不明的文件时返回 cleanup error、保留现场，不盲目删除。
5. **一个实例租约内、一个进程一个会话。** 进程内 `BaostockApi` 单例 + `threading.Lock`；
   同一进程所有请求复用同一 login 会话，禁止每请求 login/logout。单请求 60s 超时；超时/协议
   错误重置会话并释放请求锁，下个请求重登；正常完成只释放请求锁。所有 baostock 入口统一走
   `BaostockApi`。
6. **日请求预算。** 本地 `baostock-budget.json` 按协议动作计（login/logout/query/page），
   Asia/Shanghai 自然日重置；acquire 与新请求检查，45,000 警告、50,000 拒绝。不做 PG 账本。
7. **错误与重试。**
   - `10001001`：重登一次，重试同一请求，不得记成“无数据”。
   - `10001011`：致命，零重试、零换源、零换 IP；终止当前批次并写审计。
   - `10001005`：致命，不自动重试。
   - `10002004`/`10002007`/超时/连接重置：重置会话，瞬时错误最多重试一次。
   - 参数类/`10004xxx`/unsupported：不重试。
   - 结构化错误保留 `baostock_locked`、`baostock_budget_exhausted`、
     `baostock_blacklisted`、`baostock_login_limit`、`baostock_timeout`、
     `baostock_upstream_error`，新增 `baostock_cleanup_error` 表示命令收尾失败。
8. **契约同步与迁移。** MCP 描述、server instructions、相关 skill 必须声明用户确认和实例
   归属；#169 主 spec、#172 验收、#163 手册同步。#170/#171 的历史验收保留，但冲突口径由
   本修订取代。#172 重新验收通过前，#163 正式回填保持暂停。

## Considered Options

- **主机级许可，互斥只在 acquire**：仍允许同机数据进程共享 token，不能满足官方禁止并发和
  #169 的程序保证；否决。
- **只在活动请求期间加 OS session lock**：能阻止部分并发，但 acquire 后空闲期仍可通过，
  且不满足持有方正常 release；否决。
- **保留独立 CLI acquire → 数据命令 → release**：三个进程的实例身份不同，无法与进程级归属
  同时成立；以命令级自动生命周期替代。
- **MCP 数据工具自动 acquire**：会在用户未确认时占用上游使用权；否决。
- **用 OS flock 代替文件租约**：崩溃自动释放虽方便，但不能保留持有者信息，也不能满足人工
  确认 stale 的恢复契约；操作互斥可使用稳定文件的 flock，但不替代逻辑租约。
- **每请求 login/logout**：2026-11-05 事故直接成因；否决。

## Consequences

- CLI 使用更简单，但锁生命周期从操作者手工控制改为命令级自动管理；MCP 保留显式确认。
- 进程重启后旧实例锁不能由新实例正常 release，必须确认 stale 后 force；这是防止活跃锁被
  误释放的代价。
- 公开 CLI 锁命令和 #163 续跑流程发生不兼容变更，手册与验收必须同步，不能只改实现。
- 45k/50k 与单主机假设仍是保守策略；不同锁目录、不同用户和仓库外 baostock 客户端不属于
  同一协作锁作用域，多主机共享 IP 仍需额外协调。
