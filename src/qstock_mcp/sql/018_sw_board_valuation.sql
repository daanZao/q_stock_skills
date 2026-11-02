-- 申万板块每日分析报表（估值维度，issue #118，spec #111）：换手率/PE/PB/
-- 流通市值/股息率随申万官网 index_analysis_report 入库（T+1，次晨批
-- morning 的 sw_board_valuation part 落库）。与 market_boards 分表：
-- 报表为 T+1 修订口径、含估值列；market_boards 的申万行为实时快照
-- （收盘后末次快照=当日收盘截面），两表按 (trade_date, board_code) 互查。
-- 金额单位万元（ADR 0006，上游流通市值亿元 ×1e4 换算在适配器映射层）；
-- 换手率/股息率为百分数原值。
-- 承载：写 tools_morning.fetch_morning_batch（sw_board_valuation part，
--        申万官网单源）；读 query_board_data（BOARD_QUERY_TABLES 注册）
CREATE TABLE IF NOT EXISTS sw_board_valuation (
    trade_date      text NOT NULL,   -- yyyymmdd（报表发布日 = 交易日，T+1 可得）
    board_code      text NOT NULL,   -- 申万指数代码 801xxx
    board_name      text NOT NULL,
    level           smallint NOT NULL CHECK (level IN (1, 2)),  -- 1=一级 2=二级
    close_index     real,            -- 收盘指数（与市场板块行对拍基准）
    turnover_rate   real,            -- 换手率 %
    pe_ratio        real,            -- 市盈率
    pb_ratio        real,            -- 市净率
    float_cap       real,            -- 流通市值（万元，ADR 0006）
    dividend_yield  real,            -- 股息率 %
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT sw_board_valuation_date_code_key UNIQUE (trade_date, board_code)
);
CREATE INDEX IF NOT EXISTS idx_sw_board_valuation_date ON sw_board_valuation (trade_date);
CREATE INDEX IF NOT EXISTS idx_sw_board_valuation_code ON sw_board_valuation (board_code);
