-- 龙虎榜四表，结构继承旧 appdb 实测；detail 经 FK 挂 basic
-- 命名约定：lhb=龙虎榜，yyb=营业部；yyb 两表的 fetch_date 是"抓取日"语义（appdb 实测如此），区别于 trade_date
-- lhb_stock_detail 无业务唯一键（忠于 appdb）：detail 行随父行 upsert 整体替换，不做行级 ON CONFLICT
-- lhb_basic 业务键含 lhb_reason：上游按 (股票, 上榜原因) 一行一条，同股当日可多榜单；
-- 键若仅 (trade_date, stock_code)，单批多行 INSERT ... ON CONFLICT 撞键整批回滚
-- 承载：写 fetch_board_snapshot(lhb 段)；读 query_board_data
CREATE TABLE IF NOT EXISTS lhb_basic (
    id              bigserial PRIMARY KEY,
    trade_date      text NOT NULL,  -- yyyymmdd
    stock_code      text NOT NULL,
    stock_name      text,
    close_price     real,
    change_percent  real,
    turnover_rate   real,
    lhb_reason      text NOT NULL DEFAULT '',
    net_buy_amount  real,
    buy_amount      real,
    sell_amount     real,
    total_amount    real,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT lhb_basic_date_code_reason_key UNIQUE (trade_date, stock_code, lhb_reason)
);
-- 既有库迁移（幂等，ensure_schema 每次执行）：旧键 (trade_date, stock_code) → 新键含 lhb_reason
ALTER TABLE lhb_basic ALTER COLUMN lhb_reason SET DEFAULT '';
UPDATE lhb_basic SET lhb_reason = '' WHERE lhb_reason IS NULL;
ALTER TABLE lhb_basic ALTER COLUMN lhb_reason SET NOT NULL;
ALTER TABLE lhb_basic DROP CONSTRAINT IF EXISTS lhb_basic_date_code_key;
ALTER TABLE lhb_basic DROP CONSTRAINT IF EXISTS lhb_basic_date_code_reason_key;
ALTER TABLE lhb_basic ADD CONSTRAINT lhb_basic_date_code_reason_key
    UNIQUE (trade_date, stock_code, lhb_reason);
CREATE INDEX IF NOT EXISTS idx_lhb_basic_code ON lhb_basic (stock_code);
CREATE INDEX IF NOT EXISTS idx_lhb_basic_date ON lhb_basic (trade_date);

CREATE TABLE IF NOT EXISTS lhb_stock_detail (
    id              bigserial PRIMARY KEY,
    lhb_basic_id    integer NOT NULL REFERENCES lhb_basic (id) ON DELETE CASCADE,
    trade_date      text NOT NULL,
    stock_code      text NOT NULL,
    seat_name       text,
    seat_type       text,
    amount          real,
    amount_percent  real,
    buy_count_3m    integer,
    sell_count_3m   integer,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lhb_detail_basic ON lhb_stock_detail (lhb_basic_id);
CREATE INDEX IF NOT EXISTS idx_lhb_detail_date ON lhb_stock_detail (trade_date);
CREATE INDEX IF NOT EXISTS idx_lhb_detail_seat ON lhb_stock_detail (seat_name);

CREATE TABLE IF NOT EXISTS lhb_stock_statistic (
    -- 承载（issue #114 定稿，spec 冲突修正）：库内自算——单日写路径为
    -- fetch_board_snapshot(lhb 段) 落 lhb_basic 后 recompute_lhb_statistic；
    -- 历史回溯由 replace_lhb_basic_day 原子替换 basic 并以临时 staging 重算，
    -- 两条路径均以近 60 日历日窗口（含当日，承接
    -- 原新浪 ggtj「近 60 日」窗口语义）按股聚合（交易所官方数据派生，
    -- appear=窗口行数、buy/sell/net=窗口合计万元）；DELETE+INSERT 幂等可
    -- 重跑，窗口滑出/上游修订自愈。buy/sell_seat_count 恒 NULL：官方汇总
    -- 无席位计数来源、不伪造（lhb_stock_detail 接线后才可派生）。新浪 ggtj
    -- 回归 `--check` 对拍校验位（tools_lhb._compare_statistic_snapshot），
    -- 不承载落库；trade_date 为快照交易日，业务键 (trade_date, stock_code)
    id              bigserial PRIMARY KEY,
    trade_date      text NOT NULL,  -- yyyymmdd
    stock_code      text NOT NULL,
    stock_name      text,
    appear_count_3m integer,
    buy_amount_3m   real,
    sell_amount_3m  real,
    net_buy_3m      real,
    buy_seat_count  integer,
    sell_seat_count integer,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    updated_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT lhb_stock_statistic_date_code_key UNIQUE (trade_date, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_lhb_statistic_code ON lhb_stock_statistic (stock_code);
CREATE INDEX IF NOT EXISTS idx_lhb_statistic_date ON lhb_stock_statistic (trade_date);

CREATE TABLE IF NOT EXISTS lhb_yyb_capital (
    -- 上游（东财营业部资金榜）无卖出/净额/均额列：sell_amount/net_buy_amount/avg_amount_per_trade 恒 null（不伪造）；
    -- 金额列为中文串（"X亿"/"X万"/无单位按万元），经 _money_cn 解析为万元（ADR 0006）
    id                  bigserial PRIMARY KEY,
    fetch_date          text NOT NULL,  -- yyyymmdd
    rank                integer,
    seat_name           text NOT NULL,
    total_amount        real,
    buy_amount          real,
    sell_amount         real,
    net_buy_amount      real,
    avg_amount_per_trade real,
    created_at          timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT lhb_yyb_capital_date_seat_key UNIQUE (fetch_date, seat_name)
);
CREATE INDEX IF NOT EXISTS idx_lhb_yyb_capital_date ON lhb_yyb_capital (fetch_date);
CREATE INDEX IF NOT EXISTS idx_lhb_yyb_capital_seat ON lhb_yyb_capital (seat_name);

CREATE TABLE IF NOT EXISTS lhb_yyb_most (
    -- 上游（东财营业部排行）无买卖分列：sell_amount/sell_count/net_buy_amount 恒 null（不伪造）；
    -- buy_amount 上游口径为"合计动用资金"（买+卖合计），非纯买入；金额为万元（ADR 0006）
    id              bigserial PRIMARY KEY,
    fetch_date      text NOT NULL,  -- yyyymmdd
    rank            integer,
    seat_name       text NOT NULL,
    appear_count    integer,
    buy_amount      real,
    buy_count       integer,
    sell_amount     real,
    sell_count      integer,
    net_buy_amount  real,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT lhb_yyb_most_date_seat_key UNIQUE (fetch_date, seat_name)
);
CREATE INDEX IF NOT EXISTS idx_lhb_yyb_most_date ON lhb_yyb_most (fetch_date);
CREATE INDEX IF NOT EXISTS idx_lhb_yyb_most_seat ON lhb_yyb_most (seat_name);
