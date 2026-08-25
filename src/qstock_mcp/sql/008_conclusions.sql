-- 通用结论表：schema 对未来 skill 保持开放（docs/adr/0003）
-- 业务唯一键在 schema 层钉死（旧 analysis_conclusions 无唯一约束、sequence 漂移的教训）
CREATE TABLE IF NOT EXISTS conclusions (
    id              bigserial PRIMARY KEY,
    subject_type    text NOT NULL,  -- market | stock
    subject_code    text NOT NULL,  -- market 级结论为 '_market'，个股为代码
    trade_date      text NOT NULL,  -- yyyymmdd
    conclusion_type text NOT NULL,  -- 写入方 skill 自报，如 daily_review.close / sepa.stage
    payload         jsonb NOT NULL,
    created_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    updated_at      timestamp DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT conclusions_business_key
        UNIQUE (subject_type, subject_code, trade_date, conclusion_type)
);
CREATE INDEX IF NOT EXISTS idx_conclusions_subject ON conclusions (subject_type, subject_code);
CREATE INDEX IF NOT EXISTS idx_conclusions_date ON conclusions (trade_date);
CREATE INDEX IF NOT EXISTS idx_conclusions_type ON conclusions (conclusion_type);
