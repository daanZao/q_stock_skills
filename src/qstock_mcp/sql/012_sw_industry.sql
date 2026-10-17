-- 成分字典与行业名称表（issue #72，spec #70）
-- sw_industry_class 多体系同构形态：source_system（'sw'/'em' 等，为东财概念成分预留），
-- 主键 (source_system, stock_code, board_code, effective_date)。
-- 现库已有手工建表（stock_code/effective_date/industry_code/updated_at，
-- 主键 (stock_code, effective_date)，issue #67 入库 12,929 行）：本文件同时承担
-- 新库建表与现库迁移——industry_code 改名 board_code、加 source_system 回填 'sw'、
-- 主键扩为四元组，已有行不丢。幂等：迁移以旧列 industry_code 是否存在为条件，
-- 重复执行不重复迁移。

-- 行业名称表
CREATE TABLE IF NOT EXISTS sw_industry_meta (
    industry_code  char(6) PRIMARY KEY,
    name           text NOT NULL,
    level          smallint NOT NULL CHECK (level IN (1, 2, 3))
);

-- 成分字典（新库直接建成目标形态；现库旧表存在时 CREATE 为空转）
CREATE TABLE IF NOT EXISTS sw_industry_class (
    source_system  text NOT NULL DEFAULT 'sw',
    stock_code     text NOT NULL,
    board_code     char(6) NOT NULL,
    effective_date date NOT NULL,
    updated_at     timestamp DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_system, stock_code, board_code, effective_date)
);

DO $$
DECLARE
    pk_name text;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'sw_industry_class'
          AND column_name = 'industry_code'
    ) THEN
        ALTER TABLE sw_industry_class RENAME COLUMN industry_code TO board_code;

        ALTER TABLE sw_industry_class ADD COLUMN source_system text;
        UPDATE sw_industry_class SET source_system = 'sw';
        ALTER TABLE sw_industry_class ALTER COLUMN source_system SET NOT NULL;
        ALTER TABLE sw_industry_class ALTER COLUMN source_system SET DEFAULT 'sw';

        SELECT c.conname INTO pk_name
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE c.contype = 'p' AND t.relname = 'sw_industry_class'
          AND n.nspname = 'public';
        IF pk_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE sw_industry_class DROP CONSTRAINT %I', pk_name);
        END IF;
        ALTER TABLE sw_industry_class ADD CONSTRAINT sw_industry_class_pkey
            PRIMARY KEY (source_system, stock_code, board_code, effective_date);
    END IF;
END $$;
