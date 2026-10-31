-- Records a source stopped returning, or took back. They leave clinical_records, so no page, summary, export or AI tool
-- can show them as fact, and wait here where the person can see them and put them back. The row is kept as JSON so that
-- it can still be restored after clinical_records gains columns.
CREATE TABLE IF NOT EXISTS clinical_records_removed (
    id              TEXT PRIMARY KEY,
    profile_id      TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    connection_id   TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource_type   TEXT NOT NULL,
    resource_id     TEXT NOT NULL,
    category        TEXT NOT NULL,
    title           TEXT NOT NULL,
    effective_at    TEXT,
    source_name     TEXT,
    first_seen_at   REAL NOT NULL,
    row_json        TEXT NOT NULL,
    removed_at      REAL NOT NULL,
    reason          TEXT NOT NULL           -- 'missing' (no longer returned) or 'retracted' (the source says it was wrong)
);
CREATE INDEX IF NOT EXISTS idx_removed_connection ON clinical_records_removed(connection_id, removed_at);
CREATE INDEX IF NOT EXISTS idx_removed_profile ON clinical_records_removed(profile_id);

-- When a complete answer from the source first left this record out; a later complete answer has to agree before it goes.
ALTER TABLE clinical_records ADD COLUMN missing_since REAL;
-- The person put this record back after its source stopped returning it, so syncing leaves it alone.
ALTER TABLE clinical_records ADD COLUMN kept_at REAL;
