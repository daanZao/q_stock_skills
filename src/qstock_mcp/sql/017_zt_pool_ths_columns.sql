-- zt_pool 增加同花顺涨停揭秘承载的两个列（issue #120，spec #111，ADR 0012）：
--   sealed_amount  封单额（万元，ADR 0006；THS order_amount 为元，换算在适配器映射层）
--   reason_type    涨停原因题材（THS reason_type 原文，如"医药+原料药"）
-- 新库建表见 005（一次性建全量列的基线留待下次全量重建，现库走本幂等迁移）；
-- 本文件承担现库迁移：ADD COLUMN IF NOT EXISTS 幂等，重复执行不重复迁移。
-- 回滚：ALTER TABLE zt_pool DROP COLUMN IF EXISTS sealed_amount;
--       ALTER TABLE zt_pool DROP COLUMN IF EXISTS reason_type;
-- 承载：写 fetch_board_snapshot(zt_pool 段，THS 源，issue #120)；
-- 读 query_board_data / limit-up-analysis（龙头点评封单额与题材归因）
ALTER TABLE zt_pool ADD COLUMN IF NOT EXISTS sealed_amount real;
ALTER TABLE zt_pool ADD COLUMN IF NOT EXISTS reason_type text;
