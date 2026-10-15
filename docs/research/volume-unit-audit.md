# 三源日线 volume 量纲核实（issue #56）

调研日期：2026-10-15。问题：efinance / akshare / baostock 三个源的日线 `volume` 列量纲分别是什么（股 / 手）？

## 结论对照表

| 源 | 适配器调用 | 上游 | volume 单位 | amount 单位（上游原始） | 佐证 |
|---|---|---|---|---|---|
| efinance | `ef.stock.get_quote_history(klt=101, fqt=…)`（`src/qstock_mcp/adapters/efinance_adapter.py:30`） | 东财 `push2his.eastmoney.com/api/qt/stock/kline/get`，f56=成交量（`.venv/.../efinance/common/config.py:106`） | **手** | 元 | efinance 官方 docstring 示例反推（见下）+ 09 取证实测 |
| akshare | `ak.stock_zh_a_hist(period="daily", adjust=…)`（`src/qstock_mcp/adapters/akshare_adapter.py:47`） | 同一东财 kline 接口，f56→成交量（`.venv/.../akshare/stock_feature/stock_hist_em.py:981-1017`） | **手** | 元 | akshare 官方文档明示"成交量 int64 注意单位：手" |
| baostock | `bs.query_history_k_data_plus(frequency="d", …)`（`src/qstock_mcp/adapters/baostock_adapter.py:118`） | baostock 服务端 | **股** | 元 | baostock 官方知识库明示"volume 成交数量 单位：股"；其示例数据可反推验证 |

三源 amount 上游均为元，适配器统一经 `_wan` 换算为万元（ADR 0006）；volume 三源均原样透传（`_int()`，无换算），落库 `volume bigint`（`src/qstock_mcp/sql/001_stock_daily.sql:10`，schema 无单位注释）。**东财系（efinance/akshare）为手、baostock 为股，混源差 100 倍，与 09 取证（`files/09-historical-review-hardening.md` 第四节 W3）一致，akshare 缺口由此补齐。**

## 佐证细节

### efinance = 手

efinance 无单位文字声明，但其官方 docstring 自带可反推的示例（`.venv/lib/python3.12/site-packages/efinance/stock/getter.py:239`）：

> 贵州茅台 600519，2021-07-23：收盘 1900.00，成交量 47585，成交额 9.057762e9（元）

反推：9,057,762,000 / 47,585 ≈ 190,348 元 —— 若成交量单位为股则均价 19 万元/股，荒谬；若单位为手：9,057,762,000 / (47,585×100) ≈ 1903.5 元 ≈ 当日均价区间。**成交量单位为手。**

另证：efinance 与 akshare 调同一东财 kline 接口、同一字段（f56），akshare 官方文档对该字段明示"手"（见下），同源同字段必同单位。

### akshare = 手

akshare 官方文档 `stock_zh_a_hist` 输出参数表（[AKShare 股票数据文档](https://akshare.akfamily.xyz/data/stock/stock.html)，"历史行情数据-东财"节）：

- 成交量 int64 —— **注意单位：手**
- 成交额 float64 —— 注意单位：元

代码层面，`stock_zh_a_hist` 直请求 `push2his.eastmoney.com/api/qt/stock/kline/get`（`stock_hist_em.py:981`），`f56` 原样进"成交量"列，无任何换算。

### baostock = 股

baostock 官方知识库（[baostock.com 知识库 · query_history_k_data_plus](https://baostock.com/mainContent?file=)）"历史行情指标参数 → 日线指标参数"表：

- volume 成交数量 —— **单位：股**
- amount 成交金额 —— 精度小数点后 4 位；单位：人民币元

其文档自带示例可反推自验：sh.600000 浦发银行 2017-07-03，volume=38,778,949，amount=486,264,672 元，486,264,672 / 38,778,949 ≈ 12.54 元 ≈ 当日收盘 12.56 —— 股口径成立。

### 库内实测交叉验证（09 取证，002813 路畅科技）

| 日期 | source | volume | amount（万元） | 反推 |
|---|---|---|---|---|
| 20260928 | efinance | 59,586 | 14,372.9 | 59,586 手 ×100 股 × ≈24.1 元 ≈ 1.44 亿元 ✓（手） |
| 20261014 | baostock | 10,632,095 | 29,365.7 | 10,632,095 股 × ≈27.6 元 ≈ 2.94 亿元 ✓（股） |

## 本次在线实测记录（2026-10-15）

计划用三库各取 600519 最近同一交易日日线交叉验证，**全部不可达，实测未能执行**：

- efinance / akshare：`push2his.eastmoney.com` 及 `1./17./33./61./90.push2his` 全部镜像 curl 均 HTTP 000（连接被对端重置，`RemoteDisconnected`）——东财 push2 集群故障仍在持续。
- baostock：`bs.login()` 连续 5 次返回"网络接收错误"（首次尝试曾登录成功但查询时 `接收数据异常` Broken pipe）——baostock 服务端当前亦不可用。

结论改由官方文档 + 库内 docstring 示例 + 09 取证三方互证，三个来源独立指向同一答案。待网络恢复后建议补一次三源同日交叉实测（脚本即本调查所用方法：同股同日取 volume/amount/close，比较 `amount/volume` 与 `amount/(volume×100)` 哪个落在当日价格区间）。

## 对本仓库的含义

- `map_eastmoney_rows`（`_eastmoney.py:67`）与 `map_baostock_rows`（`baostock_adapter.py:93`）对 volume 都是 `_int()` 原样透传，东财系落库为手、baostock 落库为股，`stock_daily.volume` 混源时量纲跳变 100 倍。
- ADR 0006 只规约了 amount（万元），volume 无规格 —— 需要按 09 W3 建议补单位决策 ADR 并在适配器层归一（统一为股可使 `amount ≈ close × volume` 恒等式直接可用作质检）。
- 连带影响面：`q_stock_analysis/data_provider/` 的 efinance/akshare/baostock fetcher 同样原样透传 volume（各自 `_normalize_data` 只改列名不换算），下游若跨项目消费需注意同一坑。
