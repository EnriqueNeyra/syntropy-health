-- Joining used to hand every phone paired for a person over to that person's new account, whoever had paired it, so the
-- phone's holder could open their whole record. And someone whose access had been cut down kept the phone they'd paired.
-- Those phones stop here. A phone older than the account it's tied to can't have been paired by that account.
CREATE TEMP TABLE stale_phones AS
SELECT d.id, d.connection_id, d.profile_id, d.name FROM devices d
JOIN accounts a ON a.id = d.account_id
WHERE (a.profile_id = d.profile_id AND a.id != 'acc_owner' AND d.created_at < a.created_at)
   OR (a.profile_id != d.profile_id
       AND NOT EXISTS (SELECT 1 FROM profile_access g WHERE g.account_id = a.id AND g.profile_id = d.profile_id AND g.level = 'manage')
       AND (a.role != 'owner' OR EXISTS (SELECT 1 FROM accounts o WHERE o.profile_id = d.profile_id)));

INSERT INTO audit_log(at, actor, action, profile_id, detail)
SELECT CAST(strftime('%s', 'now') AS REAL), 'system', 'device.revoked', profile_id, json_object('name', name, 'why', 'paired by someone else')
FROM stale_phones;

UPDATE connections SET status = 'disconnected', updated_at = CAST(strftime('%s', 'now') AS REAL)
WHERE id IN (SELECT connection_id FROM stale_phones)
  AND NOT EXISTS (SELECT 1 FROM devices d WHERE d.connection_id = connections.id AND d.id NOT IN (SELECT id FROM stale_phones));

DELETE FROM user_sessions WHERE device_id IN (SELECT id FROM stale_phones);
DELETE FROM devices WHERE id IN (SELECT id FROM stale_phones);
DROP TABLE stale_phones;
