-- 资讯条目（mx-search 落库，issue #24/T2）
-- 业务唯一键 (news_code, subject_type, subject_code)：同一资讯可挂多个 subject；
-- subject 词汇沿用 ADR 0003（market 级为 market/_market）
CREATE TABLE IF NOT EXISTS news_items (
    id               bigserial PRIMARY KEY,
    news_code        text NOT NULL,   -- 上游条目 id（恒有），如 NW..._1 / AN...
    subject_type     text NOT NULL,   -- market | stock
    subject_code     text NOT NULL,   -- market 级为 '_market'，个股为代码
    information_type text NOT NULL,   -- 上游原值 NEWS/NOTICE/REPORT/INV_NEWS/WECHAT/BOND
    title            text NOT NULL,
    content          text NOT NULL,   -- 上游原文（截断版）
    publish_time     timestamptz NOT NULL,  -- 来自条目 date 字段
    source           text,            -- 以下为可选列：上游缺省即 NULL
    url              text,            -- 上游 jumpUrl（NOTICE 为 PDF 链接）
    author           text,
    ins_name         text,            -- 机构名（仅 REPORT）
    rating           text,            -- 评级（仅 REPORT）
    raw              jsonb NOT NULL,  -- 条目原文（含 rankScore/showText 等未列字段）
    fetched_at       timestamptz DEFAULT now(),
    CONSTRAINT news_items_business_key
        UNIQUE (news_code, subject_type, subject_code)
);
CREATE INDEX IF NOT EXISTS idx_news_items_subject ON news_items (subject_type, subject_code);
CREATE INDEX IF NOT EXISTS idx_news_items_publish_time ON news_items (publish_time);
