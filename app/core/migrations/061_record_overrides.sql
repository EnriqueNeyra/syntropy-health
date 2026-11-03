-- What a person says about an entry in their record: they aren't taking it, take it differently, it's wrong or isn't theirs, or
-- a note. Beside the source's records and never in them, and by the fact rather than a row, so it survives a re-sync and covers
-- every source reporting the same thing.
CREATE TABLE IF NOT EXISTS record_overrides (
    profile_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    dedup_key   TEXT NOT NULL,
    state       TEXT,
    note        TEXT,
    title       TEXT,
    category    TEXT,
    set_by      TEXT,
    set_at      REAL NOT NULL,
    PRIMARY KEY (profile_id, dedup_key)
);
