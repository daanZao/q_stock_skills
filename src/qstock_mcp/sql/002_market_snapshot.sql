-- 全市场快照（一只一行），结构继承旧 appdb 实测
CREATE TABLE IF NOT EXISTS market_snapshot (
    id              bigserial PRIMARY KEY,
    trade_date      text NOT NULL,  -- yyyymmdd
    stock_code      text NOT NULL,
    stock_name      text,
    latest_price    real,
    change_percent  real,
    change_amount   real,
    amplitude       real,
    high            real,
    low             real,
    open            real,
    pre_close       real,
    volume_ratio    real,
    turnover_rate   real,
    pe_ratio        real,
    pb_ratio        real,
    volume          bigint,
    amount          double precision,
    market_cap      double precision,
    float_cap       double precision,
    source          text,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT market_snapshot_date_code_key UNIQUE (trade_date, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_market_snapshot_code ON market_snapshot (stock_code);
CREATE INDEX IF NOT EXISTS idx_market_snapshot_date ON market_snapshot (trade_date);
