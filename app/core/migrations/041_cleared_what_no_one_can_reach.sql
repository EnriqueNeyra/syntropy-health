-- Conversations with an AI that nobody can open or delete: by an account that's gone, or about someone who has since
-- joined, by a former carer who lost access to them. They hold health details and travel in every backup.
DELETE FROM chats
WHERE account_id != ''
  AND (NOT EXISTS (SELECT 1 FROM accounts a WHERE a.id = chats.account_id)
       OR (EXISTS (SELECT 1 FROM accounts o WHERE o.profile_id = chats.profile_id AND o.id != chats.account_id)
           AND NOT EXISTS (SELECT 1 FROM profile_access g WHERE g.account_id = chats.account_id AND g.profile_id = chats.profile_id)));

-- What the log said about someone since removed could name their doctors and what an AI was asked. That it happened stays.
UPDATE audit_log SET detail = NULL
WHERE detail IS NOT NULL AND profile_id IS NOT NULL AND profile_id NOT IN (SELECT id FROM profiles);

-- The same goes for what AI apps searched for, which the log no longer keeps for anyone.
UPDATE audit_log SET detail = NULL WHERE detail IS NOT NULL AND action LIKE 'mcp.%';
