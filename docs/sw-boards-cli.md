# 申万 boards 固定入口操作手册（`qstock-scheduler sw-boards / backfill --source sw / sw-dict`）

issue #118（spec #111）：boards 行业维度迁移申万官网（swsresearch.com），一级
（31）+二级（124）双级口径。本手册是该能力的固定 CLI 入口说明——抓取、历史
回补、成分字典在线刷新、三方位对拍都走同一入口，可重复运行、可挂定时任务；
不使用一次性临时脚本。

## 适用场景

| 场景 | 命令要点 |
| --- | --- |
| 收盘后抓当日申万双级板块快照 | `sw-boards --date <当日>`（收盘后调用=当日收盘截面） |
| 迁移初始化：板块历史回补至 1999 | `backfill --source sw`（全量，155 板块逐只 trend 全历史） |
| 补一段历史窗口 / 修订某天 | `backfill --source sw --start <起> --end <止>` |
| 对拍验证（官网 vs 库内 vs 自算） | `sw-boards --date <日> --check [--level 1\|2\|all] [--limit N]` |
| 成分字典在线刷新（含北交所归属） | `sw-dict`（申万不定期维护，建议周频） |
| 每日估值维度例行入库 | `morning`（申万分析报表 sw_board_valuation part，T+1） |

## 前置条件

- 已完成 editable 安装：`.venv/bin/pip install -e ".[sources,dev]"`；
- 环境变量 `PG_DSN` 指向目标库（**业务库执行前请阅读下方"回滚方法"**；
  验证/冒烟用测试库，如 `postgresql://localhost/qstock_test_118`）；
- 数据库表已初始化（`init_database` 或任一写库批执行过，幂等建表含
  sql/017 sw_board_valuation）。

## 命令

```bash
# 1. 收盘后板块快照（收盘后批 close 的 boards 段同链；单段重跑/冒烟入口）
qstock-scheduler sw-boards --date 20261023
#    → market_boards 落 industry_l1（31）+ industry_l2（124）行；
#      响应 boards.planned_absent_fields 机器可读声明申万不发布的三字段
#      （资金流/量比/领涨股，第四态废弃口径，不报故障）

# 2. 板块历史回补（申万官网 trend，一次调用=单指数全历史，1999 起）
qstock-scheduler backfill --source sw                          # 全量 19990101~前一交易日
qstock-scheduler backfill --source sw --start 20260901 --end 20261023   # 指定窗口
#    → 回补行 source='swsresearch'，与实时采集行（NULL）区分；
#      业务键幂等；逐板块断点续跑（台账 sw-backfill 前缀）；连续 10 板
#      全源失败熔断；缺省 0.4s/请求限速（155 板块全量约 2~3 分钟）

# 3. 三方位对拍（不落库）
qstock-scheduler sw-boards --date 20261023 --check
qstock-scheduler sw-boards --date 20261023 --check --level all --limit 10
#    → 逐板块比较 申万官网（current 收盘截面） vs 库内 market_boards 行
#      vs 库内自算 query_sw_board_daily；mismatches 列落库差异（对拍产出
#      非失败），官网抓取失败才 exit 1

# 4. 成分字典在线刷新
qstock-scheduler sw-dict
#    → 官网分类 xls 直链下载 → 解析 → 事务内清空重载 'sw' 体系
#      （与 import_board_classification 同一入口语义，重复执行幂等）

# 5. 次晨批估值维度（T+1，随 morning 例行）
qstock-scheduler morning --date 20261023
#    → sw_board_valuation part：申万每日分析报表（换手率/PE/PB/流通市值/
#      股息率）按 (trade_date, board_code) 幂等落库
```

参数与口径说明：

- `--date`：交易日 yyyymmdd。sw-boards 为实时语义（收盘后末次快照=当日
  收盘截面），历史日期被防线拒收（`realtime_section_no_backfill`）；
  历史数据一律走 `backfill --source sw`。
- 实时快照与历史回补的边界：current 接口的 最新价/昨收 与 trend 收盘价
  对拍一致（2026-10-24 实测 ratio=1.000000）；量纲在适配器映射层换算
  （current 成交额百万元→万元、成交量万手→股；trend 成交额亿元→万元、
  成交量亿股→股；ADR 0006/0008），换算系数经同一交易日收盘对拍核实。
- 三字段废弃：申万官网不发布 主力资金净流入/量比/领涨股——响应
  `planned_absent_fields` 与采集台账 detail 机器可读声明（issue #118，
  #113 第四态语义），列恒 NULL，日报/复盘不设对应栏目、不入 data_gaps。
- 估值维度为 T+1：当日收盘后 `sw-boards` 快照行不含估值，次晨批
  `morning` 落 `sw_board_valuation` 后按 `(trade_date, board_code)` 互查。
- 板块 5/10 日涨跌：回补完成后 market_boards 申万行历史深至 1999，
  不再受"自落库日起积累"限制（daily-review 按申万口径行计算）。

## 定时任务建议

| 任务 | 频率 | 命令 |
| --- | --- | --- |
| 收盘后批（含 boards 段） | 每交易日收盘后 | `qstock-scheduler close` |
| 次晨批（含估值维度） | 每交易日开盘前 | `qstock-scheduler morning` |
| 成分字典刷新 | 每周一次 | `qstock-scheduler sw-dict` |
| 历史回补巡检 | 按需（断点续跑幂等） | `qstock-scheduler backfill --source sw` |

launchd/cron 安装样例见 `docs/scheduler-launchd.md`；出报时点与数据源就绪
时间线见 README（申万盘中实时 / 分析报表 T+1）。

## 回滚方法

所有入口均为**业务键幂等 upsert / 事务重载**，无破坏性 DDL：

- **快照/回补行（market_boards）**：按 (trade_date, board_type,
  board_name) 业务键 upsert——重跑覆盖同键行，删除即回滚：
  ```sql
  DELETE FROM market_boards
   WHERE source = 'swsresearch' AND trade_date BETWEEN :start AND :end;
  -- 实时采集行（source IS NULL）为申万收盘快照，按 trade_date 删除即可
  ```
- **估值行（sw_board_valuation）**：
  `DELETE FROM sw_board_valuation WHERE trade_date = :day;`
- **成分字典（sw_industry_class 'sw' 体系）**：sw-dict 为"以文件为准清空
  重载"——回滚需保留旧文件副本，重导旧文件即恢复：
  `import_board_classification(source_system='sw', file_path=<旧文件>)`。
- **整体**：以上表均为新增列/新增表之外的行级数据，DROP 表不是回滚手段；
  迁移为一次性断代（东财口径与申万口径不混排比较），回滚=删除申万行并
  按旧口径重采（东财链路退役后旧口径不可再采，决策见 spec #111）。

## 验证记录

- 协议解析 fixture：`tests/fixtures/sw/`（2026-10-23/24 实抓响应裁剪），
  测试 `tests/test_sw_adapter.py` 不触网。
- 量纲对拍：2026-10-24 以 801010 收盘截面实测（current 最新价=trend
  收盘价，ratio=1.000000；成交额/成交量比恰为 100），记录在
  `src/qstock_mcp/adapters/_sw_board.py` 模块 docstring。
- 真实抓取冒烟（2026-10-24，CLI 指向测试库 `qstock_test_118`，一次性）：
  | 端点 | 命令 | 结果 |
  | --- | --- | --- |
  | current 双级快照 | `sw-boards` | rc=0，155 行落库（31 一级+124 二级），source=swsresearch，planned_absent_fields 4 项声明 |
  | trend 历史回补 | `backfill --source sw --start 20261020 --end 20261023` | rc=0，155/155 板块 ok、各 4 交易日行、零失败，source='swsresearch' |
  | 分类 xls | `sw-dict` | rc=0，12,929 行 / 5,934 股入库（含北交所 920xxx 归属） |
  | 每日分析报表 | `morning --date 20261023` | 申万估值 part 经协议测试覆盖、报表端点实抓 200（fixture 录制）；**morning 整批在空测试库未跑完**——7 大指数尾行补漏走 efinance→akshare→baostock 兜底，本机东财连接级不可达（60s 超时×14 次，spec #111 已记录），baostock 全历史补齐耗时超冒烟时限。现象为环境问题非申万缺陷；有数据的生产库指数尾行已到目标日，零补抓请求，不受影响 |
  | 三方位对拍 | `sw-boards --check` | rc=0，155 板块 mismatches=0（库内行与官网一致），dict_as_of=20261014 |
