# baostock 实例租约与请求预算

状态：**#173/#172 已按进程实例租约契约验收关闭；#163 正在执行正式回填**。历史验收证据保留在
[2026-11-06 验收报告](research/2026-11-06-baostock-lease-acceptance.md)，其中被新契约取代的
结论不得继续引用。决策原因见 [ADR 0019](adr/0019-baostock-explicit-lease-and-quota.md)。

官方规则：<https://www.baostock.com/blacklist>；每日 API 请求不超过 5 万次，不能并发连接访问。
单主机文件租约只协调使用同一锁目录的合作进程，不覆盖多主机、不同用户或仓库外客户端。

## 1. 核心模型

**一次只允许一个进程实例持有 baostock 租约和会话。** 租约归属
`entry=cli|mcp + PID + instance_id`，不是“同机进程都能使用的许可”。即使另一个进程属于同一
入口、能读取 token，也不能使用数据路径或正常释放该租约。

- **CLI 命令级自动租约**：首次实际抓取 baostock 时自动 acquire，同一命令复用，命令结束自动
  release。
- **MCP 实例显式租约**：agent 获得用户确认后显式 acquire，仅持有实例可用相关数据工具，持有实例
  显式 release。
- 锁冲突返回 `baostock_locked`，显示真实持有者；不重试、不静默换源。
- 正常 release 只允许持有实例。其他进程只能在确认持有进程终止后 `release --force`。

## 2. 文件与归属

| 文件 | 内容 | 权限 |
| --- | --- | --- |
| `~/.qstock-mcp/baostock.lock` | entry、PID、instance_id、owner、command、acquired_at、reason、token_hash | 0600 |
| `~/.qstock-mcp/baostock.token` | token 原文；哈希匹配之外仍须通过实例归属校验 | 0600 |
| `~/.qstock-mcp/baostock-budget.json` | 当日 login/logout/query/page/total | 0600 |

目录可用 `QSTOCK_BAOSTOCK_LOCK_DIR` 覆盖，用于隔离环境和测试。`status` 只读且不创建目录。
锁没有 TTL，不自动判断进程是否存活；随机 instance_id 用于区分租约代际，不作为存活证明。

acquire、正常 release 和 force 在同一租约操作临界区内核对代际。旧持有者收尾或旧 force 操作不得
误删后来建立的新租约。文件损坏或代际无法确认时保留现场，返回清理错误，由人工确认后 force。

## 3. CLI：数据命令自动管理

```bash
# 直接运行；首次实际需要 baostock 时自动申请，整个命令结束自动释放
qstock-scheduler adj-backfill --start 20230101 --end 20261104

# 任何进程均可只读查看
qstock-scheduler baostock-lock status

# 仅确认持有进程已终止、锁已 stale 后清理
qstock-scheduler baostock-lock release --force
```

- 首次实际进入 baostock 抓取才 acquire；不触 baostock 的命令完全不申请。
- 同一命令多次查询只 acquire 一次，复用同一 `BaostockApi` 会话；禁止每请求 acquire/release 或
  login/logout。
- 正常结束或数据异常时先关闭会话，再 finally release。进程崩溃不自动删除文件租约。
- 锁被其他实例占用时，返回 `baostock_locked`、真实持有者及处理提示；不重试、不静默换源。
- 公开 CLI 不再提供独立 `acquire` 或普通 `release`：旧三段式命令会创建无法被数据进程继承的
  实例租约，已废止。
- 数据成功但 logout/release 失败时返回非零 `baostock_cleanup_error`，保留数据结果并提示锁可能
  残留；数据本身失败时保留原错误，并附 cleanup 警告。

## 4. MCP：用户确认后显式管理

```json
{"action": "status"}
{"action": "acquire", "owner": "qstock-agent", "reason": "daily-review"}
{"action": "release"}
{"action": "release", "force": true}
```

agent 行为契约：

1. 调用 baostock 相关数据工具前检查当前 MCP 实例是否持有租约。
2. 无锁时数据工具返回 `baostock_locked`；agent 询问用户“是否尝试申请 baostock 锁？”。
3. 只有得到用户确认才调用 `baostock_lock(acquire)`；成功后才继续数据工具。
4. acquire 失败时返回真实持有者。持有进程仍活跃时等待或由持有方正常 release；确认其已终止后，
   才可经用户确认执行 force。
5. 完成后由当前持有实例 `baostock_lock(release)`，同时关闭当前 MCP 进程的会话。

MCP server 本身和非 baostock 工具不受租约限制。`init_database`、`fetch_daily`、legacy
`query_daily`、`get_fundamentals` 无锁时在整个工具入口拒绝，不连接 PG、不尝试其他数据源。
`QSTOCK_PRICE_SOURCE=new` 的 `query_daily` 仅读 PG、不触 baostock，属于明确豁免。

工具 description 与 server instructions 必须写明确认流程、实例归属和 release；实现与描述同步
更新，不得让 agent 未经用户确认自动 acquire。

## 5. 会话、预算与错误

- 一个实例租约内、一个进程一个 `BaostockApi` 会话；进程内单例与 `threading.Lock` 串行查询。
- 单请求 60s 超时；`10001001` 重登一次并重试同一请求；瞬时错误最多一次退避重试。
- `10001011` 黑名单与 `10001005` 登录数上限均致命、零自动重试；不得换账号、代理或节点绕过。
- 日预算按协议动作 login/logout/query/page 计入，Asia/Shanghai 自然日重置；45,000 警告、50,000
  拒绝新租约和新请求。
- `baostock_locked` 与 `baostock_budget_exhausted` 不重试、不静默换源。
- `baostock_cleanup_error` 表示数据命令收尾失败；它不是数据抓取成功，也不能被忽略。

## 6. 崩溃与恢复

1. 用 CLI 或 MCP `status` 查看 entry、PID、instance_id、持有时间与 token 状态。
2. 持有进程仍活跃时，不得 force；等待其结束，或由持有实例正常 release。
3. 确认持有进程已经终止后，执行 `release --force` / `baostock_lock(release, force=true)`。
4. 重新运行 CLI 数据命令，或由用户在 MCP 中确认后重新 acquire。
5. 若出现 `10001011`，先看官方统计页；解除前不得继续发 baostock 请求。

## 7. #172/#163 验收与回填状态

#172 已完成新契约验收：真实双进程互斥、同入口实例隔离、CLI finally 自动释放、
MCP 工具入口门禁与用户确认、正常 release 归属、stale force、代际保护和 cleanup 错误。原报告中的
共享租约结论只代表旧实现，不是新契约的通过证据。

#163 正式回填直接运行：

```bash
qstock-scheduler adj-backfill --start 20230101 --end 20261104
```

不再执行独立 acquire/release。期间不得与 morning/close/daily 并发；真实小样本和历史回归证据不得
外推为长期容量或全市场性能结论。
