-- 复权切读状态（issue #166）：表名状态之外持久化模式与事实水位，支持修复回滚。
-- 承载：写 adjustment_switch 固定 CLI；读 adjustment_switch 状态预检。
CREATE TABLE IF NOT EXISTS stock_adjustment_switch_state (
    id               smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    mode             text NOT NULL CHECK (mode IN ('legacy', 'new')),
    fact_watermark   timestamptz,
    switched_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO stock_adjustment_switch_state (id, mode)
VALUES (1, 'legacy')
ON CONFLICT (id) DO NOTHING;
