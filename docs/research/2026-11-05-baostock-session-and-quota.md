# baostock 会话、并发与 5 万日额度：官方规则原文与仓库用法规约（#163 事故复盘）

日期：2026-11-05。范围：核对 baostock 官方黑名单/访问规则原文、官方错误码语义、已安装客户端
`0.9.3` 的线上行为，并盘点本仓库所有 baostock 入口的登录次数、并发可能路径与额度消耗，
给出可执行的规范化使用政策。**只读研究**：不改代码、配置、issue 或任何其他文件；本轮未发起
任何 baostock 登录/查询调用。

**证据边界**：官方文本取自 `www.baostock.com/blacklist` 的 SPA bundle（curl 抓页面 → 抓
`/assets/index-Ca2d3AOU.js` → 按字符偏移取原文）与官方 helpdocs markdown 端点；客户端行为取自
`.venv/lib/python3.12/site-packages/baostock/` 源码；仓库证据取自当前工作区（含提交 `0f746ca`
`27252a0`）与 `files/adj-backfill_formal_upstream_issue.md`、`docs/research/2026-11-04-adjustment-core-backfill.md`
§11。2026-10-31 两份报告的会话证据只作为**当时样本**引用，不外推为长期容量结论。

## 0. 结论先行

1. **官方只在一处写了额度**：黑名单页。原文是「每日API请求不能超过5万次，超过后进入黑名单控制。
   并且不能并发连接访问。」——一句话同时给了日额度与并发禁令，但**没有**定义「请求」是否含
   login/logout、也没有给并发连接数的阈值。API 文档页对此**一字未写**（已逐字核对）。
2. **冻结规则是公开公式**：「限制时长 = 本年累计限制次数 × 6 小时」，初次 6 小时，同年多次自动增长；
   黑名单错误码 `10001011`，官方示例在 **login 返回值**上判断。
3. **`10002007` 不是黑名单**：它是客户端在 `recv` 拿不到数据时**自己生成**的错误码
   （`loginout.py:77-80`、`season_index.py:224-227`、`history.py:186-189`）。2026-11-05 正式回填
   在 ~508 次登录/45 分钟后撞上的是 `10002007`，不是 `10001011`；官方统计端点当日对本机 IP
   返回零记录（§6.3）。**因此不能把这次事故当作"已被官方黑名单"的证据，但它足以证明"每请求登录"
   的握手节奏会在远低于 5 万日额度时把上游打到不响应。**
4. **本仓库默认就是「每请求一次登录」**：`BaostockAdapter(persistent=False)` 是默认值，
   `_run` 直接走 `_with_relogin`（`baostock_adapter.py:88-99, 256-258`）。`persistent=True`
   目前**只有** `adj-backfill` 一个调用方（`adjustment_backfill.py:881-885`）。
5. **`daily --all` 在最坏情形下单独就能超过 5 万**：5,924 只里 5,575 只可进 baostock（349 只北交所
   在登录前就被拒，`baostock_adapter.py:281-285`），每源 3 次尝试 ×（login+query+logout）
   = 9×5,575 = **50,175 次协议动作 > 50,000**（估算，见 §5.3）。
6. **真正卡住我们的是登录节奏与并发，不是日总量**：事故里 45 分钟约 1,524 次协议动作
   （≈ 日额度的 3%）、登录约 11.3 次/分，就已经退化到 20s 无响应；而 2026-10-31 另有
   「单会话 18 次串行全部成功」的样本（§6.2）。**一次会话 + 被踢重登**是唯一同时被两侧证据
   支持的做法，绝不能每请求登录、也不能并发。
7. **仓库缺的是制度而非开关**：无跨进程锁、无日计数器、无黑名单错误类、`fetch_chain` 对认证类
   错误照常重试 3 次（`fetch_chain.py:98-115`）、`cmd_daily`/`cmd_fundamentals` 每次循环都新建
   适配器链（`scheduler.py:471-474, 534-535`）。#163 正式回填在政策落地前**保持暂停**。

## 1. 官方规则原文（可复核）

### 1.1 黑名单/访问规则页

页面：<https://www.baostock.com/blacklist>（Vue SPA，可见文本内嵌于 JS bundle）。
复核方式（只读 HTTP，不触 baostock 数据接口）：

```bash
curl -sS https://www.baostock.com/blacklist -o page.html      # 只有 <div id="app">
grep -oE 'src="[^"]+"' page.html                              # → /assets/index-Ca2d3AOU.js
curl -sS https://www.baostock.com/assets/index-Ca2d3AOU.js -o index.js
```

原文逐字（bundle 字符偏移：`5万` @2249467、`本自然年度` @2249521、`限制时长` @2249583、
`待释放时间` @2249623、`黑名单用户` @2250248）：

| # | 原文 | 含义 |
| --- | --- | --- |
| 1 | 「每日API请求不能超过5万次，超过后进入黑名单控制。并且不能并发连接访问。」 | 日额度 5 万 + 禁止并发连接 |
| 2 | 「本自然年度初次被黑名单限制访问，冻结6小时后自动解除。本年多次被限制，限制时长会自动增长。」 | 首次 6h，同年累进 |
| 3 | 「限制时长 = 本年累计限制次数 × 6 小时。」 | 冻结时长公式 |
| 4 | 「如果待释放时间为空，请等待5分钟再后刷新页面。」 | 统计表待释放时间可能滞后 |
| 5 | （示例代码注释）「当黑名单控制时，error_code输出"10001011"、error_msg输出"黑名单用户，请与管理员联系"」 | 黑名单码 + 文案 |
| 6 | 示例代码 `lg = bs.login()` … `if(lg.error_code == "10001011")` | 官方在 **login** 上判黑名单 |

统计表列（同为 bundle 内模板）：**时间 / 互联网IP / 待释放时间 / 日访问总次数 / 并发连接访问数 /
今年被限制次数**（`日访问总次数` @2252115、`并发连接访问数` @2252173、`今年被限制次数` @2251538）。
表按 **IP** 记「日访问总次数」与「并发连接访问数」——这是「额度按 IP 计」「并发被单独计量」的
直接依据，但**不等于**公布了并发阈值。

冻结公式的两点读法（**推断，非原文**）：①「本年累计限制次数」是否含本次，原文未明说；按
「初次 6 小时」推，第 n 次 = 6n 小时（n≥1）。② 冻结按自然年累计，不因中途解封清零。

### 1.2 官方错误码文档（可直接读到明文）

页面：<https://www.baostock.com/mainContent?file=baostock_error_codes.md>；其内容由
`POST https://www.baostock.com/helpdocs/api/markdown/baostock_error_codes.md` 返回 markdown
（本轮抓取，HTTP 200）。关键行逐字：

| 码 | 官方说明 | 官方处理建议 |
| --- | --- | --- |
| `10001001` | `BSERR_NO_LOGIN` 用户未登陆 | 请先调用 `bs.login()` 进行登录 |
| `10001005` | `BSERR_LOGIN_COUNT_LIMIT` 账号登陆数达到上限 | 等待其他登录会话过期后重试，或先 `bs.logout()` 登出其他会话 |
| `10001011` | `BSERR_BLACKLIST_USER` 黑名单用户 | 该账号已被限制使用，请联系客服 |
| `10002004` | 连接断开 | 重新登录并重试 |
| `10002007` | `BSERR_RECVSOCK_FAIL` 网络接收错误 | **重新登录确认登录有没有成功** |

2.4 节「IP黑名单检查」原文：「当登录时返回错误代码 `10001011`，表示IP已被加入黑名单」，
并给出与黑名单页同款的 `lg = bs.login()` 检查示例。

**官方文档内部不一致（必须记录）**：同一份文档 2.4 节说「IP 被加入黑名单」，第 3 节表说
「该账号已被限制使用」。黑名单页与统计表都以 IP 为维度。→ 结论：**控制维度大概率是 IP**，
「账号」措辞可能只是「该 IP 背后的使用者」的口语化；但**未证实**是否存在账号维度叠加。

`10001005` 的存在说明**上游确有「同账号并发登录数上限」**这一机制（处置建议是"登出其他会话"），
但官方从未给出数字 → 阈值**待验证**。

### 1.3 官方 Python API 文档没有额度条款

`POST /helpdocs/api/markdown/pythonAPI.md`（本轮抓取，72,580 字节）内 `5万`/`并发`/`次数`/`黑名单`
出现次数均为 **0**。helpdocs 菜单里也没有第二份访问规则（菜单把 `default.md` 标为「访问规则(统计)」，
但其正文实际是 K 线字段表）。→ **5 万/并发/冻结只存在于黑名单页这一处**，
这决定了第 2 节「官方明说」与「我们保守假设」的分界。

## 2. 已证实 / 待验证

### 2.1 已证实（有一手出处）

| 命题 | 出处 |
| --- | --- |
| 日 API 请求上限 5 万，超过进入黑名单控制 | 黑名单页原文 1（bundle @2249467） |
| 不能并发连接访问；统计表单独列「并发连接访问数」 | 黑名单页原文 1 + 表列（@2252173） |
| 首次冻结 6h，同年累进，`限制时长 = 次数 × 6h` | 黑名单页原文 2、3 |
| 黑名单码 `10001011`，文案「黑名单用户，请与管理员联系」，官方在 login 上判 | 黑名单页示例 + 错误码文档 2.4 |
| `10001005` = 账号登录数达上限，处置是登出其他会话 | 错误码文档第 3 节 |
| `10002007` 由客户端在「收到空数据」时生成，不是服务端业务码 | `loginout.py:77-80`；`season_index.py:224-227`；`history.py:186-189`；`socketutil.py:89-91` |
| 客户端 `recv` 循环自身没有读超时 | `socketutil.py:66-73`（`while True` + 结束符判断） |
| 分页会额外发请求（每页 2,000 行），逐日接口不翻页（单页 20,000） | `contants.py:187`；`resultset.py:54-120`；`history.py:200`；`season_index.py:235` |
| 本仓库默认每请求登录/登出 | `baostock_adapter.py:88-99, 229-236, 256-258` |
| 仓库无跨进程锁、无上游日计数器 | 全仓检索 `flock` / `fcntl` / `advisory` 均 0 命中 |
| 2026-11-05 事故：~508 因子日/45min → `10002007`，TCP 仍可达 | `files/adj-backfill_formal_upstream_issue.md:9-23`；§11 |
| 同日官方统计端点对本机 IP 无黑名单记录 | §6.3（本轮会话采集） |

### 2.2 待验证（不得当成官方结论）

| 命题 | 现状 | 保守处置 |
| --- | --- | --- |
| **login/logout 是否计入 5 万** | 官方只说「每日API请求」，未枚举；login 是独立消息类型 `00`（`contants.py:29-32`），与查询 `13/15/98/9C` 不同 | **一律计入**，并按协议动作计（1 login = 1） |
| **翻页请求是否计入 5 万** | 官方未提；每页 2,000 行会额外发请求 | **一律计入** |
| **并发连接阈值（>1？=2？）** | 官方只写"不能并发"，未给数字；`10001005` 暗示有账号级上限 | **并发上限按 1 执行**（同机同 IP 串行） |
| **额度按 IP 还是账号** | 统计表按 IP；错误码文档同段又写"账号" | 按 IP 规划，按"同 IP 全局唯一"执行 |
| **`10001011` 是否只在 login 出现** | 官方示例只演 login；查询路径也可能透传任何 error_code（客户端不拦截） | 任何入口出现 `10001011` 都按**致命**处理 |
| **会话寿命/被踢阈值** | 内部证据互相矛盾：一次 18 请求全成功，一次约 6 次后被踢（§6.2） | 不假设寿命；`10001001` 即重登 |
| **`10001005` 的实际并发上限** | 从未观测到；只有官方处置建议 | 不试探，不并发 |
| **冻结期间新请求是否延长冻结** | 原文只给累进公式，未说"冻结中再请求"是否计入次数 | 冻结期**零请求** |
| **长期可承载 TPS / 全量 5,000+ 请求** | 未实测（§11 明确"待验证"） | 串行 ≥1.5s，小样本验证后再放量 |

## 3. 已安装客户端的线上行为（`baostock 0.9.3`）

### 3.1 login / logout

- `login(user_id='anonymous', password='123456')`（`login/loginout.py:21`）：**本仓库不传参**
  （`baostock_adapter.py:38` 的 `bs.login()`），即所有批次都登在 **anonymous 共享账号**上。
  这解释了为什么 `10001005`/被踢会跨进程互相影响——同账号会话池是共享的（**推断**）。
- 每次 `login()` 都**新建 TCP 连接**：`sock.SocketUtil().connect()`（`loginout.py:69-72`）→
  `socket.socket(...).connect((public-api.baostock.com, 10030))`（`socketutil.py:34-41`、
  `contants.py:11-12`），并把它塞进模块级 `context.default_socket`。
- 因此 **1 次 login = 1 次 TCP 握手 + 1 次登录往返**；`SocketUtil` 是单例
  （`socketutil.py:16-32`）且 `connect()` 直接覆盖 `context.default_socket`，旧 socket 不关闭
  → 同进程内二次 login 会**泄漏旧连接并让旧会话失联**（`loginout.py:41`、`socketutil.py:41`）。
- `logout()` 发 `02` 消息、解析响应，然后关闭 `context.default_socket`（`loginout.py:160-163`）。
  login 失败时 `_logout` 不会被调用（`baostock_adapter.py:90-99` 的 `finally` 只覆盖成功登录后的路径）。
- 登录失败**不是抛异常**，而是返回 `ResultData(error_code=...)`（`loginout.py:84-100`）；
  只有 `recv` 拿到空串才置 `10002007`。

### 3.2 查询与分页

- 每个 `query_*` 先做**本地**登录态检查：读 `conx.user_id`，缺失才返回 `10001001`
  （`history.py:157-163`、`season_index.py:196-201`、`stock_metadata.py:160-167`）。
  **这些检查不会发现会话已在服务端被踢** —— 于是下一次请求照发，由服务端返回 `10001001`。
- 查询结果由 `ResultData.next()` 边遍历边翻页：当本页恰好 2,000 行时，**再发一次请求**取下一页
  （`resultset.py:54-120`，页大小 `contants.py:187`）。所以「一次查询」在长区间上等于
  1 + ⌈行数/2000⌉ − 1 次协议往返。
- 两个逐日接口把 `per_page_count` 写成 `'20000'` 且不分页（`history.py:200`、
  `season_index.py:235`），单次调用即一次往返。
- 客户端**不做错误分类**：任何非 `0` 的 `error_code` 原样塞进 `ResultData`，由调用方决定；
  本仓库只在 `_query_result` 里特判 `10001001`（`baostock_adapter.py:103-108`），
  `10001011`/`10001005` 会落到普通 `FetchError`，并被 §8 的重试策略放大。

### 3.3 超时与错误码产生位置

- `send_msg` 的接收循环是 `while True: recv(8192)`，只有读到结束符才 break，**自身无读超时**
  （`socketutil.py:66-73`）；任何异常被吞成 `print` + 返回 `None`（`socketutil.py:89-91`）。
- `10002007` 的产生点：`receive_data is None or ""`（`loginout.py:77-80`、
  `season_index.py:224-227`、`history.py:186-189`）——即**客户端判定"没收到"**。
  这解释了事故现象：TCP 三次握手 0.03s 可通，但 login 20.04s 后回 `10002007`。
- 本仓库的兜底：`socket.setdefaulttimeout(60)`（`adapters/__init__.py:9-11`）+ 单请求
  `SIGALRM` 120s（`baostock_adapter.py:52-77`）。注意后者**只包住**
  `_with_relogin` / `_run` / `ensure_session` / `_logout_quietly`；`fetch_stock_list`、
  `fetch_index_daily`、`fetch_fundamentals` 的 login 与查询**没有**这层 alarm（§8 缺口 3）。

## 4. 仓库代码清单：每条入口的登录次数

「登录次数」指 `bs.login()` 调用次数；`persistent=False` 时每个**适配器方法调用**= 1 次
login + 1 次 logout（`_with_relogin` 的 `finally`）。

| # | 入口（触发点） | 代码位置 | 适配器构造 | 每次调用的登录 | 批量倍数 |
| --- | --- | --- | --- | --- | --- |
| 1 | MCP `fetch_daily` / `query_daily` | `server.py:37,53` → `tools_daily.py:163-182` | `default_daily_adapters()` **每次工具调用新建** | 1（若 baostock 胜出） | × 工具调用次数 |
| 2 | CLI `daily --codes A,B` | `scheduler.py:412,471-474` | 每 code 传 `adapters=None` → `tools_daily.py:181-182` 再建链 | 1/code | × codes |
| 3 | CLI `daily --all` | `scheduler.py:469-495` | 同上，每 code 重建 | 1/code | × 5,924（可进 baostock 5,575） |
| 4 | CLI `daily --check`（三源对拍） | `scheduler.py:464-468` → `daily_parity.py:150-151` | 一次建链，逐 code 复用**同一批适配器** | 1/code/source | × codes（baostock 腿） |
| 5 | `init_database(backfill_history=True)` | `tools_init.py:274-282` → `_init_backfill:193-208` | 一次建链，5,924 只共用 | **1/code** | × 5,924 |
| 6 | `init_database` 指数段 7 只 | `tools_init.py:280-282, 146-150` | 一次建链 | 1/index | × 7 |
| 7 | `init_database` 清单段 | `tools_init.py:274` → `default_adapters()` | 一次建链 | 1（baostock 兜底时） | × 1 |
| 8 | CLI `fundamentals --codes N` | `scheduler.py:515,534-535` | 每 code `adapters=None` → `tools_fundamentals.py:33-34` 重建链 | 1/code | × N（每 code 还 ≤8 次查询） |
| 9 | CLI `index`（7 只） | `scheduler.py:566,589-593` → `tools_index.py:88,97-99` | 一次建链，逐指数复用实例 | 1/index | × 7（baostock 腿） |
| 10 | CLI `morning` | `scheduler.py:179-181` → `tools_morning.py:394-396` | **一个**非持久 `BaostockAdapter()` | 2（astock + factor） | × 1/日 |
| 11 | `morning` 指数尾行补漏 | `tools_morning.py:398` → `default_index_daily_adapters()` | 一次建链 | 0-7（仅缺失尾行时） | × 缺失指数数 |
| 12 | CLI `stock-list` | `scheduler.py:753,772-774` | 一次建链 | 1（baostock 兜底时） | × 1 |
| 13 | CLI `adj-backfill` | `scheduler.py:218,240-243` → `adjustment_backfill.py:881-885` | **`persistent=True`** + `ensure_session()` | **1/整轮**（被踢重登 ≤3） | × 1 |
| 14 | `live-sample` | `tools_live_sample.py:282,299,320` | 一次建链（缺省注入缝） | 1/code | × 抽样数 |
| 15 | `stock_list_parity`（`stock-list --check`） | `stock_list_parity.py:24-37` | 一次建链（`default_adapters()`），逐源调用 | 1/源 | × 2（baostock 为第 2 源） |

要点：

- **同一个 CLI 命令内部，适配器实例的复用与否并不决定登录次数**——因为默认
  `persistent=False`，登录发生在**方法调用**上。第 5、9、14 行"复用实例"仍然每 code 登录一次。
- 唯一真正把登录摊薄到整轮的只有 `adj-backfill`（第 13 行）。
- `fetch_astock_daily` 走 `_with_relogin`（`baostock_adapter.py:326`），**绕过 `_run`**，
  即 `persistent=True` 对它无效；`morning` 用的是非持久实例，所以表现为每日 2 次登录。

## 5. 额度数学

### 5.1 口径

- 计数单位：**协议动作** = 一次消息往返。`login` = 1（另加 1 次 TCP 握手），`logout` = 1，
  一次查询 = 1，每次翻页 = +1。本口径把「登录是否计数」「翻页是否计数」两个未知项都按**计入**
  处理（§2.2），因此是**保守上限**。
- 基数：`stock_list` 2026-10-30 快照 **5,924** 只（`docs/research/2026-10-31-snapshot-as-raw-source.md:235`），
  其中北交所 349 只（同文件 `:316`）→ 可进 baostock 的 **5,575** 只（估算：扣除 BSE 后在
  `sh.6/sz.0/sz.3` 前缀内的数量，未逐码核对）。
- 翻页估算：全历史 1990→今约 8,700 个交易日上限；多数个股 1,000–3,000 行 → 每 code
  **1–2 页**（估算）。逐日接口 0 翻页。

### 5.2 每请求登录 vs 单会话（单轮/单命令）

| 命令 | 数据查询 | 每请求登录（协议动作） | 单会话（协议动作） | 差额来源 |
| --- | --- | --- | --- | --- |
| `daily --all`（1 日，仅 baostock 腿） | 5,575 | 3×5,575 = **16,725** | 5,575 + 2 = **5,577** | 2×N 全是 login/logout |
| `daily --all` 最坏（每源 3 次尝试） | 5,575×3 | 9×5,575 = **50,175** | 16,727 | 重试把登录也放大 3 倍 |
| `init` 全量回溯（1990→今） | 5,575（+1–2 页/code） | 3N+P ≈ **16,700–27,900** | N+P+2 ≈ **5,600–11,200** | 同上 |
| `fundamentals --codes 50` | ≤400（8/code） | 50×10 = **500** | 400 + 2 = **402** | 每 code 2 次控制动作 |
| `index`（7 只，1990→今） | 7（+≈4 页/只） | 21 + 28 = **49** | 7 + 28 + 2 = **37** | 14 次控制动作 |
| `morning`（每日） | 2 | **6** | **4** | 省 2 次 |
| `stock-list`（5,924 行 → 3 页） | 1 + 2 页 | **5** | 5 | 单次操作，无摊薄空间 |
| `adj-backfill`（928 因子日 + ≥4,116 票） | 5,044 | 3×5,044 = **15,132** | 5,044 + 2 = **5,046** | 10,088 次握手 |
| MCP 单次 `fetch_daily` | 1（+0–1 页） | 3–4 | 3–4（进程常驻才有意义） | 需按日共享会话 |

`adj-backfill` 的 4,116 只事件票是**实测下界**（510 个因子日累计，`files/adj-backfill_formal_upstream_issue.md:9`）；
按 8.07 只/日线性外推 928 日约 7,500 只（**估算，必然偏高**，唯一票数随窗口增长是次线性的），
故全窗口 raw 阶段大致 4,100–7,500 次查询，总量 5,000–8,400 次。

### 5.3 与 5 万的比较

- **最坏情形能单独突破 5 万的命令有两个**：`daily --all`（每请求登录 + 每源重试满 3 次
  = 9×5,575 = 50,175）与同样跑满 3 次重试的 `init` 全量回溯（同一算式：每 code 9 次协议动作）。
  这还**不含**同一天的任何其他命令；若同日再跑 `fundamentals`，必超。
- `init` 单次尝试（≈1.7–2.8 万）与 `adj-backfill`（≈1.5 万）单看都在额度内，但它们把
  **登录次数**推到 5,575 / 5,044 量级——事故证明 508 次/45 分钟就足以打崩（§6.1）。
- 单会话后，所有命令的协议动作降到 **N+P+2**，登录从「≈2/3 的流量」变成「2 次/轮」。
- **登录计数未知的代价**：若官方把 login/logout 计入 5 万，上表"每请求登录"列就是真实消耗；
  若不计，真实消耗仍不低于 N+P，但**握手速率**依然会触发上游限速（经验证据，非官方条款）。

## 6. 事故证据与解释

### 6.1 2026-11-05 正式回填：508 次登录/45 分钟后 `10002007`

`docs/research/2026-11-04-adjustment-core-backfill.md` §11 与
`files/adj-backfill_formal_upstream_issue.md:9-23` 记录：

| 项 | 观测 |
| --- | --- |
| 窗口/断点 | `adj-backfill --start 20230101 --end 20261104`，停在 **510/928** 因子日 |
| 前 45 分钟 | 每请求 login/logout，推进 ~508 个因子日，8,559 事件 / 4,116 票，0 失败日 |
| 退化 | 约 40–45s/日；进程 46 分钟仅 1.65s CPU，PG 连接 idle |
| 16:22 探针（`setdefaulttimeout(20)`） | DNS 正常（114.94.20.42）、TCP 3 次 0.03–0.04s；`bs.login()` **20.04s → `10002007`**；`query_daily_adjust_factor` 20.0s 同样；`logout` 19.85s 同样 |
| 冷却后单会话探针 | login 51s 成功；随后 factor 0.5–0.6s、raw 1.66–5.06s |

换算：45 分钟 ≈ 1,524 次协议动作（508 login + 508 query + 508 logout），占 5 万日额度的
**≈3%**；登录速率 ≈ **11.3 次/分**（若 5 万均匀分配，日均上限约 34.7 次/分）。
→ **日总量远未触顶，握手节奏先崩**；`10002007`（客户端"没收到"）而非 `10001011`（官方黑名单）
说明当时是**限速/上游故障**，不是已判黑。

### 6.2 2026-10-31 的两份相反样本：18 次成功 vs ~6 次被踢

| 样本 | 条件 | 结果 |
| --- | --- | --- |
| `docs/research/2026-10-31-qfq-backcast-consistency.md:34,171` | 单会话、18 次串行、间隔 ≥1.5s、adjustflag=3 | **18/18 成功**，0.03–2.97s/请求 |
| `docs/research/2026-10-31-tencent-day-event-metadata.md` A.2 | 单会话、24 次（6 日线 + 18 分红）、间隔 1.5s | 首轮 **17/24 `10001001 用户未登录`**，会话在约 6 次后被踢；重登后 24/24 成功 |
| `docs/research/2026-11-04-adjustment-core-backfill.md` §1.3 | 单会话、3 次串行 | 3/3 成功 |

同日、同网络、同客户端，一次撑过 18 次、一次约 6 次被踢 → **会话寿命不是固定请求数**，
更像服务端按账号/负载做的会话淘汰（anonymous 共享账号，§3.1）。这直接给出两条规则：
**被踢就重登重试同一请求**（否则大面积静默失败），**不要试图"算准"会话能撑多少次**。

### 6.3 官方统计端点：当日对本机 IP 无黑名单记录

黑名单 SPA 的数据源就是 `POST https://www.baostock.com/helpdocs/api/wd-blacklist-stats`
（bundle @2245097 取列表、@2246652 按 IP 搜索）。本轮会话（父 agent）于
**2026-11-05 ~18:20 Asia/Shanghai** 以本机公网 IPv4（**39.144.\*.\***，已脱敏）携
`Origin/Referer: https://www.baostock.com/blacklist` 调用，返回：

```json
{"stats":{"total":0,"data":[]},"yearlyRestrictCount":0}
```

解读（**有限结论**）：当时官方侧**没有**本机 IP 的黑名单记录、今年被限制次数为 0。
因此 16:22 的 `10002007` **不是**官方黑名单响应（那应是 `10001011`），与"登录限速/上游故障"
一致。**它不能证明"每请求登录是安全的"**——只能把"已被官方拉黑"这一解释排除掉；
黑名单统计也可能滞后（见官方文案 4「待释放时间为空…等 5 分钟」），故仍需按最坏情况设计。

### 6.4 三条硬结论

1. **一次登录、串行复用**：单会话既有成功样本（18 次、3 次、冷却后 factor 0.5–0.6s），
   又是唯一能把控制动作从 2/3 降到 2 次/轮的形态。
2. **`10001001` 只意味着一件事**：重登 + 重试**同一请求**，绝不映射成"无数据"。
3. **绝不每请求登录**：508 次/45 分钟即触发退化；`daily --all` 最坏情形还会直接越过 5 万。

## 7. 规范化使用政策（10 条）

> **决策更新（2026-11-05，grill 后）**：§7 第 2 条“自动按批次加锁”已被
> [ADR 0019](../adr/0019-baostock-explicit-lease-and-quota.md) 取代为**显式租约**：
> CLI `baostock-lock` / MCP `baostock_lock` 由操作者显式 acquire/release，数据命令/工具只
> 检查、不自动申请/释放，进程退出不自动释放；操作契约见
> [docs/baostock-lease.md](../baostock-lease.md)。§7 其余配额/错误/串行/审计条款保留，
> 冲突时以 ADR 0019 为准。
>
> 定位：本仓库所有 baostock 入口（CLI / MCP / 脚本）的**统一纪律**。参数是保守值，
> 需在 `grill-with-docs` 拍板后落成 spec（§9）。

1. **一批一登录**：一个批次（一次 CLI 命令 / 一次回填 / 一次 MCP 会话）只 `login()` 一次，
   批次内所有请求共用该会话；批次结束（含异常、Ctrl-C）`logout()` 一次。禁止在循环体内登录。
2. **跨进程互斥**：同机同公网 IP 同时只允许 **1** 个 baostock 批次。用文件锁
   （如 `~/.cache/qstock/baostock.lock` 的 `flock`，写入 PID/命令/开始时间）实现；
   拿不到锁 → 排队或直接拒绝并给出持锁者信息。**在锁内再串行**，不用"锁 + 并发"折中。
3. **共享日额度账本**：以「协议动作」计数，落本地账本（JSONL 或 PG 表，按自然日与 IP 归集）。
   软上限 **30,000**（告警 + 降级为只跑高优先级批次）、硬上限 **45,000**（拒跑，留 10% 给人工排查）。
   登录、登出、翻页都计数（§2.2 的保守假设）。
4. **`10001011` 致命**：立即终止批次、**零重试**、写审计（时间/IP/命令/累计次数）、按官方
   `待释放时间` 等待。禁止换账号、换代理、换节点绕过。
5. **`10001001` 重登重试**：捕获后关闭旧会话 → 重登 → 重试**同一请求**，最多 3 次，
   重登前退避 ≥1.5s；3 次仍失败则整批失败并保留断点，**不得**记成"当日无数据/空结果"。
6. **只重试瞬时错误**：`10002004/10002007/10002008`、超时、连接重置 → 指数退避
   `0.5/1/2s ×(0.75–1.25)`；`10001002/10001005/10001011/10001004` 与 `10004xxx` 参数类
   **一律不重试**（重试只会加重限速或掩盖配置错误）。
7. **串行 ≥1.5s + 抖动**：相邻上游请求最小间隔 1.5s，叠加 0–0.5s 随机抖动；全流程单线程串行。
   吞吐不够时**减批量**，不加并发。
8. **批量调用方复用适配器链**：`daily --all`、`fundamentals --codes`、`init --backfill` 等
   必须在循环外构造**一次** `BaostockAdapter(persistent=True)` 并传入每条记录；
   适配器需覆盖 `fetch_astock_daily`（当前绕过 `_run`，§8-4）。
9. **作用域边界**：CLI 一次命令 = 一个批次 = 一份额度；MCP 每次工具调用 = 一个批次
   （stdio 常驻进程按自然日共享账本与会话，但**不假设**会话跨调用存活，收到 `10001001` 即重登）。
   `morning`/`close`/`daily` 定时任务之间必须被第 2 条锁串行化。
10. **审计与可观测**：每批次输出/落库 `{login, logout, queries, pages, relogins, kicks,
    error_code 分布, 耗时, 最大间隔}`；错误码进 `collection_log`；批次结束打印账本余额。
    没有审计数据的批次视为未验证。

## 8. 同类代码缺口（只记录，不修改）

| # | 缺口 | 位置 | 现状与风险 |
| --- | --- | --- | --- |
| 1 | 每请求 `_with_relogin` | `baostock_adapter.py:88-99, 256-258` | 默认 `persistent=False`；每方法调用 1 login + 1 logout，是 §6.1 事故的直接成因 |
| 2 | `fetch_fundamentals` 不处理被踢 | `baostock_adapter.py:388-436` | 用裸 `_login`，`10001001` 只被 append 进 `errors` 后继续下一个季度 → 最多 8 次无效查询，最终报"无数据"而非重登 |
| 3 | 三个方法无 120s 硬超时 | `baostock_adapter.py:350-384, 388-436` | `fetch_stock_list`/`fetch_index_daily`/`fetch_fundamentals` 的 login 与查询不在 `_request_deadline` 内，只剩 60s socket 默认超时（每 recv） |
| 4 | `fetch_astock_daily` 绕过持久会话 | `baostock_adapter.py:326` | 直接 `_with_relogin`，`persistent=True` 无效；且会覆盖 `context.default_socket`，与 `self._session` 指向的旧会话不一致 |
| 5 | `fetch_chain` 重试认证/黑名单 | `fetch_chain.py:30-34, 98-115` | 统一 3 次尝试，无 `auth`/`blacklist` 分类；`10001011` 会被重试 3 次（`unsupported` 才单次即弃） |
| 6 | `cmd_daily` 每 code 重建链 | `scheduler.py:471-474` + `tools_daily.py:181-182` | 每只股票一次 `default_daily_adapters()`，适配器与会话都无法跨 code 复用 |
| 7 | `cmd_fundamentals` 每 code 重建链 | `scheduler.py:534-535` + `tools_fundamentals.py:33-34` | 同上，且每 code 还叠加 ≤8 次查询 |
| 8 | 无跨进程锁 | 全仓（`flock/fcntl/advisory` 检索 0 命中） | `morning`(08:30)、`close`(15:30)、`daily`(19:30)（`docs/scheduler-launchd.md:9-18`）可与数小时的 `adj-backfill` 重叠 → 同 IP 双会话 |
| 9 | 无上游日计数器 | 全仓 | `collection_log` 只记业务采集结果，不记协议动作/登录次数，额度不可观测 |
| 10 | 无黑名单错误类 | `baostock_adapter.py:103-108` | `_query_result` 只特判 `10001001`；`10001011`/`10001005` 落到通用 `FetchError` |
| 11 | 只有 adj-backfill 关闭会话 | `adjustment_backfill.py:1113-1119` | `close()` 仅此一处调用；其他入口靠每次 logout，进程常驻 MCP 场景无生命周期管理 |
| 12 | baostock 登录失败被分类为 `fetch_failed` | `adapters/base.py:253-271`；`baostock_adapter.py:41-42` | 客户端把网络失败变成 `error_code` 字符串而非异常，`fetch_failure_kind` 只能判 `other` → `aggregate_error_kind` 给 `fetch_failed` 而非 `upstream_unreachable`（`fetch_chain.py:57-66`） |
| 13 | `morning` 两次登录 | `tools_morning.py:394-396` | 一个非持久实例 + `fetch_astock_daily` 绕过 `_run` → 每日固定 2 次 login/logout |

## 9. 建议下一步（本轮不写代码）

1. **先决策，不先实现**：用 `/grill-with-docs` 逐条拍板下列 5 个问题，产出可验收的 spec：
   - **锁位置**：`~/.cache/qstock/` 文件锁 vs PG advisory lock？锁粒度是"每 IP"还是"每机器"？
     进程崩溃后锁如何自动失效（`flock` 天然释放是否够）？
   - **额度账本**：JSONL vs PG 表？计数口径（协议动作/数据查询二选一）？软/硬上限取值
     （本文建议 30,000/45,000）？跨日如何重置（本地时区 00:00）？
   - **黑名单政策**：`10001011` 后是否整机停跑到官方待释放时间？谁来解除（人工确认还是
     自动读统计端点）？是否允许换 IP？
   - **作用域边界**：MCP 常驻进程算"一个批次"还是"每次工具调用一个批次"？会话空闲多久关闭？
   - **请求计数落点**：在 `baostock_adapter` 内统一埋点，还是在 `fetch_chain`/调用方？
     翻页计数需要 `resultset` 侧配合（当前无钩子）。
2. **再出 spec + tickets**：把 §7 的 10 条写成 spec 的验收条款，按"锁 → 账本 → 错误分类 →
   会话复用 → 审计"拆 ticket；每个 ticket 带一条可测断言（如"`daily --all` 全跑完 login 次数 = 1"）。
3. **实现顺序建议**（依赖自低到高）：错误分类与不重试 → 会话复用覆盖 `fetch_astock_daily`
   与 `fetch_fundamentals` → 跨进程锁 → 日账本 → 审计输出。前两步就能消除"每请求登录"。
4. **#163 正式回填保持暂停**：raw 阶段 0 行、510/928 断点保留。在"单会话 + 锁 + 账本"
   落地并用小样本实测（≤50 次请求，验证长会话与登录速率）之前，不续跑；续跑前先读一次
   官方统计端点确认 `今年被限制次数 = 0`、`待释放时间` 为空。
5. **验证边界要写清**：任何新参数（并发 1、间隔 1.5s、软/硬上限）都只是**保守默认**，
   不是官方数值；实测记录须注明日期/样本/条件，单轮成功不外推为长期容量。

## 10. 引用清单

官方（本轮均可复核）：

- 黑名单/访问规则：<https://www.baostock.com/blacklist>；bundle <https://www.baostock.com/assets/index-Ca2d3AOU.js>
  （偏移见 §1.1）。
- 错误码文档：<https://www.baostock.com/mainContent?file=baostock_error_codes.md>
  （markdown 端点 `POST /helpdocs/api/markdown/baostock_error_codes.md`）。
- Python API 文档：<https://www.baostock.com/mainContent?file=pythonAPI.md>
  （`POST /helpdocs/api/markdown/pythonAPI.md`，全文无额度条款）。
- 黑名单统计端点：`POST /helpdocs/api/wd-blacklist-stats`（bundle @2245097 / @2246652）。

已安装客户端（`.venv/lib/python3.12/site-packages/baostock/`）：

- `login/loginout.py:21, 41, 69-72, 77-80, 84-100, 108-130, 160-163`
- `util/socketutil.py:16-32, 34-41, 55-84, 89-91`
- `common/contants.py:11-12, 29-32, 142, 146, 152, 159, 187`
- `data/resultset.py:28, 54-120`
- `security/history.py:20-24, 140-205`
- `evaluation/season_index.py:185-240`
- `metadata/stock_metadata.py:138-190`

仓库：

- `src/qstock_mcp/adapters/baostock_adapter.py:27-49, 52-108, 229-276, 278-346, 350-436`
- `src/qstock_mcp/adapters/__init__.py:5-11, 60-72, 91-103, 120-132, 135-146`
- `src/qstock_mcp/adapters/base.py:190-201, 217-271`
- `src/qstock_mcp/fetch_chain.py:30-39, 57-66, 87-126`
- `src/qstock_mcp/scheduler.py:179-181, 218-243, 412-495, 515-535, 566-593, 753-774`
- `src/qstock_mcp/tools_daily.py:163-182`；`tools_init.py:193-208, 274-282`；
  `tools_index.py:88-99`；`tools_fundamentals.py:33-34`；`tools_morning.py:394-398`；
  `daily_parity.py:150-151`；`tools_live_sample.py:282, 299, 320`
- `src/qstock_mcp/adjustment_backfill.py:17-24, 54-56, 881-885, 916-925, 1113-1119`
- `tests/test_adapters.py:270-333, 373-440, 445-505`；`tests/test_adjustment_backfill.py:812-865`
- `docs/research/2026-11-04-adjustment-core-backfill.md:306-325`（§11）；
  `docs/research/2026-10-31-tencent-day-event-metadata.md:342-350`（A.2）；
  `docs/research/2026-10-31-qfq-backcast-consistency.md:34, 171`；
  `docs/research/2026-10-31-snapshot-as-raw-source.md:235, 316`；
  `docs/adj-backfill-cli.md:26, 292-303`；`docs/scheduler-launchd.md:9-18`
- `files/adj-backfill_formal_upstream_issue.md:9-23, 36`
