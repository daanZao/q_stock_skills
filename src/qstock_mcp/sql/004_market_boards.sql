-- 板块快照（行业/概念），结构继承旧 appdb 实测（注意：无 source 列）
CREATE TABLE IF NOT EXISTS market_boards (
    id              bigserial PRIMARY KEY,
    trade_date      text NOT NULL,  -- yyyymmdd
    board_type      text NOT NULL,  -- industry | concept
    board_code      text,
    board_name      text NOT NULL,
    latest_price    real,
    change_percent  real,
    change_amount   real,
    volume          bigint,
    amount          real,
    amplitude       real,
    high            real,
    low             real,
    open            real,
    pre_close       real,
    volume_ratio    real,
    stock_count     integer,
    leading_stock   text,
    leading_change  real,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT market_boards_date_type_name_key UNIQUE (trade_date, board_type, board_name)
);
CREATE INDEX IF NOT EXISTS idx_market_boards_name ON market_boards (board_name);
CREATE INDEX IF NOT EXISTS idx_market_boards_date ON market_boards (trade_date);
CREATE INDEX IF NOT EXISTS idx_market_boards_type ON market_boards (board_type);
