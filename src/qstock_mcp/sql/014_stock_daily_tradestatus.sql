-- stock_daily 停牌语义（issue #80，spec #71/W2）：tradestatus 权威停牌列
-- 1=正常 / 0=停牌（baostock 路径写入权威值；腾讯/新浪/akshare 路径为 NULL，
-- 消费方以 query_daily 的启发式兜底区分停牌占位行与数据缺失行）
-- 承载：写 baostock 日线/次晨批（tradestatus 权威值）；读 query_daily（suspended 标注）
ALTER TABLE stock_daily ADD COLUMN IF NOT EXISTS tradestatus integer;
