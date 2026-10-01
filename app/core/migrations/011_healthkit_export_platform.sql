-- Devices that send HealthKit export JSON (POST /api/ingest/healthkit-export) are now platform 'healthkit-export'.
UPDATE devices SET platform = 'healthkit-export' WHERE platform = 'hae';
UPDATE connections SET metadata_json = json_set(metadata_json, '$.platform', 'healthkit-export')
    WHERE kind = 'device' AND json_valid(metadata_json) AND json_extract(metadata_json, '$.platform') = 'hae';
