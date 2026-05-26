-- Curated trial/dev alerts (prod `alerts` table untouched).
-- Apply: psql "$DATABASE_URL" -f scripts/migrate_legacy_alerts.sql

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
