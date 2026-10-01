-- Each pairing used to create a new source, so re-pairing a phone left a trail of disconnected copies (with the phone's
-- data split between them) and revoked tokens listed under Sources. Fold every phone's copies into one source per
-- person and name, keeping the one a paired device still uses (or else the newest), and drop revoked tokens.

CREATE TEMP TABLE device_keep AS
SELECT c.profile_id, c.display_name,
       (SELECT c2.id FROM connections c2
         WHERE c2.kind = 'device' AND c2.profile_id = c.profile_id AND c2.display_name = c.display_name
         ORDER BY EXISTS(SELECT 1 FROM devices d WHERE d.connection_id = c2.id AND d.revoked_at IS NULL) DESC,
                  c2.created_at DESC
         LIMIT 1) AS keep_id
  FROM connections c WHERE c.kind = 'device'
 GROUP BY c.profile_id, c.display_name;

CREATE TEMP TABLE device_remap AS
SELECT c.id AS old_id, k.keep_id FROM connections c
  JOIN device_keep k ON k.profile_id = c.profile_id AND k.display_name = c.display_name
 WHERE c.kind = 'device' AND c.id != k.keep_id;

UPDATE biometric_samples SET connection_id = (SELECT keep_id FROM device_remap WHERE old_id = biometric_samples.connection_id)
 WHERE connection_id IN (SELECT old_id FROM device_remap);
UPDATE workouts SET connection_id = (SELECT keep_id FROM device_remap WHERE old_id = workouts.connection_id)
 WHERE connection_id IN (SELECT old_id FROM device_remap);
UPDATE health_events SET connection_id = (SELECT keep_id FROM device_remap WHERE old_id = health_events.connection_id)
 WHERE connection_id IN (SELECT old_id FROM device_remap);
UPDATE devices SET connection_id = (SELECT keep_id FROM device_remap WHERE old_id = devices.connection_id)
 WHERE connection_id IN (SELECT old_id FROM device_remap);

-- The kept source takes the most recent sync time of its copies.
UPDATE connections SET last_sync_at = (
    SELECT MAX(c2.last_sync_at) FROM connections c2
     WHERE c2.id = connections.id OR c2.id IN (SELECT old_id FROM device_remap WHERE keep_id = connections.id))
 WHERE id IN (SELECT keep_id FROM device_remap);

DELETE FROM connections WHERE id IN (SELECT old_id FROM device_remap);

-- Revoked tokens can never be used again; the audit log keeps the history.
DELETE FROM devices WHERE revoked_at IS NOT NULL;

-- A phone source is paired exactly when a device uses it.
UPDATE connections SET status = CASE WHEN EXISTS(SELECT 1 FROM devices d WHERE d.connection_id = connections.id)
                                     THEN 'active' ELSE 'disconnected' END
 WHERE kind = 'device';

DROP TABLE device_remap;
DROP TABLE device_keep;
