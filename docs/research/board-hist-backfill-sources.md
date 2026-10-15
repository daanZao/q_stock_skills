# 板块历史行情回补：上游源可行性调研（issue #55）

- **日期**：2026-10-15
- **背景**：push2 集群（`push2.eastmoney.com` / `push2his.eastmoney.com`）自 2026-10-14 持续故障，market_boards 实时采集中断。本调研回答：**哪些上游接口能提供历史日期的行业/概念板块行情，用于 market_boards 历史回补**。只评估"历史回补"用途，不涉及实时兜底源。
- **方法**：读 akshare 1.18.94 / efinance 0.5.9 本地包源码定位 URL；curl 实测各端点；web 搜索补充。所有实测命令与结果见末节。
- **对照口径**：`market_boards`（`src/qstock_mcp/sql/004_market_boards.sql`）现有字段：trade_date / board_type / board_code(BK) / board_name / latest_price / change_percent / change_amount / volume / amount(万元, ADR 0006) / amplitude / high / low / open / pre_close / volume_ratio / stock_count / leading_stock / leading_change。当前实时采集（`map_board_rows`，`src/qstock_mcp/adapters/_board_em.py:54`）只填 latest_price/change_percent/change_amount/leading_*，volume/amount/OHLC 等本就为 None。

## 结论（推荐）

1. **故障期内立即可用的回补源：同花顺 `d.10jqka.com.cn` 板块指数年 K 线文件**（akshare `stock_board_industry_index_ths` / `stock_board_concept_index_ths` 底层）。实测 200、免鉴权、数据更新至 2026-10-14（即故障首日），行业+概念双覆盖，可补回 10-14 起的缺口。代价：同花顺板块体系（行业 881xxx、概念 885xxx），与东财 BK 体系口径不同，需名称映射且不可与东财口径混排。
2. **口径最一致的长期回补源：push2his 恢复后用 akshare `stock_board_industry_hist_em` / `stock_board_concept_hist_em`**（东财 BK 代码、字段最全）。当前故障期实测不可用（000）。
3. **口径一致的付费备选：Tushare `dc_daily`**（东财板块代码 `BKxxxx.DC`，支持按 trade_date 取全板块截面，历史自 2020，需 6000 积分）。
4. **行业维度权威备选：申万官网 `swsresearch.com` index_publish/trend**（akshare `index_hist_sw`），实测 200、数据至 2026-10-14、深度至 1999，但只有申万行业指数（801xxx），无概念板块，口径与东财行业板块不同。
5. 排除：datacenter-web（无板块行情历史报表）、腾讯（无公开板块 K 线）、新浪（板块只有实时快照，历史 K 线返回 null）、baostock（本次实测登录即网络错误，且无板块指数产品）、efinance（历史 K 线同样走 push2his）。

## 候选源逐项核实

### 1. akshare `stock_board_industry_hist_em` / `stock_board_concept_hist_em`（东财口径）

源码定位（本地 .venv，akshare 1.18.94）：

- `stock_board_industry_hist_em`（`akshare/stock/stock_board_industry_em.py:261`）→ `http://7.push2his.eastmoney.com/api/qt/stock/kline/get`，`secid=90.{BK代码}`，`klt=101/102/103`（日/周/月），`beg/end` 限定区间。
- `stock_board_concept_hist_em`（`akshare/stock/stock_board_concept_em.py:182`）→ `https://91.push2his.eastmoney.com/api/qt/stock/kline/get`，参数同上。

两接口都属 **push2his 集群**。实测（2026-10-15）`push2his.eastmoney.com`、`7.push2his`、`91.push2his` 对板块 K 线请求均 **HTTP 000**（连接失败，非限流）——**当前故障期不可用**。另注意：若 symbol 传板块名而非 `BK\d+` 代码，两函数会先调东财板块名单接口（push2 集群）做名称→代码映射，故障期连映射都走不通；回补实现应直接传 BK 代码绕过。

- 字段：日期、开盘、收盘、最高、最低、涨跌幅、涨跌额、成交量、成交额、振幅、换手率（11 列，比 market_boards 实时口径还全）。
- 历史深度：板块指数发布以来（单板块 beg/end 任意区间，`lmt=1000000`）。
- 粒度：**单板块逐段**，回补 N 板块 × 区间需 N 次请求。
- 频率/稳定性：东财 web 接口无官方限速承诺，akshare 默认无 sleep；主要风险即当前的集群级故障。
- efinance 0.5.9 的 `get_quote_history` 历史 K 线同样打 `push2his.eastmoney.com/api/qt/stock/kline/get`（`efinance/common/getter.py:150`），故障期同样不可用。

### 2. 同花顺 `d.10jqka.com.cn` 板块指数年 K 线（akshare `stock_board_industry_index_ths` / `stock_board_concept_index_ths`）

源码定位：`akshare/stock_feature/stock_board_industry_ths.py:121`、`stock_board_concept_ths.py:124`。两函数最终请求：

```
GET https://d.10jqka.com.cn/v4/line/bk_{板块代码}/01/{年份}.js
```

akshare 实现里带 hexin-v cookie（`ths.js` 由 py_mini_racer 算出），但**实测不带 cookie、不带 Referer 也直接返回 200 + 真实数据**（见末节）。代码映射：行业代码（881xxx）取自 `https://q.10jqka.com.cn/thshy/detail/code/881272/` 页内分类链接（实测 200）；概念代码（885xxx 等）取自概念详情页 `<input id="clid">`。

实测要点：

- `bk_881272`（软件开发，行业）2026 年文件数据**最新至 20261014**，含 OHLC/成交量/成交额；`bk_885531`（概念）同样可用。
- 历史深度：`last.js` 显示 881272 自 2018-11-26（指数发布日）起，按年分文件；2024 年文件实测可取。
- 连发 12 请求全部 200，未见即刻限流。

- 字段：日期、开、高、低、收、成交量（股）、成交额（元）。**无涨跌幅/涨跌额/换手率/振幅**（涨跌幅可由收盘派生；换手率/振幅无法回补，保持 None）。
- 粒度：单板块 × 单年一个文件，回补效率高（一年一次请求）。
- 口径：同花顺板块体系，成分与指数算法与东财不同，**点位数值与东财板块"最新价"不可比**；行业名大多与东财一致可映射，概念名存在出入（需映射表+人工兜底）。板块数：THS 行业约 90 个，概念数百个，与东财口径覆盖不同。
- 频率/稳定性风险：静态 JS 文件、CDN 性质，短期稳定；但属未公开承诺的 web 接口，THS 可能随时加 hexin-v 强制校验或限频——实现应带限速（如 ≤2 req/s）+ 重试 + 失败留台账。

### 3. datacenter-web.eastmoney.com

实测 200（`reportName=RPT_VALUEANALYSIS_DET` 正常返回）。但该报表族覆盖龙虎榜/大宗交易/解禁/北向/融资融券/股东/分红等，**无板块行情历史报表**；东财 web 端板块相关数据（板块排行、板块 K 线、板块资金流历史）分别指向 push2 clist、push2his kline、push2his fflow/daykline——全部在故障集群上。旁证：[bytexlee/stock](https://github.com/bytexlee/stock) 的接口分工表同样将板块类归入 push2、datacenter 报表族不含板块行情。**排除**。

### 4. 腾讯 / 新浪

- 腾讯：`qt.gtimg.cn/q=sh600519` 实测 200（个股实时），但 `qt.gtimg.cn/q=shBK1027` 返回 `v_pv_none_match="1"`、`web.ifzq.gtimg.cn/.../fqkline/get?param=shBK1027,...` 返回 `param error`——**无公开板块 K 线接口，排除**。
- 新浪：`vip.stock.finance.sina.com.cn/q/view/newSinaHy.php` 实测 200，提供新浪行业板块**实时快照**（new_* 代码族）；但 `CN_MarketData.getKLineData?symbol=new_blhy` 返回 `null`——**板块无历史 K 线，排除**。

### 5. 申万官网 `swsresearch.com`（akshare `index_hist_sw`，行业维度备选）

源码：`akshare/index/index_research_sw.py:29` → `GET https://www.swsresearch.com/institute-sw/api/index_publish/trend/?swindexcode=801010&period=DAY`。

实测 200，申万一级行业 801010（农林牧渔）数据**最新至 2026-10-14**，深度至 1999-12-30，支持日/周/月。字段：开/高/低/收/成交量/成交额（hike/markup 为涨跌额/涨跌幅）。官方接口、稳定性好。限制：**只有申万行业指数体系（801xxx.SI），无概念板块**，与东财行业板块口径不同。适合作为"行业维度历史序列"的权威备选，不适合直接回补 market_boards 的东财口径行。

### 6. Tushare `dc_daily`（东财口径，付费备选）

[官方文档](https://tushare.pro/document/2?doc_id=382)：概念/行业/地域板块行情，板块代码 `BKxxxx.DC`（**即东财板块代码**），字段 ts_code/trade_date/open/close/high/low/change/pct_change/vol（股）/amount（元）/swing/turnover_rate/category。历史自 2020 年起；支持按 `trade_date` 取**全板块单日截面**（单次 ≤2000 行）——与"回补某个历史日期的 market_boards 全表"的需求形状完全吻合，且口径与现有东财实时数据一致。门槛：**需 6000 积分**（付费/任务获取），第三方依赖。未实测（需 token）。

### 7. baostock

本次实测 `bs.login()` 连续 3 次返回 `10002007 网络接收错误`（Broken pipe），主机侧不可达（注：10-15 早些时候的会话曾成功取数，疑似本机当前网络/对端波动，待复测）。且 baostock 产品线为个股/指数/基本面，**无板块指数历史**。即使可达也不是板块回补源。**排除**。

## 口径差异与落库注意（相对实时 market_boards）

| 项 | 实时（东财 push2） | push2his hist_em | d.10jqka THS | swsresearch 申万 | tushare dc_daily |
|---|---|---|---|---|---|
| 板块体系 | 东财 BK | 东财 BK（同口径） | 同花顺 881/885 | 申万 801 | 东财 BK（同口径） |
| 粒度 | 全板块截面 | 单板块区间 | 单板块×年 | 单指数全历史 | 全板块×单日 |
| OHLC | 无（None） | 有 | 有 | 有 | 有 |
| 涨跌幅/涨跌额 | 有 | 有 | 可派生 | 有 | 有 |
| 换手率/振幅 | 无 | 有 | 无 | 无（振幅可派生） | 有 |
| 成交额单位 | —（None） | 元→需转万元(ADR 0006) | 元→转万元 | bargainsum 单位需核实 | 元→转万元 |
| 领涨股/涨跌家数 | 有 | 无 | 无 | 无 | 无 |

要点：

1. **领涨股/上涨家数等截面字段任何历史源都无法回补**，回补行这些字段保持 None（符合"不伪造"原则）。
2. THS/申万路径回补的行是**异口径数据**（不同板块体系的指数），不可与东财口径行在同一板块名下混排比较；market_boards 目前**无 source 列**（004 sql 注释已注明），若采纳 THS 回补，建议加 source 列或在 board_code/board_name 上可区分（如 board_code 存 THS 代码），否则造成静默混口径。
3. push2his/tushare 路径为同口径回补，无此问题；其中 push2his 免费但依赖故障集群恢复，tushare 绕过故障但有积分门槛。
4. amount 落库统一万元（ADR 0006），各源返回单位（元/ bargainsum）需在适配层换算。

## 实测记录（2026-10-15，curl / python）

```bash
# push2/push2his 集群：全部 000（连接失败）
curl -s -m 10 -o /dev/null -w "%{http_code}\n" \
  "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.BK1027&fields1=f1,f2,f3&fields2=f51,f52,f53&klt=101&fqt=0&beg=20261001&end=20261015"   # 000
curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://7.push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.BK1027&..."   # 000
curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://91.push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.BK1027&..."  # 000
curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://17.push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&fs=m:90+t:2+f:!50&fields=f12,f14,f2,f3"  # 000
curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://push2.eastmoney.com/api/qt/clist/get?..."  # 000

# 非 push2 域：全部 200
curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEANALYSIS_DET&columns=ALL&pageSize=1"  # 200
curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://quote.eastmoney.com/center/boardlist.html"  # 200（仅 HTML 页，数据仍走 push2）
curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://q.10jqka.com.cn/thshy/detail/code/881272/"  # 200
curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://d.10jqka.com.cn/v4/line/bk_881272/01/2026.js"  # 200
curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://qt.gtimg.cn/q=sh600519"  # 200

# 同花顺板块年 K 线：免 cookie/Referer 返回真实数据，最新至 20261014
curl -s -m 10 "https://d.10jqka.com.cn/v4/line/bk_881272/01/2026.js" | head -c 600
# quotebridge_v4_line_bk_881272_01_2026({"data":"20260105,2294.232,2346.574,...;...;20261014,1884.254,1884.254,1855.491,1856.273,1611443200,27065555000.000,,,,0"})
curl -s -m 10 "https://d.10jqka.com.cn/v4/line/bk_881272/01/last.js" | head -c 300
# {"year":{"2018":25,...,"2026":186},"total":"1910","start":"20181126","name":"软件开发",...}  ← 深度自 2018-11-26
curl -s -m 10 "https://d.10jqka.com.cn/v4/line/bk_885531/01/2026.js" | head -c 300   # 概念板块同样可用
for i in $(seq 1 12); do curl -s -m 8 -o /dev/null -w "%{http_code} " "https://d.10jqka.com.cn/v4/line/bk_881272/01/202$((i%3+4)).js"; done  # 12×200 无限流

# 腾讯：无板块 K 线
curl -s -m 10 "https://qt.gtimg.cn/q=shBK1027"                        # v_pv_none_match="1"
curl -s -m 10 "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=shBK1027,day,2026-10-01,2026-10-15,10,qfq"  # {"code":0,"msg":"param error","data":[]}

# 新浪：板块仅实时快照，历史 K 线 null
curl -s -m 10 "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"  # 200，new_* 行业实时快照
curl -s -m 10 "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=new_blhy&scale=240&ma=no&datalen=5"  # null

# 申万官网：200，数据至 2026-10-14，深度至 1999
curl -s -m 15 -k "https://www.swsresearch.com/institute-sw/api/index_publish/trend/?swindexcode=801010&period=DAY" | head -c 300
# {"code":"200","message":"ok","data":[{"swindexcode":"801010","bargaindate":"1999-12-30",...
# 尾行：{"bargaindate":"2026-10-14","openindex":2550.63,...,"closeindex":2528.82,"hike":-27.25,"markup":-1.07,...}

# baostock：登录即失败
.venv/bin/python -c "import baostock as bs; print(bs.login().error_msg)"   # 10002007 网络接收错误（×3 重试同）
```

## 源码引用

- `akshare/stock/stock_board_industry_em.py:261`（hist_em，URL 在函数体内）；`akshare/stock/stock_board_concept_em.py:182`
- `akshare/stock_feature/stock_board_industry_ths.py:121`；`akshare/stock_feature/stock_board_concept_ths.py:124`
- `akshare/index/index_research_sw.py:29`
- `efinance/common/getter.py:150`（历史 K 线同样走 push2his）
- `src/qstock_mcp/sql/004_market_boards.sql`（market_boards 结构，无 source 列）；`src/qstock_mcp/adapters/_board_em.py:54`（实时口径字段）
