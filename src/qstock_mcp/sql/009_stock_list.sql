-- 股票清单（issue #8）：全市场 A 股代码/名称，init 轻量初始化与全量回溯的标的来源
CREATE TABLE IF NOT EXISTS stock_list (
    stock_code  text PRIMARY KEY,
    stock_name  text NOT NULL,
    source      text,             -- 实际数据源
    updated_at  timestamp DEFAULT CURRENT_TIMESTAMP
);
