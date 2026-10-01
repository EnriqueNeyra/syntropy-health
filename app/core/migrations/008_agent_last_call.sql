-- The last tool each agent token called, so Settings can show that an app is connected and working.
ALTER TABLE agent_tokens ADD COLUMN last_call TEXT;
ALTER TABLE agent_tokens ADD COLUMN last_call_at REAL;
