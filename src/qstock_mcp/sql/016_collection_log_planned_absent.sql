-- 采集台账 status 加第四态 planned_absent（issue #113，spec #111/#102，ADR 0007 补记）：
-- 已定方向待接线的 section 以第四态占位——收盘后批每运行为其落一条占位台账
-- （不抓取、零落库），复盘历史模式就绪归因由此区分"计划缺席"（非故障、非未采集）
-- 与"未采集"。永久弃用面不占位：直接从采集清单删除（如概念维度资金流字段）。
-- 新库建表见 013（四值 CHECK 一步到位）；本文件承担现库迁移：status 上仍挂
-- 三值 CHECK 时换为四值，幂等（换过即 no-op，重复执行不重复迁移）。
-- 承载：写 tools_board fetch_board_snapshot（planned_absent 占位落账）；
-- 读 query_collection_log / daily-review 就绪归因（status=planned_absent 过滤）
DO $$
DECLARE
    chk_name text;
BEGIN
    SELECT c.conname INTO chk_name
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE c.contype = 'c' AND t.relname = 'collection_log' AND n.nspname = 'public'
      AND pg_get_constraintdef(c.oid) LIKE '%status%'
      AND pg_get_constraintdef(c.oid) NOT LIKE '%planned_absent%';
    IF chk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE collection_log DROP CONSTRAINT %I', chk_name);
        EXECUTE 'ALTER TABLE collection_log ADD CONSTRAINT collection_log_status_check
            CHECK (status IN (''ok'', ''empty'', ''error'', ''planned_absent''))';
    END IF;
END $$;
