-- 强势股池，结构继承旧 appdb 实测（历史 amount 同样不可信，不迁移）
CREATE TABLE IF NOT EXISTS strong_stocks (
    id                  bigserial PRIMARY KEY,
    trade_date          text NOT NULL,  -- yyyymmdd
    stock_code          text NOT NULL,
    stock_name          text,
    latest_price        real,
    change_percent      real,
    volume              bigint,
    amount              real,
    turnover_rate       real,
    market_cap          real,
    consecutive_boards  integer,
    industry            text,
    reason              text,
    created_at          timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT strong_stocks_date_code_key UNIQUE (trade_date, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_strong_stocks_code ON strong_stocks (stock_code);
CREATE INDEX IF NOT EXISTS idx_strong_stocks_date ON strong_stocks (trade_date);
