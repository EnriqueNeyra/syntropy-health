-- A person's newest samples, whatever the metric, read from an index instead of sorting all of theirs. Nothing used the
-- index on the date alone, since every query asks about one person.

CREATE INDEX IF NOT EXISTS idx_samples_profile_start ON biometric_samples(profile_id, start_date DESC);
DROP INDEX IF EXISTS idx_samples_start;
