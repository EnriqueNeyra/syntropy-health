-- Corrections to data already stored, each done once per database, in the background (see app/core/data_fixes.py).

CREATE TABLE IF NOT EXISTS data_fixes (
    name         TEXT PRIMARY KEY,
    state        TEXT NOT NULL,          -- running, done, failed
    cursor_json  TEXT,                   -- where it got to; saved with the writes of each step
    steps        INTEGER NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    started_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    finished_at  REAL
);
