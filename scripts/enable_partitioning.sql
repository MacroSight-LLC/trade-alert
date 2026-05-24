-- Enable monthly range partitioning on alerts.created_at (FU-007).
-- Run manually when the alerts table exceeds ~1M rows.
-- Reference: schema.sql lines 176–200. NOT auto-executed on init.

-- Step 1: Create partitioned copy
CREATE TABLE alerts_partitioned (LIKE alerts INCLUDING ALL)
    PARTITION BY RANGE (created_at);

-- Step 2: Create initial partitions (monthly) — adjust dates for your data range
CREATE TABLE alerts_y2025m01 PARTITION OF alerts_partitioned
    FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');
-- repeat for each month...
CREATE TABLE alerts_default PARTITION OF alerts_partitioned DEFAULT;

-- Step 3: Migrate data inside a transaction
BEGIN;
    INSERT INTO alerts_partitioned SELECT * FROM alerts;
    ALTER TABLE alerts RENAME TO alerts_old;
    ALTER TABLE alerts_partitioned RENAME TO alerts;
COMMIT;

-- Step 4: Auto-create future partitions via pg_partman
CREATE EXTENSION IF NOT EXISTS pg_partman;
SELECT partman.create_parent('public.alerts', 'created_at', 'native', 'monthly');
