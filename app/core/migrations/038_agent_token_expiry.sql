-- When an AI app's token stops working, if it was given an end. Tokens made before this keep working until revoked.
ALTER TABLE agent_tokens ADD COLUMN expires_at REAL;
