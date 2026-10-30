-- Which document in the record search index holds which record. The index itself is a full-text table, created by
-- app/store/search.py when this build of SQLite has FTS5, and filled in the background: a migration can't depend on a
-- compile option, and has to stay quick.
CREATE TABLE IF NOT EXISTS record_search_ids (
    n         INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL UNIQUE
);
