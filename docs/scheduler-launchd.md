# 调度配置说明（launchd，用户自行安装）

`qstock-scheduler` 是仓库自带的调度 CLI（pyproject `[project.scripts]`，直调
tools_* 核心层、不经 MCP 协议层）。本文件给出 macOS launchd 的安装样例；
技能与 MCP server 本身不内置调度。

## 子命令

| 子命令 | 用途 | 建议执行时机 |
| --- | --- | --- |
| `close [--date YYYYMMDD]` | 收盘后批：盘面五 section（boards/zt_pool/strong_stocks/lhb/indices）快照采集，逐 section 落采集台账；boards 成功时附带概念成分积累（cons → 成分字典 'em' 体系，独立成败、不影响退出码） | 交易日 15:30 之后（收盘数据就绪后） |
| `morning [--date YYYYMMDD]` | 次晨批：baostock 一把抓前一交易日全市场日线（含 tradestatus/turn，volume 股、amount 万元）+ 7 大指数尾行缺失即补，逐部分落采集台账 | 交易日开盘前（如 08:30）；baostock 当日 bar 发布较晚，不要用于当日 |
| `backfill [--start D] [--end D]` | 板块历史回补：push2his 板块 K 线回补 market_boards（默认窗口 = 前一交易日前推约 3 个月），回补行带 source='push2his' 标注 | 手动/灾难恢复；非定时任务 |
| `daily [--codes A,B \| --all] [--date D] [--adj qfq\|hfq\|none] [--check]` | 个股日线固定入口（issue #112）：腾讯 → 新浪 → baostock 抓取落库；`--check` 三源直取对拍（不落库）；`--all` 从 stock_list 取全市场（缺省 1 秒/只节流） | 收盘后 19:30 左右补当日行（T+0）；`--all` 全量刷新建议独立低频任务。手册见 [`daily-source-cli.md`](daily-source-cli.md) |

退出码：0 = 全部 section/part 成功（empty 无数据非故障）；1 = 任一
section/part 失败或输入非法（明细在 stdout JSON 的 `failed_sections`/
`failed_parts`）；2 = 参数错误。重复执行幂等（各业务键 upsert，台账
append-only 追加）。

morning 的 plist 与 close 同构：`ProgramArguments` 第二项改 `morning`、
`StartCalendarInterval` 改开盘前时间（如 Hour=8 Minute=30）、Label 与日志
路径相应改名（如 `com.qstock.scheduler-morning`）。注意 baostock 当日 bar
晚间才就绪（实测 18:00 前后尚无当日 bar，次日已有），morning 的目标日
恒为前一交易日，不要用 `--date` 指当日。

非交易日执行不会崩溃：上游当日无数据时 section 报告 empty 并落台账
（`error_kind=no_data_for_date`），退出码 0。如需严格只在交易日运行，
在 launchd 层用日历排除节假日，或先查 `query_collection_log` 就绪状态。

## launchd 安装（macOS）

1. 确认入口已注册（仓库根目录执行过一次 editable 安装即可）：

   ```bash
   .venv/bin/pip install -e .
   .venv/bin/qstock-scheduler close   # 冒烟：应输出 JSON 汇总
   ```

2. 写 plist `~/Library/LaunchAgents/com.qstock.scheduler-close.plist`（路径按
   实际仓库位置替换；`PG_DSN` 换成生产库连接串）：

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key><string>com.qstock.scheduler-close</string>
     <key>ProgramArguments</key>
     <array>
       <string>/path/to/q_stock_skills/.venv/bin/qstock-scheduler</string>
       <string>close</string>
     </array>
     <key>EnvironmentVariables</key>
     <dict>
       <key>PG_DSN</key><string>postgresql://localhost/qstock</string>
     </dict>
     <key>StartCalendarInterval</key>
     <dict><key>Hour</key><integer>15</integer><key>Minute</key><integer>30</integer></dict>
     <key>StandardOutPath</key><string>/tmp/qstock-scheduler-close.log</string>
     <key>StandardErrorPath</key><string>/tmp/qstock-scheduler-close.err</string>
   </dict>
   </plist>
   ```

3. 加载与运维：

   ```bash
   launchctl load ~/Library/LaunchAgents/com.qstock.scheduler-close.plist
   launchctl kickstart gui/$(id -u)/com.qstock.scheduler-close   # 手动触发一次验证
   tail -f /tmp/qstock-scheduler-close.log                        # 查看 JSON 输出
   launchctl unload ~/Library/LaunchAgents/com.qstock.scheduler-close.plist
   ```

4. 验证采集结果：`query_collection_log`（MCP 工具）按 trade_date 查当日五
   section 台账；有 error 行时按 `error_kind` 与 `detail` 定位（三态契约见
   ADR 0007）。

注意：launchd 的 `StartCalendarInterval` 只按日历触发、不感知 A 股交易日；
节假日触发会产生 empty 台账行，属正常语义（"上游确认无数据"），不是故障。
