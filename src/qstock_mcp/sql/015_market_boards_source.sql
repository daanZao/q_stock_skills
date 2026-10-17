-- market_boards 加 source 列（issue #84，spec #71）：区分实时采集与历史回补。
-- NULL = 实时采集（历史存量行保持 NULL，不回填——落库时未记源，不臆断）；
-- 'push2his' = 调度 backfill 子命令的东财 push2his 板块 K 线回补。
-- 承载：写 scheduler backfill（source='push2his'）；读 query_board_data（区分实时/回补口径）
ALTER TABLE market_boards ADD COLUMN IF NOT EXISTS source text;
