-- The documents behind a record: a scanned note, a PDF report, the health summary a file import came from. In the one database,
-- so a backup holds them and removing a source removes them. A row with no bytes says a file was found and not kept (too large).
CREATE TABLE IF NOT EXISTS record_files (
    id             TEXT PRIMARY KEY,
    profile_id     TEXT NOT NULL,
    connection_id  TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource_type  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    name           TEXT,
    content_type   TEXT NOT NULL,
    size           INTEGER NOT NULL,
    sha256         TEXT,
    fingerprint    TEXT,
    state          TEXT NOT NULL,
    stored_at      REAL NOT NULL,
    data           BLOB
);
CREATE INDEX IF NOT EXISTS idx_record_files_record ON record_files(connection_id, resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_record_files_profile ON record_files(profile_id);
