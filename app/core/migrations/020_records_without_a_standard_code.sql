-- A test's history also takes in results filed under an institution's own code or none, matched by name. Those are few, so they
-- get an index of their own, instead of walking through all of a person's labs for every test Insights looks at.

CREATE INDEX IF NOT EXISTS idx_records_unstandard_code ON clinical_records(profile_id, category, title, code, code_system)
    WHERE code IS NULL OR code_system IS NULL OR code_system != 'http://loinc.org';
