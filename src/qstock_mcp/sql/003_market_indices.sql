-- 市场指数快照，结构继承旧 appdb 实测
-- 承载：写 fetch_board_snapshot(indices 段，含白名单补漏)；读 query_board_data
CREATE TABLE IF NOT EXISTS market_indices (
    id              bigserial PRIMARY KEY,
    trade_date      text NOT NULL,  -- yyyymmdd
    index_code      text NOT NULL,
    index_name      text NOT NULL,
    latest_price    real,
    change_percent  real,
    change_amount   real,
    volume          bigint,          -- 股（ADR 0008）
    amount          real,              -- 万元（ADR 0006）
    amplitude       real,
    high            real,
    low             real,
    open            real,
    pre_close       real,
    volume_ratio    real,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT market_indices_date_code_key UNIQUE (trade_date, index_code)
);
CREATE INDEX IF NOT EXISTS idx_market_indices_code ON market_indices (index_code);
CREATE INDEX IF NOT EXISTS idx_market_indices_date ON market_indices (trade_date);
