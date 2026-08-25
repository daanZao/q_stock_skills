-- 个股日线（前复权 OHLCV），结构继承旧 appdb 实测
CREATE TABLE IF NOT EXISTS stock_daily (
    id              bigserial PRIMARY KEY,
    stock_code      text NOT NULL,
    trade_date      text NOT NULL,  -- yyyymmdd
    open            real,
    high            real,
    low             real,
    close           real,
    volume          bigint,
    amount          double precision,  -- 成交额（元）
    amplitude       real,
    change_percent  real,
    change_amount   real,
    turnover_rate   real,
    adj             text NOT NULL DEFAULT 'qfq',
    source          text,             -- 实际数据源
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    updated_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT stock_daily_code_date_adj_key UNIQUE (stock_code, trade_date, adj)
);
CREATE INDEX IF NOT EXISTS idx_stock_daily_code ON stock_daily (stock_code);
CREATE INDEX IF NOT EXISTS idx_stock_daily_date ON stock_daily (trade_date);
