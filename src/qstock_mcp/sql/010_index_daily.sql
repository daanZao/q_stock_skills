-- 主要指数日线（issue #8）：指数代码与个股代码冲突（000001 既是上证指数又是
-- 平安银行），不复用 stock_daily，独立建表；指数无复权概念，无 adj 列
CREATE TABLE IF NOT EXISTS index_daily (
    id              bigserial PRIMARY KEY,
    index_code      text NOT NULL,
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
    source          text,             -- 实际数据源
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    updated_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT index_daily_code_date_key UNIQUE (index_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_index_daily_code ON index_daily (index_code);
CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily (trade_date);
