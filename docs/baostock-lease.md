# baostock 显式租约与请求预算

状态：**设计契约（ADR 0019），expand 部分落地**。CLI `qstock-scheduler baostock-lock`、
MCP `baostock_lock`、`BaostockLease` 与进程内 `BaostockApi` 已实现（#170）；数据命令/工具
**尚未强制检查租约**，`baostock_locked` 返回与全部适配器收口归 #171。当前
`BaostockAdapter` 仍是每调用一次 `login → query → logout` 的旧路径；在 #171 落地前，
acquire/release 是协作约定，不是数据门禁。

官方规则：<https://www.baostock.com/blacklist>；一手证据与配额数学：
[2026-11-05-baostock-session-and-quota.md](research/2026-11-05-baostock-session-and-quota.md)；
决策：[ADR 0019](adr/0019-baostock-explicit-lease-and-quota.md)。

## 1. 一句话

**谁要使用 baostock，谁先显式申请本地租约；一次只允许一个租约/会话；用完必须显式释放；
数据命令只检查租约，不自动申请/释放。**

## 2. 为什么是显式租约

- 官方：每日 API 请求 ≤ 5 万、**不能并发连接访问**；首次黑名单冻结 6 小时，同年按
  `本年累计限制次数 × 6 小时` 增长，黑名单码 `10001011`。
- 2026-11-05 正式回填：每请求 login/logout 跑到约 508 个因子日后，登录/查询 20s 无响应
  （`10002007`，不是 `10001011`）；45 分钟约 1,524 次协议动作只占日额度约 3%。问题首先是
  握手节奏/并发，其次才是日总量。
- 自动按批次加锁无法满足“从另一个入口主动解锁”和“锁住时明确告诉用户”；因此做成**显式
  租约**，而不是 API 内部隐式锁。

## 3. 租约文件与权限

| 文件 | 内容 | 权限 |
| --- | --- | --- |
| `~/.qstock-mcp/baostock.lock` | JSON：owner、pid、command、acquired_at、token_hash、reason | 0600 |
| `~/.qstock-mcp/baostock.token` | token 原文（数据命令自动读取） | 0600 |
| `~/.qstock-mcp/baostock-budget.json` | 当日 login/logout/query/page/total 与日期 | 0600 |

- 租约是**本地文件租约**，不是 OS `flock`；**进程退出不自动释放**，崩溃遗留由
  `release --force` 清理。
- 目录默认 `~/.qstock-mcp/`，可用 `QSTOCK_BAOSTOCK_LOCK_DIR` 覆盖（隔离环境/测试）。
- 数据命令读取 token 原文并与 lock 里的 `token_hash` 比对；不匹配或文件缺失 → 视为未租约。
- 一次只允许一个租约（单主机；单出口 IP 下等价于单 IP）。多主机共享出口 IP 时需要额外的
  跨主机协调，不在本期范围。

## 4. CLI

```bash
# 申请（只在没有租约时成功；不发 baostock 请求）
qstock-scheduler baostock-lock acquire --owner qstock-agent --reason "adj-backfill 20230101-20261104"

# 查看（可在没租约时运行；显示 owner/pid/command/acquired_at/age/token_present/token_valid/当日预算）
qstock-scheduler baostock-lock status

# 释放（从另一个终端/会话执行；token 缺失/哈希不匹配/文件损坏用 --force）
qstock-scheduler baostock-lock release
qstock-scheduler baostock-lock release --force
```

行为：

- `acquire` 时已有租约 → **失败**并打印持有者/命令/开始时间/建议的 `release` 入口；不覆盖。
- `acquire` 时当日预算 ≥ 50,000 → 拒绝；≥ 45,000 → 成功但打印警告。
- `release` 删除 token/lock，并关闭当前进程已有的 `BaostockApi` 会话（可能发送 logout）；
  重新 acquire 后的首次查询必须重新 login。token 缺失/哈希不匹配/文件损坏时需 `--force`。
  `--force` 不校验 owner（owner 仅作展示，不是权限边界），用于崩溃遗留或无法读取 token 的场景。
  没有活动会话时，acquire/status/release 均只操作本地文件，不发 baostock 请求。
- `status` 只读，不申请、不释放、不登录 baostock；显示 `token_present`/`token_valid`。
- `--owner`/`--reason` 只用于 `acquire`，`--force` 只用于 `release`；其他组合明确报错，
  不静默忽略。

## 5. MCP

新增 `baostock_lock` 工具（**与 CLI 共用同一组文件**）：

```json
{"action": "acquire", "owner": "qstock-agent", "reason": "daily-review"}
{"action": "status"}
{"action": "release", "force": false}
```

- 所有 baostock 数据类 MCP 工具（`query_daily`、`fetch_daily`、`get_fundamentals` 等）在
  无租约时返回结构化错误：

```json
{"status":"error","error_kind":"baostock_locked",
 "lock":{"owner":"...","command":"...","acquired_at":"..."},
 "note":"先调用 baostock_lock(acquire)；锁住时请从另一个 CLI/MCP 会话 release"}
```

- 数据工具**不会**自动 acquire/release；agent 必须显式 `baostock_lock`。
- 租约被另一个 owner 占用时，agent 把 `lock` 信息转告用户；用户去另一个 terminal/会话
  `baostock-lock release`（或 `baostock_lock release --force`）后，再 acquire。

### MCP 描述契约（`baostock_lock` 已落入 server.py；数据工具描述随 #171）

- `baostock_lock` description：
  > 申请/释放/查询 baostock 显式租约。baostock 数据工具必须在本租约下运行；租约不会自动
  > 释放；已锁定时返回持有者信息，需从另一个 CLI/MCP 会话 release。
- 所有 baostock 数据工具 description 增补：
  > baostock 路径受显式租约保护：先调用 baostock_lock(acquire)，完成后
  > baostock_lock(release)；无租约返回 baostock_locked。
- MCP server instructions / 相关 skill 文档同步声明“先申请租约、无租约返回
  `baostock_locked`、需显式 release”。实现与描述在同一提交更新，不得先声明不存在的工具。

## 6. 一个租约内怎么用

- **一个进程一个会话**：进程内 `BaostockApi` 单例 + `threading.Lock`；同一进程内所有请求复用
  一次 `login`，串行；**禁止每请求 login/logout**。
- 不同 CLI 进程（例如先跑 `daily --all`，再跑 `fundamentals`）各自建立一次会话；租约保证
  它们不并发，不承诺跨进程共享同一个 socket。
- MCP server 是常驻进程：在同一个租约下可跨工具调用复用进程内会话；租约释放时关闭。
- 单请求超时 **60s**；超时/协议错误重置会话并释放请求锁，下个请求重登；正常完成只释放
  请求锁，会话保留到租约释放。
- 所有入口统一走 `BaostockApi`：`fetch_daily`、`fetch_astock_daily`、
  `fetch_daily_adjust_factor`、`fetch_stock_list`、`fetch_index_daily`、
  `fetch_fundamentals`；删除 `persistent` 开关和每请求 `_with_relogin`。

## 7. 日预算

- 计数单位：**协议动作** = login、logout、query、每次翻页（page）。这是保守口径；官方未
  明确 login/logout 是否计入 5 万，按计入处理。
- 计数落点：`BaostockApi` / 会话边界；`fetch_chain`/调用方不重复计数。每次协议动作即时
  原子写入 `~/.qstock-mcp/baostock-budget.json`（保证 50k 门禁跨进程可见），当日计数按
  Asia/Shanghai 日期键归零。
- 自然日：Asia/Shanghai 00:00 重置。
- **45,000 警告；50,000 停止**新租约/新请求，返回 `baostock_budget_exhausted`。
- 大回填不得与 morning/close/daily 同一天/同一时段并发；先估算请求数（如 #163 正式回填
  约 5,000 次查询，单会话下安全；`daily --all` 最坏情形才压线）。

## 8. 错误与重试

| 错误 | 处理 |
| --- | --- |
| `baostock_locked` | 不重试；提示先 acquire 或从另一入口 release |
| `baostock_budget_exhausted` | 不重试；停止批次，等次日或人工处理 |
| `10001011` 黑名单 | **致命、零重试、零换源、零换 IP**；终止当前批次并审计；人工看官方统计页释放 |
| `10001005` 账号登录数达上限 | 致命，不自动重试；提示等待其他会话释放 |
| `10001001` 用户未登录/会话被踢 | 重登一次，重试**同一请求**；不得记成“无数据” |
| `10002004`/`10002007`/超时/连接重置 | 重置会话；瞬时错误最多重试一次，指数退避 |
| 参数类/`10004xxx`/unsupported | 不重试 |

结构化错误种类与 spec #169 一致：`baostock_locked`、`baostock_budget_exhausted`、
`baostock_blacklisted`、`baostock_login_limit`、`baostock_timeout`、
`baostock_upstream_error`；它们是 baostock 专属错误，到 fetch 边界再映射到抓取三态
（ADR 0007），不与之混用。

## 9. 崩溃、stale 与排查

```bash
qstock-scheduler baostock-lock status
# owner/pid/command/acquired_at/age/token_present/token_valid/预算不合适时：
qstock-scheduler baostock-lock release --force
qstock-scheduler baostock-lock acquire --owner ... --reason ...
```

- 进程崩溃后 lock/token 会残留；这是“显式解锁”的预期代价，`status` 看 age，`--force` 清理。
- `baostock.token` 与 `baostock.lock` 的 `token_hash` 不匹配：视为陈旧/损坏，`release --force`
  后重新 acquire。
- `10001011`：先看 <https://www.baostock.com/blacklist> 的 `今年被限制次数`/`待释放时间`；
  释放前不发任何 baostock 请求。不要换账号/代理/节点绕过。
- 当日预算接近 45k：先跑 `status`；优先保留 morning/close，暂停大回填。

## 10. 与 #163 正式回填的关系

正式回填在租约/单会话/预算/错误分类落地并用小样本（≤50 请求）验证前保持暂停
（当前断点 510/928 因子日、raw 0 行）。续跑流程：

```bash
qstock-scheduler baostock-lock acquire --owner qstock-agent --reason "adj-backfill 20230101-20261104"
qstock-scheduler adj-backfill --start 20230101 --end 20261104
qstock-scheduler baostock-lock release
```

期间不得与 morning/close/daily 并发；续跑前先用官方统计端点确认
`今年被限制次数=0`、`待释放时间` 为空。

## 11. 实现验收清单（#170 expand 状态，2026-11-05）

- [x] `baostock.lock` / `baostock.token` / `baostock-budget.json` 的 acquire/status/release/force
- [x] 无 TTL、进程退出不自动释放；token 缺失/哈希不匹配视为未租约
- [x] `BaostockApi` 进程内单例 + 会话复用；60s 超时（主线程 SIGALRM 外部 deadline；
      MCP worker 线程用 login 前配置的 socket 超时，仅覆盖 socket 阻塞）、
      `10001001` 重登一次、瞬时错误一次退避
- [x] 45k/50k 预算计数、45k 警告、50k 拒绝新请求/新租约（`baostock_budget_exhausted`）；
      翻页即时计数，达到 50k 后不再发下一页
- [ ] 静默翻页截断（baostock 0.9.3 空 recv 不置 error_code）的页级校验（#171）
- [x] CLI `baostock-lock` 与 MCP `baostock_lock` 共用文件；无活动会话时零 baostock 请求，
      release 关闭本进程已有会话，重新 acquire 后重新 login
- [x] MCP `baostock_lock` 描述与 server instructions 已声明租约/不自动释放
- [ ] 数据命令/工具无租约返回 `baostock_locked`，不自动申请/释放（#171）
- [ ] 所有 baostock 数据请求经 `BaostockApi`；删除 `persistent` 与每请求 `_with_relogin`（#171）
- [ ] 数据工具 description / 相关 skill 文档声明租约（#171）
- [ ] 小样本真实测试（≤50 请求）验证长会话、锁、预算（#172）
- [x] 更新 `docs/adj-backfill-cli.md` 的租约状态与 `docs/knowledge-graph.md`
