# baostock 显式租约、单会话与 CLI/MCP 互锁验收报告（issue #172）

2026-11-06（周五，Asia/Shanghai）。范围：spec #169、ADR 0019、#170（expand）与
#171（migrate）的**验收**；#163 正式回填的解除条件。**本 ticket 只做验收，不改实现代码**：
本轮提交是本文档与 `docs/baostock-lease.md`、`docs/knowledge-graph.md`、
`docs/adj-backfill-cli.md` 的验收状态/边界说明。

**验收结论：未通过（1 项阻断）**。阻断项是 AC6 字面「CLI acquire 后 MCP 数据工具被拒」：
实测 `BaostockLease.require()` 只校验租约存在 + token 哈希，不校验 owner/pid/会话归属，
CLI 持锁时 MCP 数据工具会共享该租约并进入 `BaostockApi`。这与 ADR 0019「租约保证不同
CLI 进程不并发」的表述冲突，需要先决策「改实现（会话级互斥）」还是「改契约（主机级许可、
互斥只在 acquire）」；决策/修复见 **#173**。本 ticket 保持 open，**#163 正式回填继续暂停**。

执行环境：分支 `spec/158_new-price-adj`，基线 commit `4d11d8a`；Python 3.12.13；
`mcp` 2.0.0；`baostock` 0.9.3；macOS；租约目录默认 `~/.qstock-mcp/`，离线验收全部使用
临时 `QSTOCK_BAOSTOCK_LOCK_DIR`。原始运行产物（harness/results/真实样本/官方统计核对）留存在
本地 `files/issue-172-acceptance/`（`files/` 按仓库约定不入库；关键命令见 §10）。

## 0. 执行摘要

| # | 验收项 | 结果 | 主要证据 |
| --- | --- | --- | --- |
| 1 | 单例与单会话 | **通过** | 同进程 `BaostockApi.get()` 同一实例；3 次请求 login=1、query=3、close 后 logout=1（D）；真实样本 login=1/query=3/logout=1（§6） |
| 2 | 租约 acquire/status/release/force | **通过** | 文件/token/哈希/0600、重复 acquire 返回持有者、release 后可再 acquire、token 缺失/不匹配、损坏锁、孤儿 token、`--force`（A1-A16） |
| 3 | 上锁/解锁语义 | **通过（附解释）** | 无租约数据命令/工具 `baostock_locked`；有租约适配器路径可用且复用一次 login；进程退出不释放；token/损坏锁 stale 用 `--force`（A/B/E） |
| 4 | CLI 申请/解锁 | **通过** | 真实 `.venv/bin/qstock-scheduler baostock-lock` 子进程；退出码 0/1/2、结构化 JSON、参数专用校验、零 baostock 请求（A） |
| 5 | MCP 申请/解锁 | **通过** | 真实 MCP stdio server（19 tools）；`baostock_lock` acquire/status/release；与 CLI 共用文件；数据工具描述/server instructions 含租约声明（C） |
| 6 | CLI↔MCP 互锁 | **未通过（阻断）** | 再次 acquire 被拒成立（C9/C13）；但字面「CLI acquire 后 MCP 数据工具被拒」不成立——数据工具共享租约进入 `BaostockApi`（§7.1）。93 checks 未覆盖本条 |
| 7 | 不测黑名单路径 | **遵守** | 未构造/触发 `10001011`；真实样本 5 个协议动作，未遇到黑名单/登录上限 |
| 8 | 不改实现、全量回归 | **通过** | 无实现代码改动；定向 59 passed；全量 950 passed（233.34s）（§8） |
| 9 | 更新 #163 与 blocking | **完成** | #163 已收到“未通过/继续阻塞”的更正；#172 保持 open 且新增 native `blocked_by #173`；#163 `blocked_by` 仍为 open #172 |

**结论**：机制的大部分按 ADR 0019 工作，但 AC6 暴露一个未决的契约/实现缺口；在 #173 决策
并重新验收前，#172 不能关闭，#163 不能续跑。真实小样本只证明“一次登录 + 串行复用”可用，
不构成长期容量或全市场结论。

## 1. 方法与证据分层

| 层 | 做法 | 范围 |
| --- | --- | --- |
| 黑盒 CLI | 真实 `.venv/bin/qstock-scheduler` 子进程，临时 `QSTOCK_BAOSTOCK_LOCK_DIR`，解析 stdout JSON/退出码 | 租约全生命周期、并发 acquire、预算门禁、CLI 数据门禁 |
| 黑盒 MCP | 真实 `.venv/bin/qstock-mcp` stdio server + `mcp.ClientSession`，同一临时租约目录 | tool list/描述/instructions、acquire/status/release、CLI↔MCP 再次 acquire 互锁、数据门禁 |
| 适配器接缝 | 真实 `BaostockAdapter` + 真实 `BaostockLease` + fake baostock client（唯一被替换的系统边界） | 有/无租约的数据路径、一次 login、logout |
| 真实上游 | 真实 default 租约目录；官方统计核对后 acquire → 1 次 login + 3 次串行查询 → close → release | 单会话与真实行数据（5 个协议动作） |
| 回归 | 定向 baostock 测试 + 仓库全量 pytest | 不触网、fake 上游；记录数量/耗时 |

黑盒 harness 共 **93 项检查，0 失败**；覆盖 CLI 场景 A、CLI 数据门禁 B、MCP 场景 C、
单例 D、适配器租约门禁 E、文档声明 F。注意：harness 的 C7-C15 只验证“再次 acquire 被拒”，
C17 验证“无租约数据工具被拒”；**没有**验证 AC6 字面的“CLI 持锁时 MCP 数据工具被拒”，
因为实测该行为不发生。AC6 的证据是 §7.1 的独立探针。

数据门禁的确定性做法：给 CLI/MCP 子进程设置不可达的 `HTTP(S)_PROXY`，使 akshare 段快速
失败，真实 baostock 适配器在 `require()` 处返回 `baostock_locked`；随后核对预算文件 total=0，
确认没有协议动作、没有 login。

## 2. 单例与会话复用

- 同一进程内 `BaostockApi.get()` 连续两次返回**同一实例**（`a is b`）。
- 同一实例连续 3 次 `fetch_stock_list()`：`login_calls=1`、`query_calls=3`、
  `logout_calls=0`；显式 `close()` 后 `logout_calls=1`，重复 `close()` 幂等。
- 预算文件同步记 `login=1, query=3, logout=1`（总 5），证明计数落在会话边界，不在每个
  调用方重复计数。
- 真实上游样本（§6）同样为 `login=1, query=3, logout=1`；3 次查询之间无重登，
  未出现 `10001001`。
- `BaostockApi` 是**进程内**单例：不同 CLI 进程各自建立一次会话；跨进程不复用 socket。

## 3. 租约文件与生命周期

全部经真实 CLI 子进程执行，临时目录内观察文件：

| 检查 | 观察 |
| --- | --- |
| 无租约 status | 退出 0，`leased=false`；不创建目录/文件 |
| acquire | 退出 0；`baostock.lock`/`baostock.token` 均 0600；lock 只存 `token_hash`，不存明文；token 的 SHA-256 与 lock 一致；记录 owner/pid/command/acquired_at/reason |
| 第二次 acquire | 退出 1，`error_kind=baostock_locked`，含原持有者 owner/command/time 与 release 提示；原锁不被覆盖 |
| status | 退出 0，显示持有者、command、`age_seconds`、`token_present`、`token_valid`、当日预算 |
| release | 退出 0，删除 token/lock；`status` 变 `leased=false`；随后可再次 acquire |
| 进程退出 | acquire 子进程退出后，父进程/新 CLI 仍看到 `leased=true`；**不自动释放** |
| token 哈希不匹配 | `leased=false`、`token_valid=false`，持有者信息仍可见；`require()` 拒绝；普通 release 退出 1；`release --force` 退出 0 并清理 |
| token 缺失 | `leased=false`、`token_present=false`；普通 release 退出 1；`--force` 清理 |
| 损坏的 lock JSON | `leased=false` 且 `lock_error` 可见；`--force` 清理后重新 acquire |
| 孤儿 token（无 lock） | `leased=false`、`token_present=true`；release 删 token、`released=false`、`token_removed=true` |
| `release --force` 健康租约 | 成功；`forced=true` |
| 并发 acquire | 4 轮双进程同时 acquire，每轮恰好 1 个退出 0、1 个 `baostock_locked`；失败方在极窄窗口内可能只看到错误而没有 `lock` 详情（赢家尚未写完 JSON），随后 `status` 可见完整持有者 |
| 参数专用性 | `status --owner` / `status --force` → `invalid_argument`；非法 action → argparse 退出 2 |
| 预算门禁 | 45,000 → acquire 成功并带 45,000 警告；50,000 → `baostock_budget_exhausted` 且不建锁 |
| 零 baostock 请求 | 连续 acquire/status/release 后 `baostock-budget.json` 不存在；未创建 client |

**stale 的边界**：token 缺失/不匹配/损坏锁已覆盖；进程退出留下的是“有效锁”，不是可自动
识别的 stale——无 PID 存活检查，死进程的有效锁与活跃锁不可区分（无 TTL 设计）。`--force`
对健康锁与损坏锁均验证；没有单独的“死进程 + 有效锁”自动判定路径，这是设计现状，不是本次
验收发现的新缺陷。

## 4. CLI 数据门禁

`qstock-scheduler fundamentals --codes 600519`（akshare 段经不可达代理快速失败，随后进入
baostock 适配器）：

- 无租约：退出 1，JSON `status=error`、`error_kind=baostock_locked`，note 指向
  `baostock_lock(acquire)` / 另一入口 release；预算 total=0，未创建 lock。
- 有租约：真实 `BaostockAdapter` + fake client 两次调用只 login 一次；行映射保持原结构
  （`{"stock_code":"600519","stock_name":"贵州茅台"}`）；无租约时在 login 之前即抛出
  `baostock_locked`（fake client `login_calls=0`）。

这两项证明“数据命令只检查租约、不自动申请/释放”，且门禁在适配器/上游之前生效。

## 5. MCP 契约

真实 stdio server：19 个 tool，含 `baostock_lock`。`initialize.instructions` 与
`baostock_lock` description 均声明“显式租约、不会自动释放、release、`baostock_locked`”；
四个可能进入 baostock 的数据工具（`init_database`、`fetch_daily`、`query_daily`、
`get_fundamentals`）description 均含 `baostock_lock` 与 `baostock_locked`。四份 skill 文档、
`AGENTS.md`、`CONTEXT.md` 均含同样声明。

- `baostock_lock(status)` 在无租约时返回 `leased=false`，不创建文件。
- `baostock_lock(acquire)` 创建 owner=`qstock-mcp`、command=`mcp:baostock_lock` 的锁；
  CLI `status` 立即看到同一持有者。
- 第二次 MCP acquire 被拒并返回原持有者；CLI 在 MCP 持有期间 acquire 同样被拒。
- MCP release 后 CLI 可 acquire；CLI 持有期间 MCP 可 status/read 并 release（owner 仅展示，
  不是权限边界）；反向（CLI acquire → MCP release → MCP acquire）同样通过。
- 非法 action 返回结构化 `invalid_argument`，不抛协议错误。
- 无租约调用 `get_fundamentals`：返回 `baostock_locked`，预算 total=0，未创建锁/未 login。
- `release` 关闭本进程已有 `BaostockApi` 会话（logout 一次）由既有集成测试覆盖，本轮
  定向回归通过（`test_server_baostock_lock.py::test_mcp_release_closes_current_api_session`、
  `test_scheduler_baostock_lock.py::test_release_closes_active_api_session`）。

**未通过项**：AC6 字面要求“CLI acquire 后 MCP 数据工具被拒”；实际共享租约的数据工具
不会被拒。见 §7.1。

## 6. 真实小样本（5 个协议动作）

**时间与顺序（更正）**：

- 样本前（约 13:45）首次查询官方统计端点，只取到第 1 页 100 行（分页参数错误，实际未翻页），本机公网
  IPv4 `39.144.46.101` 不在该页。这是样本前的**初步**查询，不是全量核对。
- 13:46 执行真实样本。
- 约 13:49 以 `pageSize=1000` 取回全部 622 行，本机 IP **无记录**。这是样本后的**全量**核对。

因此不能写成“样本前完成全量前置核对”。两次查询均无本机记录，但官方统计可能滞后（官方
文案要求等待 5 分钟）；该证据只说明“当前无黑名单记录”，不外推长期可用性。本轮未测试
`10001011` 路径。

**执行**：default 目录 acquire（owner `qstock-agent`，reason `issue-172 small real sample`）
→ 真实 `BaostockAdapter` 串行 3 次查询（相邻 2s）：

| 查询 | 结果 |
| --- | --- |
| `fetch_daily("600519", "20261102", "20261104", adj="none")` | 3 行，首日 20261102 close 1439.1，末日 20261104 close 1446.44 |
| `fetch_daily_adjust_factor("20261104")` | 6 个事件；样本 `001376/B=1.046937`、`002241/B=24.264689`、`002669/B=2.184425` |
| `fetch_index_daily("000300", "20261102", "20261104")` | 3 行，末日 20261104 close 4101.335 |

`BaostockApi.close()` → CLI `release`。预算从 `0/0/0/0` 变为 **login=1, logout=1,
query=3, page=0, total=5**；总耗时约 5.67s；无 `10001001`、无 `10001011`、无 `10001005`。
样本只覆盖 3 个查询、1 个会话、5 个协议动作；不能据此宣称长期 TPS、并发上限、“长会话”
或全市场容量，正式回填仍须在 #173 解决后按 ADR 0019 的串行/单会话和
acquire→release 流程执行。

## 7. 发现与判定

### 7.1 阻断项：CLI 持锁时 MCP 数据工具未被拒（AC6）

**现象**：CLI `baostock-lock acquire` 后，MCP `get_fundamentals` 等数据工具通过
`BaostockLease.require()`，进入 `BaostockApi`；它没有返回 `baostock_locked`。无租约时同一
调用才返回 `baostock_locked`。

**证据**：CLI 持锁、把 `_load_baostock` instrument 成立即抛 `BaostockUpstreamError`（保证
不发上游请求）后调用
`get_fundamentals("600519", adapters=[BaostockAdapter()])`：

```python
# 探针（CLI 已 acquire；instrumented _load_baostock 在 require() 之后立刻抛错）
import qstock_mcp.baostock_api as api_mod
from qstock_mcp.baostock_errors import BaostockUpstreamError
from qstock_mcp.adapters.baostock_adapter import BaostockAdapter
from qstock_mcp.tools_fundamentals import get_fundamentals

def instrumented():
    raise BaostockUpstreamError("instrumented stop after lease gate")

api_mod._load_baostock = instrumented
out = get_fundamentals("600519", adapters=[BaostockAdapter()])
# -> status=error, error="全部数据源失败（各源错误见 attempted_sources）"
#    而不是 error_kind=baostock_locked
```

无租约（先 release）再跑同一探针 → `status=error, error_kind=baostock_locked`。

**代码路径**：`baostock_lease.py` 的 `require()` 只调用 `status()` 并检查 `leased`；
`adapters/baostock_adapter.py` 的 `_call_api` 先 `BaostockLease().require()`，再
`BaostockApi.get()`；没有 owner/pid/session lock。因此任何同机进程只要 lease 文件有效就能
进入数据路径。

**与 ADR 的冲突**：ADR 0019 Decision 2 写「不同 CLI 进程各登录一次，但租约保证它们不并发」；
实现实际只保证**再次 acquire** 被拒。`acquire → adj-backfill → release` 本身就是两个进程，
若数据路径只认本进程申请的租约，该流程无法工作——所以这不是简单“补一个 if”能修好的，
必须先定契约或做会话级互斥。

**待决策（#173）**：

- A. 改实现：数据路径增加进程/会话级互斥（例如数据命令启动时再获取带 owner/pid 的 session
  lock；崩溃/退出必须可清理；不能破坏 `acquire → run → release`），CLI 与 MCP 都覆盖。
- B. 改契约：承认租约是主机级许可，互斥只发生在 acquire；同步修订 ADR 0019、AGENTS.md、
  CONTEXT.md、`docs/baostock-lease.md`、#169/#172 验收条款，明确这是有意的行为变更。

本 ticket 不选边、不修复。#172 的 AC6 保持未通过；决策/修复由 #173 承载。

### 7.2 其他发现（不单独阻断）

- **文档一致性**：初版把“共享租约/互斥只在 acquire”写成已接受的契约，并据此把 AC6 判为
  通过；这违反了“验收 ticket 不单方面收窄 spec”和 ADR 冲突需显式指出的纪律。本报告已
  改为阻断结论；`docs/baostock-lease.md` §5 的表述改为“验收发现 + 待 #173 决策”，而不是
  新契约。
- **stale 的边界**：token 缺失/不匹配/损坏锁已覆盖；进程退出留下有效锁；`--force` 对健康
  锁/损坏锁已覆盖；死进程有效锁与活跃锁不可区分（无 PID 存活检查），这是无 TTL 设计的
  现状。
- **并发 acquire 的瞬态**：失败方可能在赢家写完 lock JSON 之前读取，返回
  `baostock_locked` 但 `lock=null`；顺序 acquire 稳定显示持有者。不影响“只允许一个赢家”。
- **release/acquire 竞争**：两个操作者同时 `release --force`/`release` 再 acquire 的理论
  窗口仍是协作锁边界（单操作者 acquire→run→release 不触发）；本轮不修改实现。
- **验收证据可复核性**：93 项 harness 与真实样本原始产物在本地 `files/issue-172-acceptance/`
  （gitignored），未入库；本报告给出关键命令与 AC6 探针。若要求所有断言都可独立复现，需把
  harness 作为验收工件入库（另开 ticket 或本报告修订时决定）。

### 7.3 未覆盖/未证明项

- **不测试黑名单**：未构造 `10001011`，未测试真实黑名单；`10001001`/`10002007` 的正常
  重试仅由既有 fake-client 单测覆盖，本轮真实样本未遇到。
- **真实容量**：5 个协议动作的单会话样本不能证明长回填（约 5,000 查询）或全市场性能；
  2026-11-05 的 508 次登录退化仍是有效教训。
- **官方计数口径**：login/logout/page 是否计入 5 万仍按保守假设（ADR 0019），未获官方
  确认。
- **多主机**：单主机文件租约；多主机共享出口 IP 的协调不在本期。

## 8. 回归与完成证据

| 层 | 命令 | 结果 |
| --- | --- | --- |
| 定向 | `.venv/bin/pytest -q tests/test_baostock_lease.py tests/test_baostock_api.py tests/test_scheduler_baostock_lock.py tests/test_server_baostock_lock.py tests/test_baostock_adapter_api.py` | 59 passed in 0.99s |
| 会话关闭 | 两个 release-closes-session 用例 + `test_lease_commands_do_not_touch_baostock` | 3 passed in 0.42s |
| 全量 | `.venv/bin/pytest -q`（根目录，含 `tests/` 848 + indicator-tools 87 + daily-review 15） | **950 passed in 233.34s** |
| 静态 | `git diff --check` | 通过（仅文档变更） |

回归只证明现有 fake/PG 测试通过；不覆盖 AC6 的共享租约行为。`git status` 只有文档变更，
无实现代码改动。

## 9. 结论与 #163 状态

- **#172 未通过**：AC6 字面不满足；#172 保持 open，native 新增 `blocked_by #173`。
- **#163 继续阻塞**：`blocked_by #172`（open）；上一条“验收通过/无剩余阻断”的 comment 已
  作废并更正。正式全窗口回填保持暂停。
- **解除条件**：先完成 #173 的决策/修复（A 或 B），再按新契约重新跑 AC6 与相关回归；
  #172 关闭后 #163 的 native blocker 才会全部满足。若选 B，还必须同步 ADR 0019/AGENTS/
  CONTEXT/#169/#172 的验收条款，不能只在验收报告里改口径。
- **仍然有效的部分**：单例/单会话、租约生命周期、CLI/MCP acquire/release、无租约
  `baostock_locked`、描述/instructions、定向 59 passed、全量 950 passed、真实小样本 5 个
  协议动作；它们不足以弥补 AC6，也不应被写成“全部通过”。

## 10. 复现入口

```bash
# 定向回归
.venv/bin/pytest -q tests/test_baostock_lease.py tests/test_baostock_api.py \
  tests/test_scheduler_baostock_lock.py tests/test_server_baostock_lock.py \
  tests/test_baostock_adapter_api.py

# 全量
.venv/bin/pytest -q

# 手工租约冒烟（临时目录，不触 baostock）
tmp=$(mktemp -d)
QSTOCK_BAOSTOCK_LOCK_DIR=$tmp .venv/bin/qstock-scheduler baostock-lock acquire \
  --owner qstock-agent --reason smoke
QSTOCK_BAOSTOCK_LOCK_DIR=$tmp .venv/bin/qstock-scheduler baostock-lock status
QSTOCK_BAOSTOCK_LOCK_DIR=$tmp .venv/bin/qstock-scheduler baostock-lock release
```

黑盒 harness 与原始结果在本地 `files/issue-172-acceptance/`（该目录不入库），
AC6 的独立探针见 §7.1。真实样本仅保留证据，不建议为复验重复请求上游；需要重跑时先完成
官方统计核对并遵守单会话、串行、显式 release。
