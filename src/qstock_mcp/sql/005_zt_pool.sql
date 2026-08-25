-- 涨跌停/炸板池，结构继承旧 appdb 实测
-- 表名/枚举约定：zt=涨停 dt=跌停 zb=炸板（pool_type 取值）
-- 注意：旧库历史 amount 列数据损坏（等于 volume），新项目由本服务写入，不迁移旧数据
CREATE TABLE IF NOT EXISTS zt_pool (
    id                  bigserial PRIMARY KEY,
    trade_date          text NOT NULL,  -- yyyymmdd
    pool_type           text NOT NULL,  -- zt | dt | zb
    stock_code          text NOT NULL,
    stock_name          text,
    latest_price        real,
    change_percent      real,
    zt_price            real,
    volume              bigint,
    amount              real,
    limit_up_time       text,
    limit_up_type       text,
    consecutive_boards  integer,
    industry            text,
    dt_price            real,
    zb_info             text,
    created_at          timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT zt_pool_date_type_code_key UNIQUE (trade_date, pool_type, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_zt_pool_code ON zt_pool (stock_code);
CREATE INDEX IF NOT EXISTS idx_zt_pool_type ON zt_pool (pool_type);
CREATE INDEX IF NOT EXISTS idx_zt_pool_date ON zt_pool (trade_date);
