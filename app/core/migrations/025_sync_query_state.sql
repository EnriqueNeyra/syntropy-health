-- Where each health record query got to: a long history finishes over several syncs, and a query the server has never
-- answered isn't reported as a new failure every time. One row per query per source, so this stays small.
CREATE TABLE IF NOT EXISTS sync_query_state (
    connection_id   TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    query           TEXT NOT NULL,          -- "Observation?category=laboratory"
    status          TEXT NOT NULL,          -- ok, empty, not-granted, unsupported, forbidden, paused, truncated, error:*
    record_count    INTEGER NOT NULL DEFAULT 0,
    next_url        TEXT,                   -- the page to carry on from, if the last sync didn't finish this query
    alternative     INTEGER NOT NULL DEFAULT 0,     -- which of the query's alternatives that page belongs to
    no_sort         INTEGER NOT NULL DEFAULT 0,     -- the server refuses newest-first
    ever_answered   INTEGER NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (connection_id, query)
) WITHOUT ROWID;

-- When to try a source again after a temporary failure, or to carry on with a history that didn't finish.
ALTER TABLE connections ADD COLUMN next_retry_at REAL;
ALTER TABLE connections ADD COLUMN sync_failures INTEGER NOT NULL DEFAULT 0;
