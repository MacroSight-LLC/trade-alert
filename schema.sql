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
    outcome             VARCHAR(20) CHECK (outcome IN ('WIN','LOSS','SCRATCH','EXPIRED')),
    outcome_pnl         DECIMAL(10,4),
    outcome_pnl_pct     DECIMAL(8,4)
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

-- Partial index for open alerts (outcome not yet resolved)
CREATE INDEX IF NOT EXISTS idx_alerts_open_symbol
    ON alerts(symbol, created_at DESC) WHERE outcome IS NULL;

-- Partial index for recent unresolved alerts (outcome tracker queries)
CREATE INDEX IF NOT EXISTS idx_alerts_open_created
    ON alerts(created_at DESC) WHERE outcome IS NULL;

-- CHECK constraints: edge_probability and confidence must be in [0, 1]
DO $$
BEGIN
    ALTER TABLE alerts ADD CONSTRAINT chk_edge_probability
        CHECK (edge_probability >= 0 AND edge_probability <= 1);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE alerts ADD CONSTRAINT chk_confidence
        CHECK (confidence >= 0 AND confidence <= 1);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

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

-- ── Migration helpers (safe to re-run) ──────────────────────────────
-- Add EXPIRED to outcome CHECK if not already present
DO $$
BEGIN
    ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_outcome_check;
    ALTER TABLE alerts ADD CONSTRAINT alerts_outcome_check
        CHECK (outcome IN ('WIN','LOSS','SCRATCH','EXPIRED'));
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

-- Add outcome_pnl_pct column for percentage-based PnL tracking
DO $$
BEGIN
    ALTER TABLE alerts ADD COLUMN outcome_pnl_pct DECIMAL(8,4);
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- Add forecast tracking columns for TimesFM signal analytics
DO $$
BEGIN
    ALTER TABLE alerts ADD COLUMN forecast_score DECIMAL(4,3);
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE alerts ADD COLUMN forecast_contradicted BOOLEAN DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- Index for forecast analytics queries
CREATE INDEX IF NOT EXISTS idx_alerts_forecast_contradicted
    ON alerts(forecast_contradicted) WHERE forecast_contradicted = TRUE;

-- Add langfuse_trace_id column for outcome → trace linkage
DO $$
BEGIN
    ALTER TABLE alerts ADD COLUMN langfuse_trace_id VARCHAR(64);
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- Index for Langfuse trace → alert lookups (outcome_tracker, trace_analyzer)
CREATE INDEX IF NOT EXISTS idx_alerts_trace_id
    ON alerts(langfuse_trace_id) WHERE langfuse_trace_id IS NOT NULL;

-- Composite index for historical win-rate lookup in notifier embeds
-- Covers: WHERE symbol = X AND direction = X AND edge_probability BETWEEN X AND X
--         AND outcome IN ('WIN','LOSS') AND created_at > NOW() - 30 days
CREATE INDEX IF NOT EXISTS idx_alerts_winrate_lookup
    ON alerts(symbol, direction, edge_probability, created_at DESC)
    WHERE outcome IN ('WIN', 'LOSS');

-- Execution bridge: idempotency key + dispatch tracking
DO $$
BEGIN
    ALTER TABLE alerts ADD COLUMN idempotency_key VARCHAR(36);
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE alerts ADD CONSTRAINT uq_alerts_idempotency_key UNIQUE (idempotency_key);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE alerts ADD COLUMN execution_dispatched BOOLEAN NOT NULL DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_alerts_execution_dispatched
    ON alerts(execution_dispatched) WHERE execution_dispatched = TRUE;

-- ── Partitioning prep (run manually when alerts table exceeds ~1M rows) ─────
-- Convert the alerts table to range-partitioned by created_at.
-- This is a one-time migration: create the partitioned table, migrate data,
-- then swap.  Kept here as a reference recipe — NOT auto-executed on init.
--
-- Step 1: Create partitioned copy
--   CREATE TABLE alerts_partitioned (LIKE alerts INCLUDING ALL)
--       PARTITION BY RANGE (created_at);
--
-- Step 2: Create initial partitions (monthly)
--   CREATE TABLE alerts_y2025m01 PARTITION OF alerts_partitioned
--       FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');
--   -- repeat for each month...
--   CREATE TABLE alerts_default PARTITION OF alerts_partitioned DEFAULT;
--
-- Step 3: Migrate data inside a transaction
--   BEGIN;
--     INSERT INTO alerts_partitioned SELECT * FROM alerts;
--     ALTER TABLE alerts RENAME TO alerts_old;
--     ALTER TABLE alerts_partitioned RENAME TO alerts;
--   COMMIT;
--
-- Step 4: Auto-create future partitions via pg_partman or a cron job:
--   CREATE EXTENSION IF NOT EXISTS pg_partman;
--   SELECT partman.create_parent('public.alerts', 'created_at', 'native', 'monthly');

-- ── Column constraint tightening (safe to re-run) ──────────────────────
-- Backfill NULLs before adding NOT NULL constraints.

UPDATE alerts SET sources_agree = 0 WHERE sources_agree IS NULL;
UPDATE alerts SET unusual_activity = '{}'::jsonb WHERE unusual_activity IS NULL;
UPDATE alerts SET forecast_contradicted = FALSE WHERE forecast_contradicted IS NULL;

DO $$
BEGIN
    ALTER TABLE alerts ALTER COLUMN sources_agree SET DEFAULT 0;
    ALTER TABLE alerts ALTER COLUMN sources_agree SET NOT NULL;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE alerts ALTER COLUMN unusual_activity SET DEFAULT '{}'::jsonb;
    ALTER TABLE alerts ALTER COLUMN unusual_activity SET NOT NULL;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE alerts ALTER COLUMN forecast_contradicted SET DEFAULT FALSE;
    ALTER TABLE alerts ALTER COLUMN forecast_contradicted SET NOT NULL;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

-- ── Execution delivery audit table ────────────────────────────────────────
-- Records every outbound delivery attempt to trade-execute.
-- event_id is UNIQUE so ON CONFLICT can upsert the final outcome.
-- Safe to re-run: all DDL uses IF NOT EXISTS / DO $$ exception guards.

CREATE TABLE IF NOT EXISTS execution_deliveries (
    id              SERIAL PRIMARY KEY,
    event_id        VARCHAR(36)  NOT NULL,
    symbol          VARCHAR(20)  NOT NULL,
    direction       VARCHAR(10)  NOT NULL,
    alert_class     VARCHAR(10)  NOT NULL,
    status          VARCHAR(20)  NOT NULL
                    CHECK (status IN ('success', 'failed', 'dry_run')),
    http_status     INTEGER,
    attempt_count   INTEGER      NOT NULL DEFAULT 1,
    error_detail    TEXT,
    payload_hash    CHAR(64),
    sent_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    ALTER TABLE execution_deliveries
        ADD CONSTRAINT uq_execution_deliveries_event_id UNIQUE (event_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_exec_deliveries_symbol
    ON execution_deliveries(symbol);

CREATE INDEX IF NOT EXISTS idx_exec_deliveries_status
    ON execution_deliveries(status);

CREATE INDEX IF NOT EXISTS idx_exec_deliveries_sent_at
    ON execution_deliveries(sent_at DESC);

CREATE INDEX IF NOT EXISTS idx_exec_deliveries_symbol_sent
    ON execution_deliveries(symbol, sent_at DESC);

-- ── Legacy / trial alerts (curated local dev catalog; prod alerts untouched) ──

CREATE TABLE IF NOT EXISTS legacy_alerts (
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
    unusual_activity    JSONB DEFAULT '[]'::jsonb,
    macro_regime        TEXT,
    sources_agree       INTEGER,
    raw_snapshots       JSONB DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    outcome             VARCHAR(20) CHECK (outcome IN ('WIN','LOSS','SCRATCH','EXPIRED')),
    outcome_pnl         DECIMAL(10,4),
    outcome_pnl_pct     DECIMAL(8,4),
    forecast_score      DECIMAL(4,3),
    forecast_contradicted BOOLEAN DEFAULT FALSE,
    langfuse_trace_id   VARCHAR(64),
    source_alert_id     INTEGER NOT NULL,
    legacy_note         TEXT,
    source_dump         TEXT NOT NULL,
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_legacy_alerts_source UNIQUE (source_alert_id, source_dump)
);

CREATE INDEX IF NOT EXISTS idx_legacy_alerts_created_at
    ON legacy_alerts(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_legacy_alerts_symbol
    ON legacy_alerts(symbol);
