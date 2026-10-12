---
name: indicator-tools
description: 证券指标计算与序列数学工具（MA/MACD/BOLL/RSI/窗口极值/最大回撤/N 日涨跌幅/一阶二阶导数/相关性矩阵/量价判定/晋级率）。管道版：stdin 吃 MCP query_daily 工具的 JSON 输出（corr/promote 为各自专用输入），stdout 吐指标 JSON；纯标准库，不访问数据库，现算现用不物化。
---

# indicator-tools

工具函数 skill：指标按"名称 + 计算方法 + 使用方法"目录式组织。输入为 `query_daily` 工具输出的 JSON（stdin），输出为标准化指标 JSON（stdout）；纯 Python 标准库，无第三方依赖，任何能跑 Python 3.11+ 的 agent 环境可用（与仓库 pyproject 口径一致）。

## 调用方式

```bash
<query_daily 输出 JSON> | python scripts/indicators.py --indicator <指标名> [参数...]
```

典型组合：agent 调用 MCP `query_daily` 拿到日线 JSON 后，直接作为本脚本的 stdin（可经临时文件或 heredoc 传递）。

公共参数：`--tail N`（输出末尾 N 个点，默认 60，0=全部）。

**退出码**：

- `0`：`status=ok` 或 `status=insufficient_data`（数据不足**不是错误**）
- `1`：输入/运行时错误（stdin 非法 JSON、上游 `status!=ok`、行数据缺列或非数值）
- `2`：参数错误（argparse 校验失败、`--on` 等取值非法）

**输入契约**（`query_daily` 输出结构，属跨仓库稳定契约，见 ADR 0001）：

```json
{
  "status": "ok",
  "tool": "query_daily",
  "params": {"stock_code": "600519", "adj": "qfq", "days": null, "start": null, "end": null},
  "range": {"start": "...", "end": "..."},
  "data_range": {"start": "...", "end": "..."},
  "count": 250,
  "healed": [],
  "rows": [
    {"trade_date": "...", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
     "volume": 1.0, "amount": 1.0, "amplitude": null, "change_percent": null,
     "change_amount": null, "turnover_rate": null, "source": "..."}
  ]
}
```

`rows` 按 `trade_date` 升序；`open/high/low/close/volume/amount` 须为数值（缺失视为输入错误，exit 1）。`stock_code` 从 `params` 回显到输出。

**例外输入**：`corr` 与 `promote` 不吃单标的 `rows`，输入形状见各自条目；公共校验不变（须为 JSON 对象且 `status="ok"`，否则 exit 1）。

**输出契约**：`status=ok` 时含 `stock_code`、`indicator`、`params`（参数回显）、`data_range`、`required_length`/`available_length`、`boundary_rule`、`series`（每点含 `trade_date`，数值保留 4 位小数，头部按边界规则为 null）。`status=insufficient_data` 时明确给出 `required_length` 与 `available_length`，exit 0。

**前置**：调用 `query_daily` 时取够长度（MACD 至少 35 根，EMA 收敛建议 ≥200 根；rolling/maxdd/chg 默认窗口 250）。数据不足时本脚本只报 `insufficient_data`，需要 agent 用更大的 `days`/`start` 重新查询。

## 指标目录

口径基线：旧系统 `data_provider/base.py` 的 `calculate_indicators`。边界规则（null 头部）：diff1 首点 null、diff2 前两点 null；MA/BOLL/rolling/maxdd 前 `窗口-1` 点 null；RSI 前 `周期-1` 点 null；MACD 的 DIF 前 `slow-1` 点、DEA/BAR 前 `slow+signal-2` 点 null；chg 前 `窗口` 点 null；vol_price 首点 null（环比需前一交易日）。每次输出的 `boundary_rule` 字段会回显本指标的具体规则。

### MA — 简单移动平均线

- **计算方法**：`MA_N = mean(x, 窗口 N)`，默认周期 5/10/20/60。
- **使用方法**：`--indicator ma [--periods 5,10,20,60] [--on close]`。输出列 `ma5`/`ma10`/...；`--on volume` 即均量（如 SEPA 的 20 日均量），输出列 `ma20_volume`。判断趋势方向、多空排列、支撑压力位。

### MACD — 指数平滑异同移动平均线

- **计算方法**：`DIF = EMA(close,12) - EMA(close,26)`（`ewm(span, adjust=False, min_periods=窗口)`）；`DEA = EMA(DIF,9)`；`BAR = (DIF - DEA) * 2`。参数 `--fast/--slow/--signal` 默认 12/26/9。
- **使用方法**：`--indicator macd`。输出列 `dif`/`dea`/`bar`。DIF 上穿 DEA 为金叉、下穿为死叉；BAR 正负与缩放判断动能强弱与背离。

### BOLL — 布林带

- **计算方法**：`MID = MA(close,20)`；`STD = 窗口标准差(ddof=0)`；`UPPER = MID + 2*STD`，`LOWER = MID - 2*STD`。参数 `--period`（默认 20）、`--k`（默认 2）。
- **使用方法**：`--indicator boll`。输出列 `mid`/`upper`/`lower`。价格触及上轨为强势/超买参考，触及下轨为弱势/超卖参考；带宽收窄预示变盘。

### RSI — 相对强弱指标

- **计算方法**：Wilder 平滑（`ewm(alpha=1/N, adjust=False, min_periods=N)`）：`RS = avg_gain / avg_loss`，`RSI = 100 - 100/(1+RS)`。默认周期 6/12/24。
- **使用方法**：`--indicator rsi [--periods 6,12,24]`。输出列 `rsi_6`/`rsi_12`/`rsi_24`。RSI>80 超买、<20 超卖（A 股短线常用 6 日）。

### diff1 / diff2 — 序列一阶/二阶导数（离散差分）

- **计算方法**：`diff1[i] = x[i] - x[i-1]`；`diff2 = diff1 再差分`。边界规则：**diff1 首元素为 null，diff2 首两个元素为 null**（`--on maN` 时 MA 的头部 null 会顺移），输出长度与输入一致。
- **使用方法**：`--indicator diff1|diff2 --on <序列>`，`--on` 支持 `open`/`high`/`low`/`close`/`volume`/`amount`/`maN`（如 `ma20`，内部先算 MA 再求导）。输出列如 `diff1_close`。一阶看斜率方向（上升/下降），二阶看加速度（趋势拐点、背离确认）。

### rolling_max / rolling_min — 窗口极值

- **计算方法**：窗口内最大/最小值（`min_periods=窗口`）；默认窗口 250（≈52 周）。
- **使用方法**：`--indicator rolling_max --window 250 [--on close]`。52 周最高/最低价（SEPA 趋势模板第 6/7 条）。

### maxdd — 窗口最大回撤

- **计算方法**：`dd = (x / 窗口内最高 - 1) * 100`，取窗口内 dd 最小值（负值百分比）。
- **使用方法**：`--indicator maxdd --window 250`。动量风险验证（SEPA 要求最大回撤 ≤15%）。

### chg — N 日涨跌幅

- **计算方法**：`(x / x[N 日前] - 1) * 100`，仅在序列末尾有判断意义。
- **使用方法**：`--indicator chg --window 250`。个股/指数区间收益对比（RS 相对强度近似：个股 250 日 chg vs 沪深300 同期 chg）。

### corr — 多标的相关性矩阵

- **计算方法**：多标的按 `trade_date` 交集对齐后（任一侧取值为 null 的交易日剔除），两两计算 Pearson 相关系数。方差为 0 的标的（如一字横盘）对应矩阵位置为 null。**窗口由调用方传入的序列长度控制**（20 日窗口就传 20 根），`--tail` 不适用。
- **使用方法**：`--indicator corr [--on change_percent]`。输入为多标的对象，每个标的值就是一份 `query_daily` 输出（至少含 `rows`）：

```json
{"status": "ok",
 "series": {"600519": {"status": "ok", "rows": [...]},
            "399001": {"status": "ok", "rows": [...]}}}
```

至少 2 个标的、交集至少 2 个交易日，否则分别 exit 1 / `insufficient_data`（exit 0）。输出含 `symbols`（排序后代码表）、`aligned_length`、`data_range` 与 `matrix`（`{行代码: {列代码: 相关系数}}`，4 位小数，对称、对角线为 1）。`--on` 支持 `open`/`high`/`low`/`close`/`volume`/`amount`/`maN`，另支持 corr 专用的 `change_percent`（涨跌幅序列——**指数/个股相关性的标准口径**，`query_daily` 与 `query_index_daily` 的 rows 均含此列；区间内 null 点从对齐交集剔除）。典型用途：个股与指数/板块的相关性、组合内标的共振检查。

### vol_price — 量价背离/配合判定

- **计算方法**：逐日算价格环比 `price_chg = (close/前收-1)*100` 与成交额环比 `amount_chg = (amount/前额-1)*100`；按阈值 θ（`--threshold`，小数，默认 0.2=20%）分量档：`amount_chg >= θ*100` 放量，`<= -θ*100` 缩量，其间平量。形态 = 量档 + 价向：**放量上涨/缩量上涨/平量上涨/放量下跌/缩量下跌/平量下跌/平盘**。`divergence=true` 仅当**缩量上涨**（价升量减，经典量价背离）；放量上涨/缩量下跌为量价配合。首点无环比为 null。
- **使用方法**：`--indicator vol_price [--threshold 0.2]`（输入即标准 `query_daily` 输出，`close`/`amount` 列）。输出列 `price_chg`/`amount_chg`（%，4 位小数）、`vol_price`（形态标签）、`divergence`（布尔）。至少需要 2 根日线。

### promote — 晋级率与连板梯队

- **计算方法**：`promote_rate = 昨日连板股中今日仍涨停（出现在 today.boards）的家数 / 昨日连板家数 * 100`；`ladder` 为今日各连板高度（`limit_count`）的家数分布，`ladder_detail` 给出各高度的具体代码。昨日连板为空时晋级率无意义，报 `insufficient_data`（exit 0）；今日为空则 `promote_rate=0`、`ladder={}`。
- **使用方法**：`--indicator promote`。输入为昨日/今日两个交易日的连板数据：**`yesterday.boards` 只含昨日连板股（`limit_count >= 2`，由调用方从涨停池中筛出，首板不进分母）；`today.boards` 为今日全部涨停股**（是否晋级只看今日仍涨停，与今日几板无关）：

```json
{"status": "ok",
 "yesterday": {"trade_date": "...", "boards": [{"stock_code": "600001", "limit_count": 2}]},
 "today":     {"trade_date": "...", "boards": [{"stock_code": "600001", "limit_count": 3}]}}
```

`stock_code` 须为非空字符串、`limit_count` 须为正整数，否则 exit 1。输出含 `promote_rate`（%，4 位小数）、`promoted`/`failed`（晋级/断板代码列表，保持昨日输入顺序）、`ladder`/`ladder_detail`。

> 新增指标不得改变既有指标的契约（输出结构、边界规则、退出码）。

## 测试

```bash
python -m pytest skills/indicator-tools/tests/ -v
```

含已知数据集数值 fixture、边界规则（null 头部）、数据不足语义（exit 0 + required/available 长度）、参数错误退出码（exit 2）、无数据库行为断言。
