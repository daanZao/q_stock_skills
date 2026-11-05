-- 复权影子表（issue #160）：raw + 累计 B → 物化 hfq + 每股状态；qfq 不落表。
-- 承载：写 rebuild_stock 单票竖切（morning #161、adj-backfill #163 共用）；
-- 读 adjustment 快照、NewPriceReader 与后续统一价格接缝。
-- adj-backfill 额外写 collection_log 的 stock_adjust_factor/stock_daily_raw/
-- stock_factor_repair/stock_factor_retracted 四类审计行（断点续跑、待修复
-- 台账与事件撤销标记）。
CREATE TABLE IF NOT EXISTS stock_daily_raw (
    stock_code      text NOT NULL,
    trade_date      text NOT NULL,
    open            double precision,
    high            double precision,
    low             double precision,
    close           double precision,
    preclose        double precision,
    volume          bigint,
    amount          double precision,
    amplitude       double precision,
    change_percent  double precision,
    change_amount   double precision,
    turnover_rate   double precision,
    tradestatus     smallint,
    source          text NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stock_code, trade_date)
);

CREATE TABLE IF NOT EXISTS stock_adjust_factor (
    stock_code    text NOT NULL,
    ex_date       text NOT NULL,
    factor_cum    numeric(18,10) NOT NULL,
    kind          text,
    detail        text,
    reg_date      text,
    source        text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stock_code, ex_date),
    CONSTRAINT stock_adjust_factor_positive_finite
        CHECK (factor_cum > 0::numeric AND factor_cum < 'Infinity'::numeric)
);

CREATE TABLE IF NOT EXISTS stock_daily_hfq (
    stock_code      text NOT NULL,
    trade_date      text NOT NULL,
    open            double precision,
    high            double precision,
    low             double precision,
    close           double precision,
    volume          bigint,
    amount          double precision,
    turnover_rate   double precision,
    tradestatus     smallint,
    factor_cum      numeric(18,10) NOT NULL,
    rebuilt_at      timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stock_code, trade_date),
    CONSTRAINT stock_daily_hfq_factor_positive_finite
        CHECK (factor_cum > 0::numeric AND factor_cum < 'Infinity'::numeric)
);

CREATE TABLE IF NOT EXISTS stock_factor_state (
    stock_code         text PRIMARY KEY,
    baseline_factor    numeric(18,10),
    factor_latest      numeric(18,10),
    latest_trade_date  text NOT NULL,
    rebuild_pending    boolean NOT NULL DEFAULT false,
    last_rebuilt_at    timestamptz,
    CONSTRAINT stock_factor_state_value_shape CHECK (
        (baseline_factor IS NULL) = (factor_latest IS NULL)
        AND (rebuild_pending OR baseline_factor IS NOT NULL)
        AND (baseline_factor IS NULL OR
             (baseline_factor > 0::numeric AND baseline_factor < 'Infinity'::numeric))
        AND (factor_latest IS NULL OR
             (factor_latest > 0::numeric AND factor_latest < 'Infinity'::numeric))
    )
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_raw_date ON stock_daily_raw (trade_date);
CREATE INDEX IF NOT EXISTS idx_stock_adjust_factor_date ON stock_adjust_factor (ex_date);
CREATE INDEX IF NOT EXISTS idx_stock_daily_hfq_date ON stock_daily_hfq (trade_date);
