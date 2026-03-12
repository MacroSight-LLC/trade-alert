-- trade-alert schema v1.0
-- Run once: psql -U trade_alert -d trade_alert -f schema.sql

DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pgvector;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgvector unavailable — skipping.';
END $$;

CREATE TABLE IF NOT EXISTS alerts (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    direction       VARCHAR(10) NOT NULL CHECK (direction IN ('LONG','SHORT','WATCH')),
    edge_probability DECIMAL(4,3) NOT NULL,
    confidence      DECIMAL(4,3) NOT NULL,
    timeframe       VARCHAR(5) NOT NULL,
    thesis          TEXT NOT NULL,
    entry           JSONB NOT NULL,
    timeframe_rationale TEXT,
    sentiment_context   TEXT,
    unusual_activity    JSONB,
    macro_regime        TEXT,
    sources_agree       INTEGER,
    raw_snapshots       JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    outcome             VARCHAR(20) CHECK (outcome IN ('WIN','LOSS','SCRATCH')),
    outcome_pnl         DECIMAL(10,4)
);

-- Auto-set updated_at on row modification
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_alerts_updated_at ON alerts;
CREATE TRIGGER trg_alerts_updated_at
    BEFORE UPDATE ON alerts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Indexes for analytics queries
CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_edge_prob ON alerts(edge_probability);
CREATE INDEX IF NOT EXISTS idx_alerts_outcome ON alerts(outcome)
    WHERE outcome IS NOT NULL;

-- Composite index for symbol-specific history lookups
CREATE INDEX IF NOT EXISTS idx_alerts_symbol_created ON alerts(symbol, created_at DESC);

-- Direction filter for analytics breakdowns
CREATE INDEX IF NOT EXISTS idx_alerts_direction ON alerts(direction);

-- Timeframe filter for per-timeframe analytics
CREATE INDEX IF NOT EXISTS idx_alerts_timeframe ON alerts(timeframe);

-- View: winrate by edge_probability bucket
CREATE OR REPLACE VIEW winrate_by_bucket AS
SELECT
    ROUND(edge_probability::numeric, 1) AS bucket,
    COUNT(*) AS total,
    SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
    ROUND(
        SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END)::numeric
        / NULLIF(COUNT(*), 0), 4
    ) AS winrate,
    ROUND(AVG(outcome_pnl)::numeric, 4) AS avg_pnl
FROM alerts
WHERE outcome IS NOT NULL
GROUP BY bucket
ORDER BY bucket DESC;
