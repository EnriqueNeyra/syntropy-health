-- Corrections to wearable readings: when a reading last changed, why one is left out of the daily numbers, the offset
-- from UTC where it was measured when the source says, and a person's own home time zone. Adding a column doesn't touch
-- existing rows; what they need is done in the background (app/services/wearable_fixes.py).

ALTER TABLE biometric_samples ADD COLUMN updated_at REAL;
-- range, unit or date: kept, but not in the rollups
ALTER TABLE biometric_samples ADD COLUMN flag TEXT;
ALTER TABLE biometric_samples ADD COLUMN tz_offset_min INTEGER;
ALTER TABLE workouts ADD COLUMN updated_at REAL;
ALTER TABLE workouts ADD COLUMN tz_offset_min INTEGER;
ALTER TABLE health_events ADD COLUMN updated_at REAL;
ALTER TABLE health_events ADD COLUMN tz_offset_min INTEGER;
-- For a Combined row: the sources merged into it.
ALTER TABLE biometric_daily ADD COLUMN contributors TEXT;
ALTER TABLE profiles ADD COLUMN timezone TEXT;

CREATE INDEX IF NOT EXISTS idx_samples_flagged ON biometric_samples(profile_id, metric_type) WHERE flag IS NOT NULL;

-- What kind of device each source really is, from what it says about itself rather than from its name.
CREATE TABLE IF NOT EXISTS data_sources (
    profile_id   TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    device_kind  TEXT NOT NULL,
    device_name  TEXT,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (profile_id, source_name)
);

-- A reading the person removed: a later sync of the same id doesn't bring it back.
CREATE TABLE IF NOT EXISTS sample_tombstones (
    profile_id  TEXT NOT NULL,
    sample_id   TEXT NOT NULL,
    deleted_at  REAL NOT NULL,
    PRIMARY KEY (profile_id, sample_id)
);

-- A person whose daily numbers are being worked out again a month at a time; a restart carries on from next_day.
CREATE TABLE IF NOT EXISTS daily_rebuilds (
    profile_id    TEXT PRIMARY KEY,
    next_day      TEXT,
    last_day      TEXT,
    requested_at  REAL NOT NULL
);

-- Apple Health imports, so a page can find one again and a restart can say what became of it.
CREATE TABLE IF NOT EXISTS import_jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    filename     TEXT NOT NULL,
    profile_id   TEXT NOT NULL,
    status       TEXT NOT NULL,
    processed    INTEGER NOT NULL DEFAULT 0,
    inserted     INTEGER NOT NULL DEFAULT 0,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    error        TEXT,
    result_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_jobs_profile ON import_jobs(profile_id, started_at DESC);
