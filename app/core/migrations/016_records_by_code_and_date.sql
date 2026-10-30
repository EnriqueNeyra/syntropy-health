-- One lab test's results over time, read from an index in date order. The planner preferred walking through all of a
-- person's labs by date, which took 40 ms per test at 25,000 records and made Insights take seconds.

CREATE INDEX IF NOT EXISTS idx_records_code_date ON clinical_records(profile_id, code, effective_at);
DROP INDEX IF EXISTS idx_records_profile_code;
