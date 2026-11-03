-- What records refer to, from the same source: the Medication behind a prescription, the doctor, the place. Kept so an
-- export is whole: without these a medication's name and code exist only on the normalized record.
CREATE TABLE IF NOT EXISTS fhir_references (
    connection_id  TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    resource_type  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    raw_json       TEXT NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (connection_id, resource_type, resource_id)
);
