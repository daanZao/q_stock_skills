-- 采集台账（issue #77，spec #71/W4）：一切写库动作的审计记录，全历史保留（append-only）。
-- 承载契约：每次采集动作（fetch/init/调度/手动补抓）落一行；status 三态
-- （ok/empty/error）与 error_kind 词汇同 docs/adr/0007 三态契约，外加编排
-- 第四态 planned_absent（issue #113，spec #111/#102：已定方向待接线的 section
-- 占位缺席，非抓取结果非故障；现库三值约束经 016 幂等迁移为四值）。
-- 复盘就绪检查先读本表："无台账行"= 未采集，"empty"= 上游确认无数据，
-- "error"= 采集失败，"planned_absent"= 计划缺席（迁移占位，非故障）。
-- 承载：写一切写库路径（ledger.record_collection）；读 query_collection_log（就绪检查归因）
CREATE TABLE IF NOT EXISTS collection_log (
    id          bigserial PRIMARY KEY,
    trade_date  char(8) NOT NULL,   -- 采集目标交易日 yyyymmdd（区间采集记区间尾日，区间见 detail）
    section     text NOT NULL,      -- 采集对象：盘面 section 名（indices/boards/...）或表名（stock_daily/market_snapshot/...）
    status      text NOT NULL CHECK (status IN ('ok', 'empty', 'error', 'planned_absent')),
    source      text,               -- 实际数据源（tencent/sina/baostock/akshare/ths/exchange/selfcalc/file 等）；失败为 NULL
    rows        integer,            -- 本次写库行数
    error_kind  text,               -- ADR 0007：upstream_unreachable / fetch_failed / no_data_for_date
    detail      text,               -- 摘要：逐源明细 / 区间 / 部分失败计数等
    started_at  timestamptz,        -- 动作开始时间
    finished_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_collection_log_date ON collection_log (trade_date);
CREATE INDEX IF NOT EXISTS idx_collection_log_section ON collection_log (section, trade_date);
CREATE INDEX IF NOT EXISTS idx_collection_log_status ON collection_log (status);
