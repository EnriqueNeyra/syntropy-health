-- How many syncs in a row a query's saved place has failed, and, for a history too long for one sync, when it was last
-- read whole (0 until the first time). The table has one row per query per source, so this is quick.
ALTER TABLE sync_query_state ADD COLUMN place_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_query_state ADD COLUMN swept_at REAL;
