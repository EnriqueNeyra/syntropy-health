-- Sample counts per source (the Sources page) and per source name (the Activity overview) read an index instead of
-- scanning every sample.

CREATE INDEX IF NOT EXISTS idx_samples_connection ON biometric_samples(connection_id);
CREATE INDEX IF NOT EXISTS idx_samples_profile_source ON biometric_samples(profile_id, source_name, start_date);
