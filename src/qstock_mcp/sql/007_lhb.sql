-- 龙虎榜四表，结构继承旧 appdb 实测；detail 经 FK 挂 basic
-- 命名约定：lhb=龙虎榜，yyb=营业部；yyb 两表的 fetch_date 是"抓取日"语义（appdb 实测如此），区别于 trade_date
-- lhb_stock_detail 无业务唯一键（忠于 appdb）：detail 行随父行 upsert 整体替换，不做行级 ON CONFLICT
CREATE TABLE IF NOT EXISTS lhb_basic (
    id              bigserial PRIMARY KEY,
    trade_date      text NOT NULL,  -- yyyymmdd
    stock_code      text NOT NULL,
    stock_name      text,
    close_price     real,
    change_percent  real,
    turnover_rate   real,
    lhb_reason      text,
    net_buy_amount  real,
    buy_amount      real,
    sell_amount     real,
    total_amount    real,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT lhb_basic_date_code_key UNIQUE (trade_date, stock_code)
);
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
