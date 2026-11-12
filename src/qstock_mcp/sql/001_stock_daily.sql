-- legacy 个股日线（前复权 OHLCV），结构继承旧 appdb 实测
-- 承载：写 legacy fetch_daily/query_daily 自愈、init 全量回溯、legacy 模式次晨批；
-- 读 LegacyPriceReader；cutover 后改名 stock_daily_legacy 只读留档，DDL 不重建新空表。
DO $$
BEGIN
    IF to_regclass('public.stock_daily') IS NULL
       AND to_regclass('public.stock_daily_legacy') IS NULL THEN
        CREATE TABLE stock_daily (
            id              bigserial PRIMARY KEY,
            stock_code      text NOT NULL,
            trade_date      text NOT NULL,
            open            real,
            high            real,
            low             real,
            close           real,
            volume          bigint,
            amount          double precision,
            amplitude       real,
            change_percent  real,
            change_amount   real,
            turnover_rate   real,
            adj             text NOT NULL DEFAULT 'qfq',
            source          text,
            created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
            updated_at      timestamp DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT stock_daily_code_date_adj_key UNIQUE (stock_code, trade_date, adj)
        );
        CREATE INDEX idx_stock_daily_code ON stock_daily (stock_code);
        CREATE INDEX idx_stock_daily_date ON stock_daily (trade_date);
    END IF;
END $$;
