-- Access tokens for AI agents that connect over MCP (Claude, ChatGPT, Gemini, local agents...).
-- Read-only; optionally limited to one profile. Only the SHA-256 hash of each token is stored.

CREATE TABLE IF NOT EXISTS agent_tokens (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    profile_id    TEXT,                    -- NULL: every profile on this instance
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    revoked_at    REAL
);
