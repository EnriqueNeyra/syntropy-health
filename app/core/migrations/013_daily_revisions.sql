-- Each person's daily rollups get a revision number, raised whenever they're rebuilt or deleted, so what's worked out
-- from them (the metrics a person has, the Overview's numbers) can be kept until it changes instead of recomputed on
-- every request.
CREATE TABLE IF NOT EXISTS daily_revisions (
    profile_id  TEXT PRIMARY KEY,
    rev         INTEGER NOT NULL
);

-- The metrics a person has, and from which sources, read from the index alone (it used to look up every row).
CREATE INDEX IF NOT EXISTS idx_daily_metric_source ON biometric_daily(profile_id, metric_type, day, source_name);
DROP INDEX IF EXISTS idx_daily_metric;
